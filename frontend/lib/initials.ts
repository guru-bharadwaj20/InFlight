/**
 * Initials for an avatar, from a display name.
 *
 * Lived inline in Sidebar (and, until it was removed, in a second hand-copied
 * form in the conversation header). Pulling it out is not just tidiness: the
 * original walked `name.trim().split(/\s+/)` and indexed `[0][0]` and `[1][0]`,
 * which quietly mishandles most of the real inputs a name field receives.
 *
 * Cases this now gets right:
 *   - an empty or whitespace-only name  -> "?" rather than ""
 *   - a single name ("Prince")          -> "P"
 *   - three or more names               -> first and *last*, the convention,
 *                                          rather than first and middle
 *   - leading punctuation ("  -ada ")   -> skipped, not used as an initial
 *   - non-ASCII ("Ada Łowicka")         -> upper-cased per locale rules
 *   - astral characters (emoji, some
 *     CJK)                              -> taken as one character, not as half
 *                                          a surrogate pair
 *
 * The last is the one that actually breaks in production: `"𝒜da"[0]` is a lone
 * surrogate and renders as a replacement glyph. Array.from iterates code points.
 */
export function initials(name: string): string {
  const words = (name ?? "")
    .split(/\s+/)
    .map((word) => Array.from(word).filter((ch) => /\p{L}|\p{N}/u.test(ch)))
    .filter((chars) => chars.length > 0);

  if (words.length === 0) return "?";

  const first = words[0][0];
  const last = words.length > 1 ? words[words.length - 1][0] : "";
  return (first + last).toLocaleUpperCase();
}
