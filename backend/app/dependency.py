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
    # "tell me more" continues something; "tell me more about the Baroque
    # period" names its own subject. The lookahead needs two words after
    # "about", so "more about that" still reads as a continuation.
    (r"\b(tell|explain|give|say) (me )?more\b(?!\s+about\s+\w+\s+\w+)", "asks for more"),
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

# Selectors that index into a set the prompt never establishes: "which one",
# "the second approach". Treated like pronouns — they need an antecedent, and
# the same local-antecedent test decides whether to commit or defer.
ORDINAL = re.compile(
    r"\b(?:which|the)\s+(?:one|first|second|third|fourth|fifth|last|next|other|former|latter)\b",
    re.I,
)

# Below this many words, with no referring expression and no stated subject, a
# prompt is almost certainly a fragment continuing something ("shorter.", "in
# bullet points"). Almost, not certainly — so these defer rather than decide.
FRAGMENT_MAX_WORDS = 4

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
    "use", "apply", "try", "now", "also", "please", "again", "instead",
    "compare", "rewrite", "refactor", "translate", "summarise", "summarize",
    "expand", "rephrase", "convert", "define", "draft", "name", "pick",
}

# A demonstrative directly followed by one of these is not naming a thing.
_NON_NOUN_AFTER = {
    "is", "are", "was", "were", "will", "would", "can", "could", "should",
    "means", "works", "does", "did", "has", "have", "to", "and", "or", "but",
}


# A word inside quotes or backticks is being *mentioned*, not used: "explain
# what `this` means" asks about a keyword, it does not point at an earlier turn.
# The lookbehind keeps apostrophes in contractions from opening a quoted span.
QUOTED = re.compile(r"`[^`]*`|(?<!\w)'[^']*'|\"[^\"]*\"|“[^”]*”")

# A colon followed by several words supplies the content the sentence refers
# forward to — "summarise this text: ..." carries its own object.
COLON_CONTENT = re.compile(r":\s*\S+(?:\s+\S+){2,}")


def _strip_mentions(text: str) -> str:
    return QUOTED.sub(" ", text)


def _content_colon(text: str) -> int | None:
    match = COLON_CONTENT.search(text)
    return match.start() if match else None


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _has_local_antecedent(text: str, before_index: int) -> bool:
    """Is there anything in this prompt the pronoun could be pointing at?

    Crude on purpose: any content word earlier in the prompt counts. It cannot
    tell a subject from an object, so it is used only to downgrade a verdict to
    UNSURE, never to declare independence outright.
    """
    return any(t not in FUNCTION_WORDS for t in _tokens(text[:before_index]))


def _is_relative_that(text: str, match: re.Match) -> bool:
    """True for "a function that reverses a list" — a relative clause, not a pointer.

    Only "that" is ambiguous this way; "this"/"these"/"those" do not introduce
    relative clauses. The tell is what precedes it: a bare noun ("function
    that...") makes it a connector, whereas a verb or the start of the sentence
    ("compare that", "Is that true") makes it an object, and therefore a
    reference. Instruction verbs are listed as function words precisely so they
    read as the latter.
    """
    if match.group(0).lower() != "that":
        return False
    before = _tokens(text[: match.start()])
    return bool(before) and before[-1] not in FUNCTION_WORDS


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

    # Quoted spans are removed before matching so a keyword being discussed is
    # not read as a keyword being used.
    probe = _strip_mentions(text)
    lowered = probe.lower()
    colon = _content_colon(probe)

    def supplies_own_content(index: int) -> bool:
        """The trigger sits before a colon that then hands over the content."""
        return colon is not None and index < colon

    for pattern, reason in CONTINUATION + OUTWARD:
        found = re.search(pattern, lowered)
        if found and not supplies_own_content(found.start()):
            return Detection(Verdict.DEPENDENT, reason, found.group(0))

    demo = next(
        (m for m in DEMONSTRATIVE.finditer(probe) if not _is_relative_that(probe, m)),
        None,
    )
    if demo and not supplies_own_content(demo.start()) and _bare_demonstrative(probe, demo):
        return Detection(
            Verdict.DEPENDENT, "demonstrative with nothing to attach to", demo.group(0)
        )

    # Pronouns and ordinal selectors are the same problem: a referring expression
    # that either resolves inside the prompt or does not. Whichever appears
    # first is the one that decides.
    referring = min(
        (m for m in (PRONOUN.search(probe), ORDINAL.search(probe)) if m),
        key=lambda m: m.start(),
        default=None,
    )
    if referring:
        if _has_local_antecedent(probe, referring.start()):
            # Something earlier *might* be the referent, but this heuristic
            # cannot tell. Hand it to the classifier rather than guess.
            return Detection(
                Verdict.UNSURE, "reference with a possible local antecedent", referring.group(0)
            )
        return Detection(
            Verdict.DEPENDENT, "reference with no antecedent in the prompt", referring.group(0)
        )

    if demo and not supplies_own_content(demo.start()):
        return Detection(Verdict.UNSURE, "demonstrative modifying a noun", demo.group(0))

    # No referring expression at all. Usually that means self-contained — but a
    # bare fragment ("shorter.", "in bullet points") has nothing to refer *with*
    # and still cannot stand alone. Rules cannot separate those from a genuinely
    # terse request, so defer instead of deciding.
    if len(_tokens(probe)) <= FRAGMENT_MAX_WORDS:
        return Detection(Verdict.UNSURE, "short fragment with no stated subject")

    return Detection(Verdict.INDEPENDENT, NO_REFERENCES)


