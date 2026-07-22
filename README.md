# Non-Blocking Concurrent LLM Chat

A chat interface where the input box is never locked. You can fire a follow-up
prompt while a previous one is still generating; both run concurrently against a
shared, evolving conversation history, and each response is reconciled back into
that history using a snapshot-based (MVCC-like) concurrency model rather than a
hard queue.

This is a systems/concurrency project wearing an AI costume. The interesting part
is not the model call — it is deciding what "the conversation so far" means when
two answers are in flight at once.

**Status: Stage 5 of 12 complete** — several prompts generate at once against one
shared history, each reading its own snapshot; the UI keeps display order and
completion order visibly separate, and reports what it all cost.

Stages 1–5 are a coherent project on their own: non-blocking concurrent chat
with snapshot-based context and a transparency dashboard. Stages 6–9 add the
dependency-awareness layer on top.

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

All `DateTime` columns are `timestamptz(6)`. In a system whose correctness rests
on comparing instants written by different concurrent tasks, a timestamp without
a zone is a latent bug — and so is one without enough precision. The columns
started at `timestamptz(3)`, which silently rounded away the one-microsecond
offset that makes a job's cutoff admit its own prompt, colliding the two
instants. Precision here is load-bearing, not cosmetic.

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

## What Stage 3 gives you

The `disabled` attribute is gone. Fire a second prompt while the first is still
generating and both stream into the same socket at once, each routed to its own
bubble by `job_id`. Measured with the local generator, three prompts fired
back-to-back:

```
3 prompts accepted in 866 ms total (never blocked)
peak simultaneous streaming jobs: 3
chunks from different jobs interleaved: True
submitted order : ['LONG', 'SHORT', 'MID']
completed order : ['SHORT', 'MID', 'LONG']
```

### The bug concurrency exposed: unanswered sibling prompts

User rows commit the instant they are submitted. So the moment a job takes its
snapshot, the conversation may already contain *other* prompts that were fired
concurrently and have no answers yet. Feeding those to the model is actively
wrong — it reads them as part of the question and tries to answer all of them at
once. In testing, three concurrent prompts produced three answers addressed to
the wrong questions.

Visibility alone is therefore not the whole rule. **Context is assembled from
answered pairs**: an exchange enters the transcript only when both halves have
committed, and the job's own prompt is appended last. A prompt still waiting on
its answer is invisible, exactly like the answer itself.

Making that exact needs one thing timestamps cannot give you — which prompt an
answer belongs to, once "the most recent one" is ambiguous. Hence
`prompt_message_id` on the assistant row. Verified: of two prompts fired
simultaneously, neither job's context contains the other's prompt, while a job
started after both settled sees all of it.

### Testing concurrency without a provider

`USE_FAKE_LLM=true` swaps in a deterministic local generator that streams with a
delay, with answer length scaled to prompt length so out-of-order completion is
reproducible. It is off by default and never inferred from a missing key — a
silent fallback to fake answers would be worse than a visible error. It exists
because Stage 11's load test needs streaming that is free and repeatable.

---

## What Stage 4 gives you

Once answers can land out of order, "when you asked" and "when you got an
answer" stop being the same thing, and the UI has to express both without
letting either corrupt the other.

**Position comes from `submitted_at`, never `completed_at`.** The list reads
top-to-bottom in the order you asked, and a bubble's slot is fixed the moment it
is submitted. A fast answer landing before a slow one asked earlier changes that
bubble's height and nothing else — nothing ever moves past anything.

**The unit being ordered is the exchange, not the row.** This is the second bug
concurrency exposed. Prompts submitted close enough together get stamped at the
same instant — `submitted_at` ties are real and were observed in testing — and
sorting rows independently under a tie interleaves two exchanges into prompt,
prompt, answer, answer. So an answer sorts by *its prompt's* timestamp, and the
sort falls through to id and then role to stay total, so React never sees two
rows swap places. `orderMessages` in
[frontend/lib/ordering.ts](frontend/lib/ordering.ts) is a pure function for
exactly this reason; it has unit tests covering the tie case, the late-answer
case, and idempotency. The server applies the same rule in
[backend/app/ordering.py](backend/app/ordering.py) — the client still sorts,
because it holds rows straight from POST responses and frames that the server
has not seen, but the API should not hand back an order that needs repairing.

