"""The rate table, served so the client can price usage it already has.

Message rows already carry their own token counts, so the client can total a
conversation without asking the server to. All it is missing is the rates — hand
those over once and the dashboard stays live off the same frames that drive the
bubbles, with no polling.
"""

from fastapi import APIRouter

from .. import pricing
from ..schemas import PricingOut

router = APIRouter(tags=["usage"])


@router.get("/pricing", response_model=PricingOut)
async def get_pricing() -> PricingOut:
    return PricingOut(**pricing.table())
