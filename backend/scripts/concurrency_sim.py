"""Randomised interleaving checker for the snapshot-isolation model.

    docker compose exec backend python -m scripts.concurrency_sim
    docker compose exec backend python -m scripts.concurrency_sim --runs 20000 --seed 7

The load test proves jobs *overlap*. This proves they overlap *correctly*: that
however their submit / resolve / wait / snapshot / commit steps interleave, no
job ever reads a message that committed at or after its own read snapshot. That
is the one property the whole design rests on, and asserting it once by hand is
not the same as asserting it over thousands of schedules a person would never
think to try.

The approach is a small discrete-event simulator with a virtual microsecond
clock (mirroring `utcnow()` and `timestamptz(6)`). Each generation job is a state
machine — submit -> resolve -> [wait] -> snapshot -> commit — and a seeded RNG
interleaves the ready steps of all jobs into one global schedule, exactly the way
`asyncio` interleaves the real coroutines. After each run an independent checker
replays the recorded history and asserts the invariants below.

The semantics mirror production precisely:

  * A prompt commits the instant it is submitted (user row: submitted == completed).
  * The answering job's cutoff is stamped one tick later, so it can see its own
    prompt but nothing submitted after it (conversations.py: cutoff = submitted + 1us).
  * An independent job keeps that cutoff. A dependent one waits until no
    strictly-earlier-submitted job is still in flight, then RE-STAMPS its cutoff
    (jobs.py `_resolve_dependency`) — the whole point of waiting is to see what
    landed while it waited.
  * `build_context` admits an answer only when it is COMPLETE and committed
    strictly before the cutoff (`completed_at < context_cutoff`).

Invariants asserted (a violation exits non-zero):

  I1  Snapshot isolation. No job's context contains an answer whose true commit
      instant is >= the job's cutoff. (No reading the future.)
  I2  No torn reads. Every answer a job reads is a fully committed pair — its
      prompt committed before the cutoff too.
  I3  Wait safety. Every wait edge points strictly backwards in submission order,
      so the wait-for graph is a DAG and every schedule drains (no deadlock).
  I4  Waiting is effective. A dependent job that waited ends up able to see every
      earlier-submitted answer — blocking then ignoring the result would be a bug.
  I5  Display order. The real `order_for_display` is a total, stable,
      permutation-invariant order with each prompt immediately followed by its
      answer.
  I6  Clock precision. Every commit instant is distinct — the property
      timestamptz(6) exists to guarantee and timestamptz(3) silently broke.

To show the checker has teeth, two mutations reintroduce real bug classes:

  * `coarse` coarsens the observed clock the way `timestamptz(3)` did — distinct
    microsecond instants collapse onto one tick, so the one-microsecond cutoff
    offset rounds away and a dependent job loses the earlier answer it waited for
    (caught by I4/I2). This is the exact bug this project hit and fixed.
  * `late` reads the transcript against the job's own completion instant instead
    of its cutoff (the classic "used completed_at where context_cutoff was
    meant") — so a job admits answers that committed after its snapshot (caught
    by I1, the headline isolation invariant).

main() runs a clean batch (must find zero violations) AND both mutated batches
(each must find at least one) — all three are required to pass, so the suite
fails the moment it stops being able to catch a bug it is supposed to catch.
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.models import Message, Role
from app.ordering import order_for_display

EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _at(tick: int) -> datetime:
    """A virtual tick as a real timestamp — one tick == one microsecond."""
    return EPOCH + timedelta(microseconds=tick)


class Clock:
    """A strictly monotonic tick source: the model of `utcnow()` under a clock
    fine enough that no two calls collide (which is what timestamptz(6) buys)."""

    def __init__(self) -> None:
        self._t = 0

    def tick(self) -> int:
        self._t += 1
        return self._t


@dataclass
class Job:
    idx: int
    dependent: bool  # what dependency detection concluded for this prompt

    # Filled in as the simulation runs. All are TRUE, fine-grained ticks; the
    # checker treats these as ground truth regardless of any clock coarsening.
    prompt_commit: int | None = None  # when the user row committed
    cutoff: int | None = None  # the read snapshot stamped on the job
    commit: int | None = None  # when the answer committed
    phase: str = "new"
    waited_on: list[int] = field(default_factory=list)
    context: list[int] = field(default_factory=list)  # answer idxs this job read


def _observed(tick: int, coarsen: int) -> int:
    """The instant as the *storage layer* sees it. coarsen == 1 is exact
    (timestamptz(6)); a larger value rounds distinct instants together, which is
    what timestamptz(3) did to the one-microsecond cutoff offset."""
    return tick // coarsen


class Violation(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def simulate(rng: random.Random, n_jobs: int, visibility: str) -> list[Job]:
    """Run one randomly-interleaved schedule of `n_jobs` concurrent prompts.

    Every job is available from the start; the scheduler repeatedly picks a random
    job with an enabled next step and advances it by one step, consuming ticks
    from the shared clock. That is the whole source of interleaving — two runs
    with different RNG draws produce genuinely different global orders, and the
    tick a step lands on depends on everything scheduled before it.
    """
    clock = Clock()
    jobs = [Job(idx=i, dependent=rng.random() < 0.5) for i in range(n_jobs)]

    def submitted(j: Job) -> bool:
        return j.prompt_commit is not None

    def done(j: Job) -> bool:
        return j.phase == "done"

    def earlier_in_flight(j: Job) -> list[Job]:
        # Mirrors jobs.py `_earlier_in_flight`: strictly-earlier-submitted jobs
        # that have not reached a terminal state yet.
        return [
            k
            for k in jobs
            if submitted(k)
            and k.prompt_commit < j.prompt_commit  # type: ignore[operator]
            and not done(k)
        ]

    def visible(a: Job, j: Job) -> bool:
        # build_context: an answer is visible when it committed strictly before
        # the job's cutoff. `exact` is production. The two mutations reintroduce
        # real bug classes so the checker can prove it still catches them.
        if visibility == "coarse":
            coarsen = 32  # timestamptz(3): distinct instants collapse together
            return _observed(a.commit, coarsen) < _observed(j.cutoff, coarsen)  # type: ignore[arg-type]
        if visibility == "late":
            return a.commit < j.commit  # BUG: reads against completion, not cutoff
        return a.commit < j.cutoff  # type: ignore[operator]

    def snapshot(j: Job) -> None:
        for a in jobs:
            if a.idx == j.idx or a.commit is None:
                continue
            if visible(a, j):
                j.context.append(a.idx)

    def enabled_steps(j: Job) -> list[str]:
        if j.phase == "new":
            return ["submit"]
        if j.phase == "submitted":
            return ["resolve"]
        if j.phase == "waiting":
            # A dependent job may take its snapshot only once nothing earlier is
            # still in flight — the gate that makes waiting terminate.
            return ["snapshot"] if not earlier_in_flight(j) else []
        if j.phase == "ready":
            return ["commit"]
        return []

    def step(j: Job, action: str) -> None:
        if action == "submit":
            j.prompt_commit = clock.tick()  # user row commits at submit
            j.cutoff = clock.tick()  # cutoff is one tick later (the +1us offset)
            j.phase = "submitted"
        elif action == "resolve":
            if j.dependent:
                j.phase = "waiting"
            else:
                j.phase = "ready"  # independent: keep the submit-time cutoff
        elif action == "snapshot":
            j.waited_on = [k.idx for k in jobs if submitted(k) and k.prompt_commit < j.prompt_commit]  # type: ignore[operator]
            j.cutoff = clock.tick()  # re-stamp: see what landed while we waited
            j.phase = "ready"
        elif action == "commit":
            j.commit = clock.tick()
            snapshot(j)  # read the transcript once, at the cutoff
            j.phase = "done"

    guard = 0
    limit = n_jobs * 8 + 16
    while not all(done(j) for j in jobs):
        ready = [(j, a) for j in jobs for a in enabled_steps(j)]
        if not ready:
            # No job can move but not all are done: a wait cycle. The strictly
            # backwards wait rule is supposed to make this impossible (I3).
            stuck = [j.idx for j in jobs if not done(j)]
            raise Violation("I3", f"deadlock: jobs {stuck} cannot proceed")
        guard += 1
        if guard > limit:
            raise Violation("I3", "schedule failed to drain within its step budget")
        job, action = rng.choice(ready)
        step(job, action)

    return jobs


def check(jobs: list[Job]) -> None:
    """Replay one finished schedule and assert every invariant. Raises Violation."""
    by_idx = {j.idx: j for j in jobs}

    # I6 — every commit instant is distinct (the timestamptz(6) guarantee).
    commits = [j.commit for j in jobs]
    if len(set(commits)) != len(commits):
        raise Violation("I6", "two answers committed at the same instant")

    for j in jobs:
        for a_idx in j.context:
            a = by_idx[a_idx]
            # I1 — snapshot isolation: nothing committed at or after the cutoff.
            if a.commit >= j.cutoff:  # type: ignore[operator]
                raise Violation(
                    "I1",
                    f"job {j.idx} (cutoff {j.cutoff}) read answer {a_idx} "
                    f"committed at {a.commit} — at or after its own snapshot",
                )
            # I2 — no torn read: the answer's prompt committed before the cutoff too.
            if a.prompt_commit >= j.cutoff:  # type: ignore[operator]
                raise Violation(
                    "I2",
                    f"job {j.idx} read answer {a_idx} whose prompt was not yet committed",
                )

        # I3 — every wait points strictly backwards in submission order.
        for w in j.waited_on:
            if by_idx[w].prompt_commit >= j.prompt_commit:  # type: ignore[operator]
                raise Violation("I3", f"job {j.idx} waited on non-earlier job {w}")

        # I4 — a dependent job that waited can see every earlier-submitted answer.
        if j.dependent:
            for k in jobs:
                if k.idx != j.idx and k.prompt_commit < j.prompt_commit:  # type: ignore[operator]
                    if k.idx not in j.context:
                        raise Violation(
                            "I4",
                            f"dependent job {j.idx} waited but still missed "
                            f"earlier answer {k.idx}",
                        )

    _check_display_order(jobs)


def _rows_for(jobs: list[Job]) -> list[Message]:
    """Build the user/assistant rows a finished schedule would persist."""
    rows: list[Message] = []
    for j in jobs:
        rows.append(
            Message(id=f"u{j.idx}", role=Role.USER, submitted_at=_at(j.prompt_commit))  # type: ignore[arg-type]
        )
        rows.append(
            Message(
                id=f"a{j.idx}",
                role=Role.ASSISTANT,
                submitted_at=_at(j.cutoff),  # type: ignore[arg-type]
                prompt_message_id=f"u{j.idx}",
            )
        )
    return rows


def _check_display_order(jobs: list[Job]) -> None:
    """I5 — exercise the REAL order_for_display, not a reimplementation.

    Permuting the input must not change the output (stable + total), and every
    assistant must sit immediately after its own prompt.
    """
    rows = _rows_for(jobs)
    canonical = [m.id for m in order_for_display(rows)]

    rng = random.Random(len(rows))
    for _ in range(5):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        got = [m.id for m in order_for_display(shuffled)]
        if got != canonical:
            raise Violation("I5", f"display order not permutation-invariant: {got} != {canonical}")

    for i in range(0, len(canonical), 2):
        prompt, answer = canonical[i], canonical[i + 1]
        if not (prompt.startswith("u") and answer == "a" + prompt[1:]):
            raise Violation("I5", f"prompt/answer not adjacent at {prompt},{answer}")


@dataclass
class Report:
    runs: int = 0
    jobs: int = 0
    dependent: int = 0
    reads: int = 0
    max_concurrency: int = 0
    violations: list[str] = field(default_factory=list)


def _concurrency(jobs: list[Job]) -> int:
    """Peak number of jobs simultaneously between submit and commit."""
    events: list[tuple[int, int]] = []
    for j in jobs:
        events.append((j.prompt_commit, +1))  # type: ignore[arg-type]
        events.append((j.commit, -1))  # type: ignore[arg-type]
    events.sort()
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def run_batch(runs: int, seed: int, max_jobs: int, visibility: str) -> Report:
    rng = random.Random(seed)
    rep = Report()
    for _ in range(runs):
        n = rng.randint(1, max_jobs)
        try:
            jobs = simulate(rng, n, visibility)
            check(jobs)
        except Violation as v:
            rep.violations.append(str(v))
            continue
        rep.runs += 1
        rep.jobs += n
        rep.dependent += sum(1 for j in jobs if j.dependent)
        rep.reads += sum(len(j.context) for j in jobs)
        rep.max_concurrency = max(rep.max_concurrency, _concurrency(jobs))
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description="Randomised snapshot-isolation checker")
    ap.add_argument("--runs", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-jobs", type=int, default=8)
    ap.add_argument(
        "--mutate",
        choices=["none", "coarse", "late"],
        default="none",
        help="run a single mutated batch and report what the checker catches",
    )
    args = ap.parse_args()

    if args.mutate != "none":
        rep = run_batch(args.runs, args.seed, args.max_jobs, args.mutate)
        print(f"mutation '{args.mutate}': {len(rep.violations)} violation(s) over {args.runs} runs")
        for v in rep.violations[:5]:
            print("  -", v)
        return 0

    # 1) The real thing: a clean, exact clock must never violate an invariant.
    clean = run_batch(args.runs, args.seed, args.max_jobs, "exact")
    print("snapshot-isolation checker")
    print(f"  runs                {clean.runs}/{args.runs}")
    print(f"  jobs simulated      {clean.jobs}")
    print(f"  dependent jobs      {clean.dependent}")
    print(f"  context reads       {clean.reads} (every one isolation-checked)")
    print(f"  peak concurrency    {clean.max_concurrency}")
    print(f"  invariants          I1 I2 I3 I4 I5 I6")
    print(f"  violations          {len(clean.violations)}")

    # 2) The teeth: each mutation reintroduces a real bug class that MUST be
    #    caught. If any goes uncaught the checker has gone blind, and the suite
    #    fails even though the clean run was perfect.
    coarse = run_batch(args.runs, args.seed, args.max_jobs, "coarse")
    late = run_batch(args.runs, args.seed, args.max_jobs, "late")
    print()
    print("mutation self-tests (each must be caught)")
    print(f"  coarse clock  (timestamptz(3) collision)   {len(coarse.violations)} caught")
    if coarse.violations:
        print(f"                                             e.g. {coarse.violations[0]}")
    print(f"  late read     (completed_at vs cutoff)     {len(late.violations)} caught")
    if late.violations:
        print(f"                                             e.g. {late.violations[0]}")

    ok = not clean.violations and coarse.violations and late.violations
    print()
    print("PASS" if ok else "FAIL")
    if clean.violations:
        print(f"  clean run produced {len(clean.violations)} violation(s):")
        for v in clean.violations[:5]:
            print("   -", v)
    if not coarse.violations:
        print("  coarse mutation produced no violations — checker is blind to it")
    if not late.violations:
        print("  late mutation produced no violations — checker is blind to it")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