**Per-bubble state.** Each bubble carries its own `pending → streaming →
complete` chip with a pulsing dot while unsettled, so several in-flight answers
are obvious at a glance. Framer Motion's `layout` animates the height change as
an answer fills in, and pending bubbles reserve a line so the first token grows a
bubble instead of creating one. Autoscroll only follows the tail when you are
already at it — with several answers growing at once, yanking the viewport down
on every chunk would make reading anything above impossible.

---

## What Stage 5 gives you

A live per-conversation token and cost readout: prompt, completion and total
tokens, an estimated spend, and a per-model breakdown, alongside a count of how
many answers are still in flight.

**It is a dashboard, not a gatekeeper.** Nothing in it limits or blocks a
request. The point is honesty about what concurrency costs — firing three
prompts at once is three prompts' worth of tokens, and that should be visible
rather than discovered on a bill.

Two decisions keep the numbers trustworthy:

- **Totals are derived, never fetched.** Message rows already carry their own
  token counts, so the client sums rows it already holds. The panel updates off
  the same frames that drive the bubbles, which means it cannot drift from
  what's on screen and needs no polling. The server only supplies the rate
  table, via `GET /pricing`.
- **An unknown model produces no estimate, not a zero.** Rates live in
  [backend/app/pricing.json](backend/app/pricing.json) with the date and source
  they were taken from — updating them is an edit, not a code change. A model
  with no entry still contributes tokens, but marks the total partial and shows
  `—` for cost. An absent number is obviously absent; a wrong one is not.

`summarize` in [frontend/lib/usage.ts](frontend/lib/usage.ts) is a pure function
with unit tests covering the per-million divisor, in-flight exclusion, unpriced
models, and a missing rate table.

Note that with `USE_FAKE_LLM=true` the "tokens" are word counts from the local
generator, so the cost estimate is meaningless — it exercises the plumbing, not
real spend.

**On a free-tier API key the estimate is also not what you pay** — free tier is
$0, and capped instead by requests per day (20/day for `gemini-3.6-flash` at time
of writing, per model). The dashboard prices usage at standard paid-tier rates
regardless, because that is what the same traffic would cost in production. Worth
knowing before reading the number as a bill.

---

## What Stage 6 gives you

Every prompt is now scored at submit time for whether it depends on an answer
that does not exist yet. Stage 6 only *records* the verdict — Stage 7 is what
starts making jobs wait on it — so the concurrent behaviour is unchanged.

Three verdicts, chosen because the errors are not symmetric:

| Verdict | Action | Cost if wrong |
| --- | --- | --- |
| `dependent` | wait for the predecessor | latency |
| `independent` | fire immediately | **correctness** |
| `unsure` | escalate to the classifier | one cheap model call |

Against 38 hand-labelled prompts in
[eval/dependency_cases.json](eval/dependency_cases.json):

```
decided           27  (71% of set)
deferred (unsure) 11  -> Stage 7 classifier
correct        27/27  (100% of decided)
```

`docker compose exec backend python -m scripts.eval_dependency`

**Deferring is not counted as an error.** Those 11 cases are the ones Stage 7
exists for. A heuristic that answered all 38 confidently and got a fifth wrong
would be worse than one that answers 27 and knows when it doesn't know — 100% is
a statement about coverage-adjusted precision, not about the problem being
solved.

It is keyword and shape matching, not a parser. It has no notion of what a noun
is, so "does this pronoun have an antecedent?" is approximated by "does any
content word appear before it?" — which is why that approximation only ever
*downgrades* a verdict to `unsure`, never promotes one to `independent`. Rules
are scoped to avoid obvious over-reach: "do the same for Rust" is dependent,
while "are these two the same?" defers.

The verdict, its source, and its human-readable reason are all stored on the
row, so the UI can explain a wait rather than just imposing one.

---

## What Stage 7 gives you

The `unsure` verdicts now get one cheap, constrained model call, and a job that
turns out to be dependent actually waits.

```
prompt ─> heuristic ─┬─ dependent ───────────────┐
                     ├─ independent ─> fire now  │
                     └─ unsure ─> classifier ─┬──┴─> wait for earlier jobs
                                              └─────> fire now
```

