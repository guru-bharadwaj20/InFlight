"""Graceful shutdown: drain in-flight generations before the process exits.

A rolling deploy sends SIGTERM and moves on. If the worker just dies, every
answer it was streaming is cut off mid-sentence. Draining makes shutdown orderly:

  1. Flip to *draining* — the readiness probe starts returning 503, so a load
     balancer takes this worker out of rotation and routes new prompts elsewhere.
  2. Wait for the jobs already running to finish (up to a timeout), so their
     answers commit and their clients see a clean `done`.
  3. Cancel anything still going past the timeout — a cancelled job still settles
     terminally and keeps its partial text, so nothing is left half-written.

The readiness/liveness split is the standard one: liveness asks "is the process
up?" (restart it if not), readiness asks "should it get traffic right now?" —
which is false both while dependencies are unreachable and while draining.
"""

from __future__ import annotations

import asyncio
import logging

from . import jobs

logger = logging.getLogger(__name__)

_draining = False


def is_draining() -> bool:
    return _draining


async def drain(timeout: float) -> None:
    """Stop taking traffic, then let running jobs finish before killing stragglers."""
    global _draining
    _draining = True

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while jobs._running and loop.time() < deadline:
        logger.info("draining: %d job(s) still in flight", len(jobs._running))
        await asyncio.sleep(0.2)

    if jobs._running:
        # Past the deadline: cancel what remains. A cancelled job commits its
        # partial answer, so this is orderly, not a hard kill.
        logger.warning("drain timed out with %d job(s) left; cancelling", len(jobs._running))
        for task in list(jobs._running.values()):
            task.cancel()
        await asyncio.gather(*jobs._running.values(), return_exceptions=True)
