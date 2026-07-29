"""Score the dependency pipeline against trivial baselines, with precision/recall/F1.

    docker compose exec backend python -m scripts.eval_baselines
    docker compose exec backend python -m scripts.eval_baselines --cases adversarial
    docker compose exec backend python -m scripts.eval_baselines --with-classifier

The base eval reports the pipeline's own numbers. This answers the harder
question an interviewer asks next: *does the pipeline earn its complexity, or
would a one-line rule do as well?* It puts the real thing beside the strategies
you could write without it and reports the same metrics for each.

Treating "dependent" as the positive class (a missed dependency is the costly
error), for every strategy it reports precision, recall, F1, the two domain
errors — missed dependencies (FN) and needless waits (FP) — and the number of
classifier calls the strategy costs. That last column is the point: the pipeline
reaches near-perfect accuracy for a fraction of the calls of classifying
everything.

The classifier-based strategies cost real quota, so they are opt-in
(`--with-classifier`); the free baselines and the heuristic always run.
"""

import argparse
import asyncio
import json
from dataclasses import dataclass

from app.dependency import Verdict, evaluate
from app.llm import classify_dependency_strict

from ._paths import case_file

CASE_FILES = {
    "base": "dependency_cases.json",
    "adversarial": "adversarial_cases.json",
}

DEP, IND = Verdict.DEPENDENT, Verdict.INDEPENDENT


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    calls: int = 0  # classifier calls this strategy spent

    def add(self, predicted: str, actual: str) -> None:
        pos = predicted == DEP
        truth = actual == DEP
        if pos and truth:
            self.tp += 1
        elif pos and not truth:
            self.fp += 1
        elif not pos and truth:
            self.fn += 1
        else:
            self.tn += 1

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        total = self.tp + self.fp + self.fn + self.tn
        return (self.tp + self.tn) / total if total else 0.0


async def classify_retry(prompt: str, context: str) -> bool:
    delay = 4.0
    for attempt in range(4):
        try:
            return await classify_dependency_strict(prompt, [context])
        except Exception as exc:
            transient = "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)
            if not transient or attempt == 3:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


async def score_pipeline(cases: list[dict], gate: asyncio.Semaphore) -> Metrics:
    """The real system: heuristic first, classifier only on what it defers."""
    m = Metrics()

    async def one(case: dict) -> tuple[str, str]:
        det = evaluate(case["prompt"])
        if det.verdict != Verdict.UNSURE:
            return det.verdict, case["label"]
        async with gate:
            depends = await classify_retry(case["prompt"], case["context"])
        return (DEP if depends else IND), case["label"]

    # Count deferrals up front so `calls` is exact even though `one` runs them.
    m.calls = sum(1 for c in cases if evaluate(c["prompt"]).verdict == Verdict.UNSURE)
    for predicted, actual in await asyncio.gather(*[one(c) for c in cases]):
        m.add(predicted, actual)
    return m


async def score_always_classify(cases: list[dict], gate: asyncio.Semaphore) -> Metrics:
    """Skip the heuristic; ask the classifier about every prompt."""
    m = Metrics(calls=len(cases))

    async def one(case: dict) -> tuple[str, str]:
        async with gate:
            depends = await classify_retry(case["prompt"], case["context"])
        return (DEP if depends else IND), case["label"]

    for predicted, actual in await asyncio.gather(*[one(c) for c in cases]):
        m.add(predicted, actual)
    return m


def score_constant(cases: list[dict], predicted: str) -> Metrics:
    m = Metrics()
    for c in cases:
        m.add(predicted, c["label"])
    return m


def score_heuristic(cases: list[dict], fallback: str) -> Metrics:
    """Free stage alone; undecided cases fall back to a fixed policy."""
    m = Metrics()
    for c in cases:
        v = evaluate(c["prompt"]).verdict
        m.add(v if v != Verdict.UNSURE else fallback, c["label"])
    return m


def row(name: str, m: Metrics) -> str:
    return (
        f"  {name:26} {m.precision:6.1%} {m.recall:6.1%} {m.f1:6.1%} "
        f"{m.accuracy:6.1%} {m.fn:>7} {m.fp:>8} {m.calls:>6}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", choices=list(CASE_FILES), default="base")
    ap.add_argument("--with-classifier", action="store_true",
                    help="also run the classifier-based strategies (costs quota)")
    args = ap.parse_args()

    cases = json.loads(
        case_file(CASE_FILES[args.cases]).read_text(encoding="utf-8")
    )["cases"]
    n_dep = sum(1 for c in cases if c["label"] == DEP)
    gate = asyncio.Semaphore(2)

    print(f"=== baselines on '{args.cases}' set ({len(cases)} cases, "
          f"{n_dep} dependent / {len(cases) - n_dep} independent) ===")
    print("  positive class = dependent; FN = missed dependency, FP = needless wait\n")
    print(f"  {'strategy':26} {'prec':>6} {'recall':>6} {'F1':>6} "
          f"{'acc':>6} {'missed':>7} {'needless':>8} {'calls':>6}")

    print(row("always wait (all dep.)", score_constant(cases, DEP)))
    print(row("never wait (all indep.)", score_constant(cases, IND)))
    print(row("heuristic + wait-if-unsure", score_heuristic(cases, DEP)))
    print(row("heuristic + proceed-if-uns.", score_heuristic(cases, IND)))

    if args.with_classifier:
        pipeline = await score_pipeline(cases, gate)
        always = await score_always_classify(cases, gate)
        print(row("full pipeline (heur+clf)", pipeline))
        print(row("always classify", always))
        print()
        print(f"  pipeline reaches F1 {pipeline.f1:.1%} for {pipeline.calls} classifier "
              f"calls vs {always.calls} for always-classify — "
              f"{always.calls - pipeline.calls} calls saved at "
              f"{'equal or better' if pipeline.f1 >= always.f1 else 'lower'} F1")
    else:
        print("\n  (run with --with-classifier to add the pipeline and always-classify "
              "rows — those cost quota)")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
