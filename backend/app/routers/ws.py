"""One WebSocket per client, multiplexed by job_id.

Every frame names the job it belongs to, so the client routes frames to bubbles
by id rather than by arrival order. In Stage 2 that is invisible — there is only
ever one job in flight — but it is the property Stage 3 leans on entirely once
several jobs stream into the same socket at once.
"""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import redis_client

router = APIRouter(tags=["ws"])


async def _replay_active_jobs(websocket: WebSocket, conversation_id: str) -> None:
    """Catch a reconnecting client up on jobs that are already streaming.

    Sent after the subscription is live, never before: a job that starts in the
    gap would otherwise have its opening chunks published to nobody. The cost of
    that ordering is that the replayed text may overlap the first frames off the
    channel, which is what the `seq` is for — the client drops any chunk at or
    below the sequence number it replayed.
    """
    for job_id in await redis_client.active_jobs(conversation_id):
        state, buffer = await redis_client.job_snapshot(job_id)
        await websocket.send_json(
            {
                "job_id": job_id,
                "type": "resume",
                "status": state.get("status") or "pending",
                "text": buffer,
                "seq": int(state.get("seq") or 0),
            }
        )


async def _forward_frames(websocket: WebSocket, conversation_id: str) -> None:
    async with redis_client.subscribe(conversation_id) as pubsub:
        await _replay_active_jobs(websocket, conversation_id)
        async for frame in redis_client.frames(pubsub):
            await websocket.send_json(frame)


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    """Read and discard client frames purely to notice when the socket closes.

    Without a pending receive, a disconnect goes unobserved until the next send
    fails — which for an idle conversation may be never, leaking the Redis
    subscription behind it.
    """
    while True:
        await websocket.receive_text()


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_socket(websocket: WebSocket, conversation_id: str) -> None:
    await websocket.accept()

    tasks = [
        asyncio.create_task(_forward_frames(websocket, conversation_id)),
        asyncio.create_task(_watch_for_disconnect(websocket)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        # Surface a genuine failure in the forwarding task rather than closing
        # the socket as if the client had simply gone away.
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
