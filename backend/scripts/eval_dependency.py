"""Score the dependency heuristic against the hand-labelled set.

    docker compose exec backend python -m scripts.eval_dependency

UNSURE is not counted as an error. Deferring is the heuristic working as
designed — those cases are what Stage 7's classifier exists for — so they are
reported separately as coverage rather than folded into accuracy. A heuristic
that answered everything confidently and got a fifth of them wrong would be
worse than one that answers less and knows when it doesn't know.
"""

import json

from app.dependency import Verdict, evaluate

from ._paths import case_file


def main() -> int:
    data = json.loads(case_file("dependency_cases.json").read_text(encoding="utf-8"))
    cases = data["cases"]

    decided = wrong = deferred = 0
    misses: list[tuple[str, str, str, str]] = []

    for case in cases:
        result = evaluate(case["prompt"])
        if result.verdict == Verdict.UNSURE:
            deferred += 1
            continue
        decided += 1
        if result.verdict != case["label"]:
            wrong += 1
            misses.append((case["prompt"], case["label"], result.verdict, result.reason))

    total = len(cases)
    correct = decided - wrong

    print(f"cases            {total}")
    print(f"decided          {decided}  ({decided / total:.0%} of set)")
    print(f"deferred (unsure){deferred:>3}  -> Stage 7 classifier")
    print(f"correct          {correct}/{decided}  ({correct / decided:.1%} of decided)")

    for label in ("dependent", "independent"):
        subset = [c for c in cases if c["label"] == label]
        got = [evaluate(c["prompt"]).verdict for c in subset]
        hit = sum(1 for g in got if g == label)
        unk = sum(1 for g in got if g == Verdict.UNSURE)
        print(f"  {label:12} {hit}/{len(subset)} correct, {unk} deferred")

    if misses:
        print("\nmisclassified:")
        for prompt, want, got, why in misses:
            print(f"  want {want:11} got {got:11} | {prompt}")
            print(f"       reason: {why}")

    return 0 if wrong == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
