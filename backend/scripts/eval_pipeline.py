"""Score the whole dependency pipeline end to end, the way it actually runs.

    docker compose exec backend python -m scripts.eval_pipeline
    docker compose exec backend python -m scripts.eval_pipeline --heuristic-only

Every prompt goes through the real path — heuristic first, classifier only for
what it defers — so the number describes the system rather than one component.
`eval_dependency` and `eval_classifier` remain for looking at each stage alone.

Reported per class, not as bare accuracy. The two errors have very different
costs: a missed dependency ships a wrong answer, while a false dependency only
costs a wait. One accuracy figure hides which one you are making.
"""

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path

from app.dependency import Verdict, evaluate
from app.llm import classify_dependency_strict

CASES = Path(__file__).resolve().parents[1] / "eval" / "dependency_cases.json"

# Each case carries its own plausible predecessor. Scoring a dependent prompt
# against an unrelated in-flight turn measures the harness, not the classifier:
# "refactor this function" genuinely does not depend on a paging explainer.
def in_flight(case: dict) -> list[str]:
    return [case["context"]]


def rate(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:6.1%}" if denominator else "     —"


# Firing every deferred case at once rate-limits the harness against itself, so
# cases drop out as "provider errors" that are really self-inflicted. The run is
# not a latency benchmark, so bounding it costs nothing and makes the score
# reproducible.
CONCURRENCY = 2

# A rate-limit rejection is not a judgement, and dropping those cases leaves the
# score computed over a shifting subset — a different two each run. Retrying the
# transient ones is what makes the tally reproducible. Only 429s are retried;
# a genuine error still surfaces rather than being papered over.
RETRIES = 4


async def classify_with_retry(case: dict) -> bool:
    delay = 4.0
    for attempt in range(RETRIES):
        try:
            return await classify_dependency_strict(case["prompt"], in_flight(case))
        except Exception as exc:
            transient = "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)
            if not transient or attempt == RETRIES - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


async def resolve(
    case: dict, heuristic_only: bool, gate: asyncio.Semaphore
) -> tuple[str, str, str | None]:
    """Return (verdict, source, error) exactly as the running system would."""
    detection = evaluate(case["prompt"])
    if detection.verdict != Verdict.UNSURE:
        return detection.verdict, "heuristic", None
    if heuristic_only:
        return Verdict.UNSURE, "heuristic", None
    async with gate:
        try:
            depends = await classify_with_retry(case)
        except Exception as exc:
            return Verdict.UNSURE, "classifier", type(exc).__name__
    return (Verdict.DEPENDENT if depends else Verdict.INDEPENDENT), "classifier", None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--heuristic-only", action="store_true",
                        help="skip the classifier; report what the free stage alone achieves")
    args = parser.parse_args()

    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    gate = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(
        *[resolve(c, args.heuristic_only, gate) for c in cases]
    )

    errors = [(c, e) for c, (_, _, e) in zip(cases, results) if e]
    scored = [(c, v, s) for c, (v, s, e) in zip(cases, results) if not e]

    # counts[actual][predicted]
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_source: dict[str, list[bool]] = defaultdict(list)
    # (correct, decided, total) — kept apart so "never decided" is not shown as
    # "always wrong". A category the heuristic declines to touch is a coverage
    # fact, not an error rate.
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    misses = []

    for case, verdict, source in scored:
        counts[case["label"]][verdict] += 1
        ok = verdict == case["label"]
        stats = by_category[case.get("category", "—")]
        stats[2] += 1
        if verdict != Verdict.UNSURE:
            stats[1] += 1
            stats[0] += int(ok)
            by_source[source].append(ok)
            if not ok:
                misses.append((case, verdict, source))

    total = len(scored)
    decided = sum(
        counts[a][p] for a in counts for p in (Verdict.DEPENDENT, Verdict.INDEPENDENT)
    )
    correct = counts["dependent"]["dependent"] + counts["independent"]["independent"]
    undecided = sum(counts[a][Verdict.UNSURE] for a in counts)

    mode = "heuristic only" if args.heuristic_only else "heuristic + classifier"
    print(f"=== dependency pipeline — {mode} ===")
    print(f"cases scored     {total}" + (f"   ({len(errors)} excluded: provider errors)" if errors else ""))
    print(f"decided          {decided}   ({rate(decided, total)} coverage)")
    print(f"undecided        {undecided}")
    print(f"accuracy         {correct}/{decided}  ({rate(correct, decided)} of decided)")

    print("\nconfusion (rows = truth)")
    print(f"{'':13}{'dependent':>11}{'independent':>13}{'unsure':>9}")
    for actual in ("dependent", "independent"):
        row = counts[actual]
        print(f"  {actual:11}{row['dependent']:>11}{row['independent']:>13}{row['unsure']:>9}")

    print("\nper class")
    for label in ("dependent", "independent"):
        tp = counts[label][label]
        other = "independent" if label == "dependent" else "dependent"
        fp = counts[other][label]
        fn = counts[label][other]
        print(f"  {label:12} precision {rate(tp, tp + fp)}   recall {rate(tp, tp + fn)}")

    print("\nby stage")
    for source, oks in sorted(by_source.items()):
        print(f"  {source:12} {sum(oks)}/{len(oks)}  {rate(sum(oks), len(oks))}")

    print("\nby category            correct/decided   coverage")
    for category, (ok, dec, tot) in sorted(
        by_category.items(), key=lambda kv: (kv[1][1] / kv[1][2], kv[0])
    ):
        flag = "  (all deferred)" if dec == 0 else ""
        print(f"  {category:20} {ok:>4}/{dec:<9} {rate(dec, tot)}{flag}")

    # The asymmetry that matters, called out rather than left to be derived.
    missed = counts["dependent"]["independent"]
    overheld = counts["independent"]["dependent"]
    print(f"\nmissed dependencies (ship a wrong answer)  {missed}")
    print(f"needless waits      (cost only latency)    {overheld}")

    if misses:
        print("\nmisclassified")
        for case, verdict, source in misses:
            print(f"  [{case.get('id', '?')}] want {case['label']:11} got {verdict:11} via {source}")
            print(f"        {case['prompt']}")

    if errors:
        print("\nprovider errors (excluded)")
        for case, kind in errors[:5]:
            print(f"  {kind}: {case['prompt'][:56]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
