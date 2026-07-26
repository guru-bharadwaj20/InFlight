"""Fire N prompts at randomised intervals and measure what concurrency costs.

    docker compose exec backend python -m scripts.load_test --jobs 12
    docker compose exec backend python -m scripts.load_test --jobs 24 --max-gap 0.4

Run this with USE_FAKE_LLM=true. Against a real provider the numbers measure
that provider's queueing and your rate limit, not this system's scheduling — and
it would cost real money to learn that.

The claim under test is that submission never blocks and that jobs genuinely
overlap. Two things falsify it: submit latency that grows with the number
already in flight, and a peak concurrency of one.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
import uuid

import httpx
import websockets

BASE = "http://localhost:8000"

TOPICS = [
    "how DNS resolution works", "the causes of the 1929 crash",
    "how a refrigerator works", "the rules of cricket",
    "how vaccines train the immune system", "the history of the printing press",
    "how bridges handle expansion", "why the sky is blue",
    "how sourdough starters work", "the geology of volcanoes",
    "how noise-cancelling headphones work", "the origins of jazz",
]


def summarise(name: str, values: list[float], unit: str = "ms") -> str:
    if not values:
        return f"  {name:24} —"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return (
        f"  {name:24} min {min(ordered):7.0f}  median {statistics.median(ordered):7.0f}  "
        f"p95 {p95:7.0f}  max {max(ordered):7.0f} {unit}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--max-gap", type=float, default=0.25,
                        help="upper bound on the random pause between submissions, seconds")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    random.seed(args.seed)

    async with httpx.AsyncClient(base_url=BASE, timeout=300) as http:
        health = (await http.get("/health")).json()

        # Every route but /health and /auth requires a bearer token, so sign up
        # a fresh throwaway account for this run rather than hardcoding
        # credentials that would collide across runs.
        signup = await http.post(
            "/auth/signup",
            json={
                "name": "Load Test",
                "email": f"load-test-{uuid.uuid4().hex[:12]}@example.com",
                "password": "load-test-password",
            },
        )
        signup.raise_for_status()
        token = signup.json()["token"]
        http.headers["Authorization"] = f"Bearer {token}"

        conv = await http.post("/conversations", json={"title": "load test"})
        conv.raise_for_status()
        cid = conv.json()["id"]

        print(f"=== load test — {args.jobs} prompts, gaps up to {args.max_gap:.2f}s ===")
        print(f"model {health['generation_model']}\n")

        submit_ms: list[float] = []
        accepted: dict[str, int] = {}
        rejected = 0
        started_at: dict[str, float] = {}

        async with websockets.connect(
            f"ws://localhost:8000/ws/conversations/{cid}?token={token}"
        ) as ws:
            t0 = time.perf_counter()

            async def submit_all() -> None:
                nonlocal rejected
                for i in range(args.jobs):
                    prompt = f"Explain {random.choice(TOPICS)} in detail, step by step."
                    sent = time.perf_counter()
                    r = await http.post(f"/conversations/{cid}/messages",
                                        json={"content": prompt})
                    submit_ms.append((time.perf_counter() - sent) * 1000)
                    if r.status_code == 202:
                        job = r.json()["assistant_message"]["id"]
                        accepted[job] = i
                        started_at[job] = sent
                    else:
                        rejected += 1
                    await asyncio.sleep(random.uniform(0, args.max_gap))

            submitter = asyncio.create_task(submit_all())

            ttft: dict[str, float] = {}
            done: dict[str, str] = {}
            live: set[str] = set()
            peak = 0

            while not submitter.done() or len(done) < len(accepted):
                try:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                except asyncio.TimeoutError:
                    print("!! timed out waiting for frames")
                    break
                job = frame["job_id"]
                if frame["type"] == "status":
                    live.add(job)
                    peak = max(peak, len(live))
                elif frame["type"] == "chunk" and job not in ttft:
                    ttft[job] = (time.perf_counter() - started_at.get(job, t0)) * 1000
                elif frame["type"] in ("done", "error"):
                    live.discard(job)
                    done[job] = frame.get("status", "?")

            await submitter

        wall = time.perf_counter() - t0

    completed = sum(1 for s in done.values() if s == "complete")
    print(f"submitted        {args.jobs}   accepted {len(accepted)}   rejected(429) {rejected}")
    print(f"completed        {completed}/{len(accepted)}")
    print(f"peak concurrent  {peak}")
    print(f"wall clock       {wall:.1f}s\n")

    print(summarise("submit latency", submit_ms))
    print(summarise("time to first token", list(ttft.values())))

    # The claim: submitting stays flat no matter how many are already running.
    # If it climbs with load, the input is blocking after all.
    half = max(1, len(submit_ms) // 2)
    early, late = submit_ms[:half], submit_ms[half:]
    if early and late:
        drift = statistics.median(late) - statistics.median(early)
        print(f"\n  submit latency drift  first half {statistics.median(early):.0f} ms  ->  "
              f"second half {statistics.median(late):.0f} ms  ({drift:+.0f} ms)")
        print("  " + ("submission does NOT degrade under load"
                      if drift < 150 else "!! submission slows as jobs pile up"))
    print(f"  peak concurrency {peak} " +
          ("— jobs genuinely overlap" if peak > 1 else "!! jobs ran one at a time"))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
