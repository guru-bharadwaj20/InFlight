# Non-Blocking Concurrent LLM Chat

A chat interface where the input box is never locked. You can fire a follow-up
prompt while a previous one is still generating; both run concurrently against a
shared, evolving conversation history, and each response is reconciled back into
that history using a snapshot-based (MVCC-like) concurrency model rather than a
hard queue.

This is a systems/concurrency project wearing an AI costume. The interesting part
is not the model call — it is deciding what "the conversation so far" means when
two answers are in flight at once.

**Status: Stage 2 of 12 complete** — a working streaming chat, deliberately kept
sequential by a client-side input lock. Stage 3 deletes that lock.

---

## The idea in one column

Concurrency control lives in the `messages` table, which stores *jobs*, not just
chat bubbles. Three timestamps do the work:

| Column | Meaning |
| --- | --- |
| `submitted_at` | When the user hit send. Controls **display** order — the conversation reads top-to-bottom the way the user experienced it. |
| `completed_at` | When generation finished. Controls **commit** order — a row only becomes visible as context once it has committed. |
| `context_cutoff` | The read snapshot stamped on the job when it started. It may only read messages that committed strictly before this instant. |

With one prompt in flight these three collapse into the same ordering, which is
why ordinary chat apps never need to distinguish them. With two in flight they
come apart, and that gap is the entire problem this project is about.

`GET /conversations/{id}/context` makes the read side inspectable: it returns
exactly what a job stamped at a given instant would be allowed to see. A prompt
submitted earlier but still streaming is correctly absent from it.

---

## Stack

- **Frontend** — Next.js 14 (App Router), Tailwind, Framer Motion
- **Backend** — FastAPI + `asyncio`
- **Realtime** — one WebSocket per client, multiplexed by `job_id`
- **State / pub-sub** — Redis (job status + streaming chunk fan-out)
- **Persistence** — Prisma + Postgres
- **Models** — Gemini: `gemini-3.6-flash` for generation, `gemini-3.5-flash-lite`
  for the Stage 7 dependency classifier

No GPU, no self-hosted inference, no training. Every "smart" feature later in the
plan is a heuristic, a cheap classifier call, or a UI affordance.

### Who owns the schema

`prisma/schema.prisma` is the single source of truth for the database, and Prisma
owns migrations. The Python backend reads and writes those same tables through
SQLAlchemy rather than a generated Prisma client, so `backend/app/models.py` is a
hand-maintained mirror. That duplication is only safe if something checks it:

```bash
docker compose exec backend python -m scripts.check_schema
```

Run it after every migration. It compares the mapped tables and columns against
the live database and exits non-zero on drift.

All `DateTime` columns are `timestamptz`. In a system whose correctness rests on
comparing instants written by different concurrent tasks, a timestamp without a
zone is a latent bug.

---

## Running it

Requirements: Docker, and Node 18+ on the host if you want to run migrations
without the containerized runner.

```bash
cp .env.example .env          # defaults work as-is for local development
docker compose up -d --build  # postgres, redis, backend, frontend
npm install                   # Prisma CLI (host-side)
npx prisma migrate dev        # create/apply migrations
```

| Service | URL |
| --- | --- |
| Frontend | http://localhost:3000 |
| Backend | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |

If you would rather not install Node locally, run migrations through the
one-shot container instead:

```bash
docker compose --profile tools run --rm migrate
```

It runs on Debian rather than Alpine — Prisma's schema engine is a native binary
that needs glibc and OpenSSL, and fails to start on musl. The first run installs
OpenSSL and downloads the Prisma CLI, so give it a few minutes; the host-side
`npx prisma migrate dev` is much faster for day-to-day work.

