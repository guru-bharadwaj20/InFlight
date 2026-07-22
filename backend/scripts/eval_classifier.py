"""Score the Stage 7 classifier on the cases the heuristic could not settle.

    docker compose exec backend python -m scripts.eval_classifier

Only the deferred subset is scored, because that is the only place the
classifier ever runs. Measuring it on the full set would flatter it with cases
the heuristic already handles for free.

Costs real tokens on the classifier model. Expect cents, not dollars.
"""

import asyncio
import json
from pathlib import Path

from app.dependency import Verdict, evaluate
from app.llm import classify_dependency_strict

CASES = Path(__file__).resolve().parents[1] / "eval" / "dependency_cases.json"

# Each case carries the predecessor it would realistically follow.


async def main() -> int:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    deferred = [c for c in cases if evaluate(c["prompt"]).verdict == Verdict.UNSURE]

    if not deferred:
        print("no deferred cases — nothing for the classifier to do")
        return 0

    results = await asyncio.gather(
        *[classify_dependency_strict(c["prompt"], [c["context"]]) for c in deferred],
        return_exceptions=True,
    )

    wrong, failed = [], []
    for case, depends in zip(deferred, results):
        if isinstance(depends, BaseException):
            # Counted apart from the score. A provider outage is not a judgement,
            # and averaging the two together reports an error rate as accuracy.
            failed.append((case["prompt"], type(depends).__name__))
            continue
        got = Verdict.DEPENDENT if depends else Verdict.INDEPENDENT
        if got != case["label"]:
            wrong.append((case["prompt"], case["label"], got))

    scored = len(deferred) - len(failed)
    correct = scored - len(wrong)
    print(f"deferred cases   {len(deferred)}")
    if failed:
        print(f"provider errors  {len(failed)}  (excluded from the score)")
        for prompt, kind in failed[:3]:
            print(f"    {kind}: {prompt[:56]}")
    if not scored:
        print("nothing scored — every call failed")
        return 1
    print(f"scored           {scored}")
    print(f"correct          {correct}/{scored}  ({correct / scored:.1%})")

    # Which way it errs matters more than the rate: a false "independent" ships a
    # wrong answer, a false "dependent" only costs a wait.
    false_independent = sum(1 for _, want, got in wrong if want == Verdict.DEPENDENT)
    false_dependent = sum(1 for _, want, got in wrong if want == Verdict.INDEPENDENT)
    print(f"  false independent (costs correctness) {false_independent}")
    print(f"  false dependent   (costs latency)     {false_dependent}")

    for prompt, want, got in wrong:
        print(f"  want {want:11} got {got:11} | {prompt}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
