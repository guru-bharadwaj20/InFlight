"""Token pricing, held as data rather than code.

Published rates change often enough that baking them into a module guarantees
they will be wrong at some point, and silently. `pricing.json` sits next to this
file with the date and source it was taken from, so updating it is an edit and a
commit rather than a hunt through the codebase.

A model with no entry produces *no* estimate rather than a zero or a guess. An
absent number is obviously absent; a wrong one is not.
"""

import json
from functools import lru_cache
from pathlib import Path

PRICING_PATH = Path(__file__).parent / "pricing.json"

TOKENS_PER_UNIT = 1_000_000


@lru_cache
def table() -> dict:
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))


def rate_for(model: str | None) -> dict[str, float] | None:
    if not model:
        return None
    return table()["models"].get(model)


def cost_usd(
    model: str | None, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    """Estimated cost of one exchange, or None if the model's rate is unknown."""
    rate = rate_for(model)
    if rate is None:
        return None
    return (
        (prompt_tokens or 0) * rate["input"] + (completion_tokens or 0) * rate["output"]
    ) / TOKENS_PER_UNIT