Set `GEMINI_API_KEY` in `.env` before Stage 2's chat will generate anything — get
one from [AI Studio](https://aistudio.google.com/apikey). Without it the app
still runs, and prompts fail with a visible `GEMINI_API_KEY is not set` on the
bubble rather than silently hanging.

### Ports already in use

`POSTGRES_PORT`, `REDIS_PORT`, `BACKEND_PORT`, and `FRONTEND_PORT` in `.env`
remap the published ports. Note that `DATABASE_URL` in `.env` is the *host-side*
URL used by the Prisma CLI; the backend container gets its own URL pointing at
the `postgres` service name, set in `docker-compose.yml`.

---

## What Stage 1 gives you

- Postgres, Redis, backend, and frontend come up together under Compose.
- The schema is migrated, with the indexes the later concurrency queries need:
  `(conversation_id, submitted_at)` for display, `(conversation_id, completed_at)`
  for context assembly, `(conversation_id, status)` for finding in-flight jobs.
- A REST surface for creating conversations, reading them in display order, and
  reading a context snapshot.
- Redis key conventions decided in one place before there are several writers —
  `job:{id}`, `conversation:{id}:active`, `channel:conversation:{id}`.

---

## What Stage 2 gives you

A normal, working chat: type, watch tokens arrive, get an answer with its token
counts. The control group. Its value is that it is boring — it is the thing
Stage 3 must not break.

The important detail is *how* it is sequential. `POST /conversations/{id}/messages`
commits the prompt, spawns a background job, and returns `202` immediately — it
never waits for the model. The answer arrives over the WebSocket addressed by the
assistant row's id. **The only thing making this chat one-at-a-time is a
`disabled` attribute on the textarea.** Stage 3 is therefore a deletion, not a
rewrite:

```
POST /messages ──> commit prompt row (completed_at = t)
                   commit assistant row (context_cutoff = t + 1µs, status=pending)
                   spawn asyncio task ──────────────┐
                   return 202                        │
                                                     v
                              read snapshot: completed_at < cutoff
                              stream from Gemini
                              ├─> Redis buffer + seq   (replay on reconnect)
                              └─> publish {job_id, type, ...}
                                            │
   WebSocket /ws/conversations/{id} <────────┘
                    │
                    └─> client routes frames to bubbles by job_id
```

Three decisions in there are load-bearing later:

- **The cutoff is derived, not re-read from the clock.** It is the prompt's own
  `completed_at` plus one microsecond. Two `utcnow()` calls in quick succession
  can return the same value on a coarse clock, and `completed_at < cutoff` is a
  strict comparison — a job would then fail to see the very message it is
  answering.
- **The channel is per conversation, not per job.** One socket, and every frame
  names its `job_id`. Subscribing per job would mean re-subscribing on every
  send, and would race against jobs starting in the gap.
- **Chunks are sequenced.** Redis holds a replay buffer and the sequence number
  it covers. A client reconnecting mid-stream replays the buffer and drops any
  chunk at or below that sequence, so refreshing the browser mid-answer doesn't
  duplicate text.

Failures are per job: a missing API key or a provider error marks that one row
`error` with the reason on the bubble, and touches nothing else.

---

## Roadmap

| Stage | What it adds |
| --- | --- |
| 1 ✅ | Foundations and data model |
| 2 ✅ | Baseline single-threaded chat (the control group) |
| 3 | Concurrency plumbing — remove the input lock |
| 4 | History reconciliation UI |
| 5 | Token/cost dashboard |
| 6 | Dependency heuristic |
| 7 | Dependency classifier (cheap model call, ambiguous cases only) |
| 8 | Manual "chain" override |
| 9 | Optimistic fallback + regenerate nudge |
| 10 | Resilience: cancel, reconnect, per-job failure isolation |
| 11 | Evaluation harness |
| 12 | Documentation, demo, writeup |

---

## Known limitations (stated up front, not buried)

Dependency detection in Stages 6–9 is heuristic plus a cheap classifier. It will
sometimes be wrong in both directions, by design. The mitigations are a manual
chain override (Stage 8) and a retrospective regenerate nudge (Stage 9) — the
abort/retry half of the optimistic-concurrency pattern this project implements.

This is applied concurrency control, not novel AI research. MVCC is decades-old
database theory and concurrent async requests are routine; what appears to be
unbuilt is the specific combination — N generations in flight against one shared
linear history, resolved by snapshot isolation rather than by queuing or
branching.
