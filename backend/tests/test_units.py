"""Unit coverage for the pure logic the rest of the suite never touches.

Everything else in this project is an integration harness: concurrency_sim
simulates schedules, chaos_test drives real jobs through real Postgres and
Redis, test_auth_http goes over the wire. All of them need services, and none of
them exercises the small deterministic functions that decide what the system
actually does -- the dependency heuristic (the single highest-risk pure function
here, ~280 lines of regex with no coverage at all), display ordering, the tree
builder, event projection, the circuit breaker, the token bucket, pricing, id
generation, and DSN rewriting.

These need no database and no Redis, so they run in milliseconds and can gate
every push. Several encode bugs found during the audit, so a regression on any
of them fails rather than being reported in a log nobody reads.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app import events, pricing
from app.db import to_async_dsn
from app.dependency import (
    Source,
    Verdict,
    evaluate,
    prepare_retrospective,
    retrospective_conflict,
    retrospective_match,
    topic_words,
)
from app.ids import new_id
from app.models import Message, Role, Status
from app.ordering import order_for_display
from app.resilience import CircuitBreaker, CircuitOpen, State, backoff, is_transient
from app.scheduler import TokenBucket
from app.tree import build_tree, thread_to

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _at(micros: int) -> datetime:
    return EPOCH + timedelta(microseconds=micros)


# --- dependency heuristic --------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "continue",
        "go on",
        "tell me more",
        "elaborate",
        "in more detail",
        "summarise that",
        "do the same for Rust",
        "and why is that",
        "why?",
        "what about Python",
        "explain the above",
        "what you said was confusing",
        "your answer contradicts itself",
        "the previous answer was wrong",
        "as mentioned earlier",
    ],
)
def test_continuations_and_backreferences_are_dependent(prompt: str) -> None:
    assert evaluate(prompt).verdict == Verdict.DEPENDENT


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain how TCP congestion control works, in detail.",
        "List three sorting algorithms with one line each.",
        "Write a Python function that reverses a linked list in place.",
        "What were the main causes of the 1929 crash?",
    ],
)
def test_self_contained_prompts_are_independent(prompt: str) -> None:
    assert evaluate(prompt).verdict == Verdict.INDEPENDENT


def test_empty_prompt_is_independent_not_a_crash() -> None:
    for blank in ("", "   ", None):
        assert evaluate(blank).verdict == Verdict.INDEPENDENT


def test_quoted_keywords_are_mentioned_not_used() -> None:
    """`this` inside backticks is a keyword being discussed, not a pointer."""
    assert evaluate("explain what `this` means in JavaScript").verdict != Verdict.DEPENDENT


def test_colon_content_supplies_its_own_object() -> None:
    """'summarise this text: ...' carries the thing it refers to."""
    verdict = evaluate("summarise this text: the mitochondrion is the powerhouse of the cell").verdict
    assert verdict != Verdict.DEPENDENT


def test_relative_that_is_not_a_back_reference() -> None:
    """'a function that reverses a list' is a relative clause, not a pointer."""
    assert evaluate("write a function that reverses a list").verdict == Verdict.INDEPENDENT


def test_short_fragments_defer_rather_than_guess() -> None:
    assert evaluate("shorter").verdict == Verdict.UNSURE
    assert evaluate("in bullet points").verdict == Verdict.UNSURE


def test_pronoun_with_a_possible_local_antecedent_defers() -> None:
    detection = evaluate("the compiler crashed, why did it do that")
    assert detection.verdict in (Verdict.UNSURE, Verdict.DEPENDENT)


def test_verdicts_are_only_ever_the_three_known_values() -> None:
    known = {Verdict.DEPENDENT, Verdict.INDEPENDENT, Verdict.UNSURE}
    for prompt in ["", "hi", "continue", "it", "which one", "a" * 500, "?!", "123"]:
        assert evaluate(prompt).verdict in known


# --- retrospective check ---------------------------------------------------


def test_retrospective_split_matches_the_single_shot_helper() -> None:
    """The two-phase form must be a pure refactor of retrospective_conflict."""
    cases = [
        ("summarise that in bullet points", "The Baroque period produced ornate music and elaborate architecture."),
        ("what is the capital of France", "The Baroque period produced ornate music."),
        ("explain it again", "quantum tunnelling lets particles cross barriers, quantum barriers"),
        ("continue", ""),
    ]
    for prompt, answer in cases:
        prepared = prepare_retrospective(prompt)
        split = None if prepared is None else retrospective_match(prepared, topic_words(answer))
        assert split == retrospective_conflict(prompt, answer)


def test_self_contained_prompt_never_flags() -> None:
    assert prepare_retrospective("Explain how DNS resolution works in detail") is None


# --- display ordering ------------------------------------------------------


def _exchange(idx: int, submitted: int) -> list[Message]:
    return [
        Message(id=f"u{idx}", role=Role.USER, submitted_at=_at(submitted)),
        Message(
            id=f"a{idx}",
            role=Role.ASSISTANT,
            submitted_at=_at(submitted + 1),
            prompt_message_id=f"u{idx}",
        ),
    ]


def test_answer_sorts_with_its_prompt_not_by_its_own_time() -> None:
    rows = _exchange(0, 10) + _exchange(1, 11)
    assert [m.id for m in order_for_display(rows)] == ["u0", "a0", "u1", "a1"]


def test_display_order_is_permutation_invariant() -> None:
    """The property concurrency_sim asserts (I5), pinned here without a simulation."""
    rows = _exchange(0, 10) + _exchange(1, 11) + _exchange(2, 12)
    canonical = [m.id for m in order_for_display(rows)]
    for rotation in range(len(rows)):
        shuffled = rows[rotation:] + rows[:rotation]
        assert [m.id for m in order_for_display(shuffled)] == canonical


def test_ordering_survives_a_missing_prompt_row() -> None:
    """An orphaned answer must still sort somewhere, not raise."""
    rows = [Message(id="a9", role=Role.ASSISTANT, submitted_at=_at(5), prompt_message_id="gone")]
    assert [m.id for m in order_for_display(rows)] == ["a9"]


# --- conversation tree -----------------------------------------------------


def _node(mid: str, parent: str | None, submitted: int) -> Message:
    return Message(
        id=mid,
        role=Role.USER,
        content=f"content of {mid}",
        status=Status.COMPLETE,
        submitted_at=_at(submitted),
        parent_message_id=parent,
    )


def test_tree_nests_children_under_parents() -> None:
    roots = build_tree([_node("a", None, 1), _node("b", "a", 2), _node("c", "a", 3)])
    assert len(roots) == 1
    assert [child["id"] for child in roots[0]["children"]] == ["b", "c"]


def test_tree_drops_a_cycle_instead_of_looping() -> None:
    """Two messages naming each other must not hang the request."""
    roots = build_tree([_node("a", "b", 1), _node("b", "a", 2)])
    assert {r["id"] for r in roots} <= {"a", "b"}
    assert len(roots) >= 1


def test_tree_treats_a_self_parent_as_a_root() -> None:
    assert [r["id"] for r in build_tree([_node("a", "a", 1)])] == ["a"]


def test_thread_walks_root_to_leaf() -> None:
    messages = [_node("a", None, 1), _node("b", "a", 2), _node("c", "b", 3)]
    assert [n["id"] for n in thread_to(messages, "c")] == ["a", "b", "c"]


def test_thread_to_unknown_message_is_empty() -> None:
    assert thread_to([_node("a", None, 1)], "nope") == []


# --- event projection ------------------------------------------------------


def test_projection_reproduces_a_completed_job() -> None:
    log = [
        {"id": "1", "type": events.SUBMITTED, "message_id": "m1", "data": {}},
        {"id": "2", "type": events.STREAMING, "message_id": "m1", "data": {}},
        {
            "id": "3",
            "type": events.COMPLETED,
            "message_id": "m1",
            "data": {"content": "hello", "prompt_tokens": 7, "completion_tokens": 3},
        },
    ]
    assert events.project(log)["m1"] == {
        "status": "complete",
        "content": "hello",
        "prompt_tokens": 7,
        "completion_tokens": 3,
    }


def test_regeneration_supersedes_earlier_terminal_state() -> None:
    log = [
        {"id": "1", "type": events.COMPLETED, "message_id": "m1",
         "data": {"content": "old", "prompt_tokens": 1, "completion_tokens": 2}},
        {"id": "2", "type": events.REGENERATED, "message_id": "m1", "data": {}},
    ]
    row = events.project(log)["m1"]
    assert row["status"] == "pending" and row["content"] is None
    assert row["prompt_tokens"] is None and row["completion_tokens"] is None


def test_prefix_projection_is_time_travel() -> None:
    """Folding only the prefix shows the state at that instant, mid-flight."""
    log = [
        {"id": "1", "type": events.SUBMITTED, "message_id": "m1", "data": {}},
        {"id": "2", "type": events.STREAMING, "message_id": "m1", "data": {}},
        {"id": "3", "type": events.COMPLETED, "message_id": "m1", "data": {"content": "done"}},
    ]
    assert events.project(log[:2])["m1"]["status"] == "streaming"
    assert events.project(log)["m1"]["status"] == "complete"


def test_events_without_a_message_id_are_ignored() -> None:
    assert events.project([{"id": "1", "type": events.SUBMITTED, "message_id": None, "data": {}}]) == {}


# --- circuit breaker -------------------------------------------------------


def test_breaker_opens_after_threshold_consecutive_failures() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown=60)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state is State.CLOSED and breaker.allow()
    breaker.record_failure()
    assert breaker.state is State.OPEN and not breaker.allow()


def test_success_resets_the_failure_run() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is State.CLOSED, "failures before a success must not carry over"


def test_breaker_half_opens_after_cooldown_then_closes_on_success() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown=0.01)
    breaker.record_failure()
    assert not breaker.allow()
    time.sleep(0.02)
    assert breaker.allow() and breaker.state is State.HALF_OPEN
    breaker.record_success()
    assert breaker.state is State.CLOSED


def test_failure_while_half_open_reopens_immediately() -> None:
    breaker = CircuitBreaker(threshold=5, cooldown=0.01)
    breaker.record_failure()
    breaker._opened_at = time.monotonic() - 1
    breaker._set(State.HALF_OPEN)
    breaker.record_failure()
    assert breaker.state is State.OPEN, "one trial failure must reopen, not count toward threshold"


def test_state_change_hook_fires_only_on_transitions() -> None:
    seen: list[State] = []
    breaker = CircuitBreaker(threshold=1, cooldown=60)
    breaker.on_state_change = seen.append
    breaker.record_failure()
    breaker.record_failure()
    assert seen == [State.OPEN], "repeated failures must not re-emit the same state"


@pytest.mark.parametrize(
    "text,transient",
    [
        ("RESOURCE_EXHAUSTED", True),
        ("429 Too Many Requests", True),
        ("503 UNAVAILABLE", True),
        ("DEADLINE_EXCEEDED", True),
        ("PERMISSION_DENIED", False),
        ("NOT_FOUND", False),
        ("", False),
    ],
)
def test_transient_classification(text: str, transient: bool) -> None:
    assert is_transient(Exception(text)) is transient


def test_backoff_is_jittered_and_capped() -> None:
    for attempt in range(1, 12):
        for _ in range(20):
            delay = backoff(attempt, base=1.0, cap=30.0)
            assert 0 <= delay < 30.0 + 1e-9


# --- token bucket ----------------------------------------------------------


def test_bucket_starts_full_and_drains() -> None:
    bucket = TokenBucket(rate_per_sec=1000, capacity=3)
    assert [bucket.try_take() for _ in range(4)] == [True, True, True, False]


def test_bucket_reports_when_a_token_will_exist() -> None:
    bucket = TokenBucket(rate_per_sec=10, capacity=1)
    assert bucket.try_take()
    wait = bucket.time_until_token()
    assert 0 < wait <= 0.1 + 1e-6


def test_bucket_refills_over_time() -> None:
    bucket = TokenBucket(rate_per_sec=1000, capacity=2)
    assert bucket.try_take() and bucket.try_take() and not bucket.try_take()
    time.sleep(0.01)
    assert bucket.try_take(), "tokens must accrue"


def test_bucket_never_exceeds_capacity() -> None:
    bucket = TokenBucket(rate_per_sec=10_000, capacity=2)
    time.sleep(0.01)
    bucket._refill()
    assert bucket.tokens <= bucket.capacity


# --- pricing ---------------------------------------------------------------


def test_pricing_table_is_well_formed() -> None:
    """The table is data, and the client applies it, so its shape is the contract."""
    table = pricing.table()
    for key in ("updated", "source", "currency", "unit", "note", "models"):
        assert key in table, f"pricing.json is missing {key!r}"
    assert table["models"], "no models priced"
    for model, rate in table["models"].items():
        assert set(rate) == {"input", "output"}, f"{model} has an unexpected rate shape"
        assert rate["input"] >= 0 and rate["output"] >= 0, f"{model} has a negative rate"


def test_pricing_table_is_cached() -> None:
    assert pricing.table() is pricing.table()


# --- ids -------------------------------------------------------------------


def test_ids_are_cuid_shaped_and_unique() -> None:
    ids = {new_id() for _ in range(5_000)}
    assert len(ids) == 5_000, "collision in 5000 ids"
    assert all(i.startswith("c") and i.isalnum() and len(i) > 20 for i in ids)


def test_ids_sort_in_creation_order() -> None:
    """The timestamp prefix is base36 and fixed-width within an era, so lexical
    order tracks creation order -- relied on wherever ids break a timestamp tie."""
    first = new_id()
    time.sleep(0.005)
    assert first < new_id()


# --- DSN rewriting ---------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db?schema=public", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db?sslmode=require", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db?connection_limit=5", "postgresql+asyncpg://u:p@h:5432/db"),
    ],
)
def test_prisma_dsn_becomes_an_asyncpg_dsn(given: str, expected: str) -> None:
    assert to_async_dsn(given) == expected


def test_dsn_keeps_parameters_asyncpg_understands() -> None:
    assert "application_name=inflight" in to_async_dsn(
        "postgresql://u:p@h:5432/db?application_name=inflight&schema=public"
    )
