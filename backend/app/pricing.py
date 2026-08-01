"""Token pricing, held as data rather than code.

Published rates change often enough that baking them into a module guarantees
they will be wrong at some point, and silently. `pricing.json` sits next to this
file with the date and source it was taken from, so updating it is an edit and a
commit rather than a hunt through the codebase.

A model with no entry produces *no* estimate rather than a zero or a guess. An
absent number is obviously absent; a wrong one is not.

This module serves the rates and stops there. Applying them is the client's job:
message rows already carry their own token counts, so the dashboard totals a
conversation from data it holds and stays live off the same frames that fill the
bubbles. There used to be a second, unused implementation of that arithmetic
here (`rate_for`, `cost_usd`) that nothing called -- two copies of a formula, one
of which could drift without anyone noticing because it never ran.
"""

import json
from functools import lru_cache
from pathlib import Path

PRICING_PATH = Path(__file__).parent / "pricing.json"


@lru_cache
def table() -> dict:
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))
