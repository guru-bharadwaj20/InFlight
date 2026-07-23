# Streaming protocol

How a generation's tokens get from the job to the bubble, and why a dropped or
duplicated frame can never leave the rendered answer wrong.

## Frames

One WebSocket per client carries every job in a conversation; each frame names
its `job_id`, so the client routes by id, never by arrival order.

| type | payload | meaning |
| --- | --- | --- |
| `status` | `status` | job moved to pending/streaming |
| `chunk` | `seq`, `text` | the `seq`-th token span of this job |
| `resume` | `seq`, `text`, `status` | on (re)connect: the whole buffer so far |
| `dependency` | verdict, source, reason | the classifier settled an `unsure` verdict |
| `stale_context` | reason, source_id | this answer may have missed context |
| `done` / `error` | final content, tokens, model | terminal state |

## Guarantees the server provides

For a single job, `chunk` sequence numbers are **strictly increasing and
contiguous from 1** (`seq = 1, 2, 3, …`). The Redis replay buffer and its `seq`
are written in one transaction (`append_chunk`), so the buffer always holds
*exactly* the first `seq` chunks — a reader can never see a buffer and a `seq`
that disagree.

Redis pub/sub itself is **at-most-once** per subscriber: a frame published while
the client is momentarily disconnected is gone. Replay on (re)connect
(`resume`) plus the resync endpoint make the end-to-end channel **at-least-once**.

## The client cursor

The client keeps one cursor per job — `seen`, the highest `seq` it has applied —
and follows four rules:

1. `seq <= seen` → **duplicate**, drop it. (Idempotent: replay and live frames
   overlap by design.)
2. `seq == seen + 1` → **in order**, append `text`, advance `seen`.
3. `seq > seen + 1` → **gap**: a chunk was missed. Do *not* append (that would
   splice text across the hole). Fetch `GET …/messages/{id}/stream` — the
   authoritative buffer — reset the text and `seen` to it, and carry on.
4. `resume` → set text and `seen` to the buffer wholesale; `done`/`error` set the
   final committed content and park the cursor so late duplicates are ignored.

Rules 1 and 3 are what upgrade at-least-once delivery to **exactly-once in
effect**: duplicates are dropped, and a loss is healed from the buffer instead of
being rendered as a hole.

## Why the rendered text is always correct

Let `T_n` be the concatenation of the true first `n` token spans. Claim: the text
the client shows is always some `T_k`, and `k` only increases, converging to the
committed answer.

- Rule 2 advances the shown text from `T_seen` to `T_seen+1` — still a true
  prefix.
- Rule 1 changes nothing.
- Rule 3 replaces the shown text with the buffer, which is exactly `T_seq'` for
  the buffer's own `seq'` (server guarantee) — a true prefix, and `seq' >= seen`
  because chunks only move forward, so `k` never regresses.
- `done` sets `T_N`, the whole answer.

So the bubble never shows spliced, out-of-order, or duplicated text; the worst a
dropped frame can do is briefly hold the text one span behind until the next
frame or the resync catches it up. The property holds independent of how many
jobs share the socket, since every rule keys off `job_id`.

## Recovery paths, cheapest first

1. **Duplicate/overlap** — handled inline by the cursor (rule 1). No I/O.
2. **Single dropped chunk** — one resync fetch (rule 3). Coalesced per job.
3. **Socket drop** — reconnect + `resume` replays the buffer (rule 4); the
   backoff is bounded so a stalled bubble is never left silent for long.
4. **Worker restart mid-stream** — the row is orphaned; the client's resync sees
   a terminal/absent buffer and settles on the committed row content.