NO_REFERENCES = "no referring expressions"

# Two shared topic words is a deliberately low bar. The nudge it produces is
# dismissible and non-blocking, so a false positive costs a suggestion while a
# false negative costs a silently wrong answer.
OVERLAP_THRESHOLD = 2
MIN_TOPIC_WORD = 4


def topic_words(text: str) -> set[str]:
    """Content words worth comparing between a prompt and an answer.

    Public so a caller running the retrospective check over a window of answers
    can tokenise each answer once instead of once per pair.
    """
    return {
        t for t in _tokens(text)
        if len(t) >= MIN_TOPIC_WORD and t not in FUNCTION_WORDS
    }


_topic_words = topic_words  # existing internal callers


def prepare_retrospective(prompt: str) -> tuple[Detection, set[str]] | None:
    """The per-prompt half of `retrospective_conflict`, done once.

    `evaluate` runs twenty-odd regexes and tokenises the whole prompt, and
    `_topic_words` tokenises it again. Neither depends on the answer being
    compared against, so doing them inside a pairwise loop repeats the same work
    once per candidate. Returns None when the prompt cannot conflict with
    anything, which lets a caller skip it entirely.
    """
    detection = evaluate(prompt)
    if detection.verdict == Verdict.INDEPENDENT and detection.reason == NO_REFERENCES:
        return None
    return detection, _topic_words(prompt)


def retrospective_match(
    prepared: tuple[Detection, set[str]], answer_topics: set[str]
) -> str | None:
    """The per-pair half: a set intersection, and nothing else."""
    detection, prompt_topics = prepared
    shared = prompt_topics & answer_topics
    if len(shared) < OVERLAP_THRESHOLD:
        return None
    terms = ", ".join(sorted(shared)[:3])
    return (
        f"this prompt refers to something ({detection.matched or 'a reference'}) "
        f"and the answer it could not see discusses {terms}"
    )


def retrospective_conflict(prompt: str, answer: str) -> str | None:
    """Judged after the fact: did this prompt probably need that answer?

    The last line of defence, for when the heuristic and the classifier both let
    a prompt run concurrently and the answer it needed then landed anyway. Same
    signals as `evaluate`, run backwards: the prompt had a referring expression
    that nothing local resolved, and the answer it could not see turns out to
    talk about the same things.

    Returns a human-readable reason, or None if there is nothing to suggest.
    This only ever produces a dismissible nudge — it never re-runs anything on
    its own, because a check this crude should not be spending tokens unasked.
    """
    prepared = prepare_retrospective(prompt)
    if prepared is None:
        return None
    return retrospective_match(prepared, _topic_words(answer))
