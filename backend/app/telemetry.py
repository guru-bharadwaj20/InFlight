"""Observability for the job lifecycle: Prometheus metrics and per-job traces.

A project whose entire pitch is concurrency should let you *see* the concurrency.
This module makes each generation's lifecycle — submit, resolve dependency,
optionally wait, snapshot, stream, commit — measurable two ways:

  * `/metrics` — Prometheus text: counters and latency histograms aggregated
    across every job. Scrape it, or just `curl` it.
  * `/traces`  — the last N job traces as JSON, each a list of timed spans. This
    is the "open the trace of a request and walk it" view; in an interview it
    turns "answers overlap" from a claim into something on screen.

Both are in-process. Metrics are per-worker (the standard Prometheus model — a
scraper aggregates across replicas), and traces live in a bounded ring buffer, so
neither grows without limit or needs an external collector to be useful.

Traces record ids, timings, the dependency verdict, the model, and token counts —
never prompt or answer text, so exposing them leaks nothing about a conversation.
"""

from __future__ import annotations

import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Iterator

from prometheus_client import Counter, Gauge, Histogram

# --- Metrics ---------------------------------------------------------------

PROMPTS_SUBMITTED = Counter(
    "inflight_prompts_submitted_total", "Prompts accepted by POST /messages"
)
JOBS_TOTAL = Counter(
    "inflight_jobs_total", "Generation jobs by terminal outcome", ["outcome"]
)
DEPENDENCY_VERDICT = Counter(
    "inflight_dependency_verdict_total",
    "Final dependency verdict per job, by how it was decided",
    ["verdict", "source"],
)
CLASSIFIER_CALLS = Counter(
    "inflight_classifier_calls_total",
    "Calls to the dependency classifier (the unsure cases only)",
)
SPECULATIONS = Counter(
    "inflight_speculations_total",
    "Dependent jobs that proceeded without waiting (speculative execution)",
)
SPECULATION_OUTCOME = Counter(
    "inflight_speculation_outcome_total",
    "How a speculation resolved: kept (no conflict) or rolled back (auto-rerun)",
    ["outcome"],
)
JOBS_ACTIVE = Gauge(
    "inflight_jobs_active", "Generation jobs currently running in this worker"
)
PROVIDER_RETRIES = Counter(
    "inflight_provider_retries_total", "Transient provider failures retried"
)
CIRCUIT_TRIPS = Counter(
    "inflight_circuit_trips_total", "Times the provider circuit breaker opened"
)
CIRCUIT_STATE = Gauge(
    "inflight_circuit_state", "Provider circuit: 0 closed, 1 half-open, 2 open"
)
SCHEDULER_WAITING = Gauge(
    "inflight_scheduler_waiting", "Jobs queued for a generation slot (backpressure)"
)
SLOT_WAIT_SECONDS = Histogram(
    "inflight_slot_wait_seconds",
    "Time a job waited for a fair generation slot",
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
EVENT_APPEND_FAILURES = Counter(
    "inflight_event_append_failures_total",
    "Audit-log events that failed to append (swallowed so the job itself never fails)",
    ["event_type"],
)

# Buckets chosen for the quantities this system actually produces: sub-second
# TTFT, single-digit-second generations, and dependency waits up to the cap.
WAIT_SECONDS = Histogram(
    "inflight_wait_seconds",
    "Time a dependent job spent blocked on earlier jobs",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
TTFT_SECONDS = Histogram(
    "inflight_ttft_seconds",
    "Time from generation start to first streamed token",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
GENERATION_SECONDS = Histogram(
    "inflight_generation_seconds",
    "Time spent streaming a completion",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
JOB_SECONDS = Histogram(
    "inflight_job_seconds",
    "Total job lifetime, from run start to terminal state",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 150),
)


def record_verdict(verdict: str | None, source: str | None) -> None:
    DEPENDENCY_VERDICT.labels(verdict=verdict or "none", source=source or "none").inc()


# --- Traces ----------------------------------------------------------------


@dataclass
class Span:
    name: str
    start_ms: float  # relative to the trace's start
    dur_ms: float


@dataclass
class Trace:
    job_id: str
    conversation_id: str
    model: str | None
    started_at: float = field(default_factory=time.time)
    _t0: float = field(default_factory=time.perf_counter)
    spans: list[Span] = field(default_factory=list)
    verdict: str | None = None
    source: str | None = None
    outcome: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_ms: float | None = None

    def _now_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        start = self._now_ms()
        t = time.perf_counter()
        try:
            yield
        finally:
            self.spans.append(
                Span(name=name, start_ms=round(start, 2), dur_ms=round((time.perf_counter() - t) * 1000, 2))
            )

    def note(self, verdict: str | None, source: str | None) -> None:
        self.verdict, self.source = verdict, source

    def mark_ttft(self) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = round(self._now_ms(), 2)

    def finish(
        self,
        outcome: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        self.outcome = outcome
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        JOB_SECONDS.observe(self._now_ms() / 1000)

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "conversation_id": self.conversation_id,
            "model": self.model,
            "started_at": self.started_at,
            "verdict": self.verdict,
            "source": self.source,
            "outcome": self.outcome,
            "ttft_ms": self.ttft_ms,
            "total_ms": round(self._now_ms(), 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "spans": [{"name": s.name, "start_ms": s.start_ms, "dur_ms": s.dur_ms} for s in self.spans],
        }


_TRACES: deque[Trace] = deque(maxlen=200)
_LOCK = Lock()


def begin_trace(job_id: str, conversation_id: str, model: str | None) -> Trace:
    trace = Trace(job_id=job_id, conversation_id=conversation_id, model=model)
    with _LOCK:
        _TRACES.append(trace)
    return trace


def recent_traces(limit: int = 50) -> list[dict]:
    with _LOCK:
        traces = list(_TRACES)[-limit:]
    # Newest first — an interviewer wants the request they just fired at the top.
    return [t.as_dict() for t in reversed(traces)]
