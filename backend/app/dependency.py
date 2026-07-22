"""Cheap, prospective dependency detection — the first line of defence.

Run at submit time, before a job is fired, to answer one question: does this
prompt need an answer that has not been generated yet? Most prompts are settled
here for free. Only the genuinely ambiguous ones are worth paying a model call
for, which is what Stage 7 does with the `UNSURE` verdict.

The three verdicts are deliberately asymmetric in what they cost to get wrong:

    DEPENDENT    hold the job until its predecessor lands. Costs latency.
    INDEPENDENT  fire immediately. Costs correctness if it was wrong.
    UNSURE       escalate to the classifier. Costs one cheap model call.

This is a keyword and shape heuristic, not a parser. It has no notion of what a
noun is, so "antecedent present" is approximated by "some content word appears
before the pronoun". That approximation is why `UNSURE` exists at all: rather
than guess on a pronoun that might resolve inside its own sentence, it defers.
Being honestly uncertain is the whole design — see the limitations section in the
README.
"""

import re
from dataclasses import dataclass


class Verdict:
    DEPENDENT = "dependent"
    INDEPENDENT = "independent"
    UNSURE = "unsure"


class Source:
    HEURISTIC = "heuristic"
    CLASSIFIER = "classifier"
    CHAINED = "chained"


@dataclass
class Detection:
    verdict: str
    reason: str
    matched: str | None = None


# Phrases that only make sense as a continuation of something already said.
# These are unambiguous enough to decide on their own.
CONTINUATION = [
    (r"\b(continue|carry on|go on|keep going)\b", "continuation verb"),
    (r"\b(elaborate|expand)\b", "elaboration request"),
    (r"\b(tell|explain|give|say) (me )?more\b", "asks for more"),
    (r"\bgo deeper\b", "asks to go deeper"),
    (r"\bin (more|greater) detail\b", "asks for more detail"),
    (r"\b(rephrase|reword|summari[sz]e|simplify) (that|this|it)\b", "restates prior output"),
    # Scoped to instruction shapes ("do the same for Rust") so that comparisons
    # which resolve locally ("are these two the same?") are not swept up.
    (r"\b(do|repeat|try|apply|run) the same\b", "repeats a prior instruction"),
    (r"\bthe same (for|with|in|but)\b", "repeats a prior instruction"),
    (r"^(and|but|so|then)\b", "opens as a continuation"),
    (r"^why\b", "bare follow-up question"),
    (r"^how so\b", "bare follow-up question"),
    (r"^what about\b", "bare follow-up question"),
]

# Explicit references to earlier turns. No pronoun analysis needed.
OUTWARD = [
    (r"\bthe above\b", "refers to the above"),
    (r"\bwhat you (said|wrote|mentioned|described)\b", "refers to your answer"),
    (r"\byour (answer|response|reply|explanation|last)\b", "refers to your answer"),
    (r"\bthe (previous|last|prior|preceding) (answer|response|one|message|point)\b",
     "refers to the previous message"),
    (r"\b(that|this) (answer|response|explanation)\b", "refers to an answer"),
    (r"\bas (mentioned|described|explained) (above|earlier|before)\b", "refers backwards"),
    (r"\b(earlier|previously) (you|we)\b", "refers backwards"),
]

# Referring expressions that may or may not resolve inside the prompt itself.
PRONOUN = re.compile(
    r"\b(it|its|it's|they|them|their|theirs|he|him|his|she|her|hers)\b", re.I
)
DEMONSTRATIVE = re.compile(r"\b(that|this|these|those)\b", re.I)

# Words that cannot serve as an antecedent, so their presence before a pronoun
# says nothing about whether it resolves locally.
FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "as",
    "of", "to", "in", "on", "at", "by", "for", "with", "from", "into", "about",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "doing", "have", "has", "had", "having",
    "can", "could", "will", "would", "shall", "should", "may", "might", "must",
    "what", "when", "where", "who", "whom", "which", "why", "how",
    "i", "you", "we", "me", "us", "my", "your", "our", "please",
    "explain", "describe", "tell", "give", "show", "write", "list", "make",
    "not", "no", "yes", "all", "any", "some", "more", "most", "very", "just",
}

# A demonstrative directly followed by one of these is not naming a thing.
_NON_NOUN_AFTER = {
    "is", "are", "was", "were", "will", "would", "can", "could", "should",
    "means", "works", "does", "did", "has", "have", "to", "and", "or", "but",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _has_local_antecedent(text: str, before_index: int) -> bool:
    """Is there anything in this prompt the pronoun could be pointing at?

    Crude on purpose: any content word earlier in the prompt counts. It cannot
    tell a subject from an object, so it is used only to downgrade a verdict to
    UNSURE, never to declare independence outright.
    """
    return any(t not in FUNCTION_WORDS for t in _tokens(text[:before_index]))


def _bare_demonstrative(text: str, match: re.Match) -> bool:
    """True for "explain that" but not "that function returns a value".

    A demonstrative with no noun after it has nothing local to attach to, so it
    is almost always pointing at an earlier turn.
    """
    rest = _tokens(text[match.end():])
    return not rest or rest[0] in _NON_NOUN_AFTER


def evaluate(prompt: str) -> Detection:
    """Classify a prompt's dependence on earlier, possibly unfinished, turns."""
    text = (prompt or "").strip()
    if not text:
        return Detection(Verdict.INDEPENDENT, "empty prompt")

    lowered = text.lower()

    for pattern, reason in CONTINUATION:
        found = re.search(pattern, lowered)
        if found:
            return Detection(Verdict.DEPENDENT, reason, found.group(0))

    for pattern, reason in OUTWARD:
        found = re.search(pattern, lowered)
        if found:
            return Detection(Verdict.DEPENDENT, reason, found.group(0))

    demo = DEMONSTRATIVE.search(text)
    if demo and _bare_demonstrative(text, demo):
        return Detection(
            Verdict.DEPENDENT, "demonstrative with nothing to attach to", demo.group(0)
        )

    pronoun = PRONOUN.search(text)
    if pronoun:
        if _has_local_antecedent(text, pronoun.start()):
            # Something earlier in the sentence *might* be the referent, but this
            # heuristic cannot tell. Hand it to the classifier rather than guess.
            return Detection(
                Verdict.UNSURE, "pronoun with a possible local antecedent", pronoun.group(0)
            )
        return Detection(
            Verdict.DEPENDENT, "pronoun with no antecedent in the prompt", pronoun.group(0)
        )

    if demo:
        return Detection(
            Verdict.UNSURE, "demonstrative modifying a noun", demo.group(0)
        )

    return Detection(Verdict.INDEPENDENT, "no referring expressions")
