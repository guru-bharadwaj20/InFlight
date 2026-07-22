# Build log

Twelve stages, each shipping a working increment. Kept out of the README because
it is a record of how the system was built and what testing found along the way,
not what it does — but that record is most of the engineering, so it is not
thrown away.

Every stage that fixed a bug names the bug and why it existed. The interesting
ones: answers addressed to the wrong prompts (Stage 3), interleaved exchanges
under timestamp ties (Stage 4), millisecond precision silently defeating a
microsecond cutoff, stale ORM reads inside the dependency wait (Stage 7), and a
concurrency cap that admitted everything because it was not atomic (Stage 10).

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
decided           32  (64% of set)
deferred (unsure) 18  -> Stage 7 classifier
correct        32/32  (100% of decided)
```

`docker compose exec backend python -m scripts.eval_dependency`

**Deferring is not counted as an error.** Those 18 cases are the ones Stage 7
exists for. A heuristic that answered all 50 confidently and got a fifth wrong
would be worse than one that answers 32 and knows when it doesn't know — 100% is
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

Measured on the deferred cases (`python -m scripts.eval_classifier`):

```
correct  14/15  (93.3%)   3 excluded: provider errors
  false independent (costs correctness) 1
  false dependent   (costs latency)     0
```

The single miss is "Refactor this function to be tail recursive", called
independent when it was dependent — the expensive direction, and exactly the
case Stage 9's retrospective check exists to catch. Full pipeline numbers are in
Stage 11.

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

## What Stage 10 gives you

Survives being used badly, verified against all four failure modes at once:

```
1. cancel mid-stream      A cancelled, 81 chars of partial output kept,
                          B unaffected, A absent from context (never committed)
2. failure isolation      one job errors, sibling completes
3. reconnect mid-stream   resume + chunks rebuild to 513 chars vs 513 stored
                          — no gap, no duplication
4. concurrency cap        12 fired -> 8 accepted, 4 rejected with 429
```

**Cancelling keeps what was already produced.** It is what the user watched
appear, and discarding it would make the bubble jump. The row settles as
`cancelled` with no `completed_at`, so it never becomes context for anything —
a half-answer is not a fact about the conversation. Anything waiting on it
unblocks, because `cancelled` is terminal.

### The bug the cap test found

The limit was a count read from Redis and then compared — check-then-act, with a
round trip in the middle. Twelve simultaneous sends all read the same count
before any of them registered, and all twelve were admitted. A concurrency limit
implemented non-atomically is not a limit.

Reservation is now a Lua script, which Redis runs atomically, so the test and the
insert cannot interleave. The slot is claimed *before* any row is written, and
released if the write then fails.

Cancel also settles rows orphaned by a restart, which would otherwise sit
`streaming` forever with no task behind them.

---