**The wait lives in the job, not in the request.** `POST /messages` still returns
in ~400 ms even for a dependent prompt; classification and waiting happen inside
the background job. Sending must stay instant even when answering cannot — that
is the entire premise of the project, and putting a classifier call in the submit
path would have quietly undone it.

Three things make the waiting safe:

- **Only ever waits on strictly earlier submissions**, so no cycle can form and
  two mutually dependent prompts cannot deadlock.
- **Re-stamps the cutoff after waiting.** Blocking for an answer and then reading
  a snapshot taken before it would be the worst of both.
- **Bounded.** Past `MAX_DEPENDENCY_WAIT_SECONDS` it proceeds anyway — a slightly
  stale snapshot beats never answering.
- **Fails toward waiting.** A classifier error returns `dependent`, because
  guessing independence ships a wrong answer while guessing dependence costs a
  wait.

Measured on the 11 deferred cases (`python -m scripts.eval_classifier`):

```
correct  10/11  (90.9%)
  false independent (costs correctness) 1
  false dependent   (costs latency)     0
```

Pipeline end to end: **37/38 (97.4%)** — heuristic 27/27 on what it decides,
classifier 10/11 on the rest. The single miss is "Refactor this function to be
tail recursive", called independent when it was dependent. That is the expensive
direction, and it is exactly the case Stage 9's retrospective check exists to
catch. It will not be the only one — see the limitations section.

Verified live: a dependent follow-up ("now do the same for search algorithms")
held until its predecessor landed and then answered about *search*, proving it
read the answer it waited for; two independent prompts still streamed
concurrently.

---

## What Stage 8 gives you

A `↩ chain` affordance on any bubble. The next prompt is then created with
`dependencyMode = "chained"` and a `parentMessageId`, and its job waits for that
message before taking its snapshot — **whatever detection would have concluded**.

This is the escape hatch for when Stages 6–7 are wrong in either direction, so it
deliberately does not route through them: no heuristic, no classifier, no verdict
to be wrong. It is the one path in the system that is guaranteed correct by
construction rather than by accuracy.

Chaining works on a bubble that is *still streaming* — that is rather the point,
since it locks in the wait before the answer even exists. Chaining to a question
rather than an answer waits for that question's answer, since the prompt row is
already complete and waiting on it would silently do nothing.

Verified: a prompt chained to a still-generating message waited for it, moved its
cutoff past that message's completion, and read the result. The same prompt text
sent *unchained* is judged `independent` and fires immediately — so the override
was doing real work, not agreeing with detection by luck.

---

## What Stage 9 gives you

The last layer, for when Stages 6–8 all miss. After every completion the
conversation is re-scanned for pairs where the later prompt could not see the
earlier answer, was not made to wait, and — judged after the fact — contains a
reference that the unseen answer would have resolved. Those get a dismissible
nudge and a one-click regenerate.

This is the **abort/retry half of optimistic concurrency control**, and it is the
honest admission that the layers above it are not going to be right every time.
The Stage 7 miss ("refactor this function...") is precisely this shape.

Two properties make it safe to be crude:

- **It only ever suggests.** Nothing re-runs itself. A check this rough should
  not be spending tokens unasked, and the answer already on screen stands until
  the user decides otherwise.
- **It is tuned to over-offer.** Two shared topic words is a low bar, because a
  false positive costs a dismissible suggestion while a false negative costs a
  silently wrong answer.

Regenerating re-stamps the cutoff to now and re-runs the *same prompt* — that
single change is the entire fix, since everything committed since is now inside
the snapshot. Verified end to end: a concurrent prompt was flagged naming the
answer it missed, and regenerating took its context from 12 to 94 prompt tokens.

The review runs after the answer is committed and sent, and its failures are
swallowed — a fault in an advisory check must never fail the job that just
succeeded.

---

## Roadmap

| Stage | What it adds |
| --- | --- |
| 1 ✅ | Foundations and data model |
| 2 ✅ | Baseline single-threaded chat (the control group) |
| 3 ✅ | Concurrency plumbing — remove the input lock |
| 4 ✅ | History reconciliation UI |
| 5 ✅ | Token/cost dashboard |
| 6 ✅ | Dependency heuristic |
| 7 ✅ | Dependency classifier (cheap model call, ambiguous cases only) |
| 8 ✅ | Manual "chain" override |
| 9 ✅ | Optimistic fallback + regenerate nudge |
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
