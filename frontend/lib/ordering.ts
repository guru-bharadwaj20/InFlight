import type { Message } from "./api";

/**
 * Put a conversation into the order the user experienced it.
 *
 * Display order is `submitted_at`, never `completed_at` — the list reads
 * top-to-bottom in the order questions were asked, regardless of the order the
 * answers came back in. A bubble's position is therefore fixed the moment it is
 * submitted: a fast answer landing before a slow one asked earlier changes that
 * bubble's height and nothing else.
 *
 * The unit being ordered is the *exchange*, not the row. An answer sorts by its
 * prompt's timestamp rather than its own, because prompts submitted close
 * enough together get stamped at the same instant, and ordering those rows
 * independently would interleave two exchanges into prompt, prompt, answer,
 * answer. The sort is total — ties fall through to id, then to role — so React
 * never sees two rows swap places between renders.
 */
// Lexical comparison of `submitted_at` is only correct if the backend always
// emits fixed-width, same-timezone ISO-8601 strings (it does — see the
// microsecond-timestamps migration). Nothing here enforced that assumption,
// so a differently-formatted or malformed value would silently reorder the
// whole thread with no error. This is a one-time, dev-visible tripwire rather
// than a hard failure: reordering a chat thread is not worth crashing over.
const TIMESTAMP_SHAPE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:Z|[+-]\d{2}:\d{2})$/;
let warnedAboutTimestampShape = false;

function checkTimestampShape(value: string): void {
  if (warnedAboutTimestampShape || TIMESTAMP_SHAPE.test(value)) return;
  warnedAboutTimestampShape = true;
  console.error(
    `ordering.ts: submitted_at "${value}" is not a fixed-format ISO-8601 ` +
      "timestamp — lexical ordering assumes it is, so messages may silently reorder."
  );
}

export function orderMessages(messages: Message[]): Message[] {
  const byId = new Map(messages.map((m) => [m.id, m]));

  const anchor = (m: Message) =>
    (m.role === "assistant" && m.prompt_message_id
      ? byId.get(m.prompt_message_id)
      : null) ?? m;

  return [...messages].sort((a, b) => {
    const [pa, pb] = [anchor(a), anchor(b)];
    checkTimestampShape(pa.submitted_at);
    checkTimestampShape(pb.submitted_at);
    if (pa.submitted_at !== pb.submitted_at)
      return pa.submitted_at < pb.submitted_at ? -1 : 1;
    if (pa.id !== pb.id) return pa.id < pb.id ? -1 : 1;
    return Number(a.role === "assistant") - Number(b.role === "assistant");
  });
}
