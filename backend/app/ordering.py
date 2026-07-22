"""Display order for a conversation.

The mirror of `orderMessages` in frontend/lib/ordering.ts. The client sorts too —
it holds rows the server has not seen yet, straight from POST responses and
frames — but the API should not hand back an order that needs repairing.

Ordering is by `submitted_at`, never `completed_at`: the list reads in the order
questions were asked, whatever order the answers arrived in.
"""

from .models import Message, Role


def order_for_display(messages: list[Message]) -> list[Message]:
    """Sort into exchange order: each prompt immediately followed by its answer.

    The unit being ordered is the exchange, not the row. An answer sorts by its
    prompt's timestamp rather than its own, because a prompt and the job
    answering it are stamped within a microsecond of each other and several
    exchanges can start inside the same instant. Sorting rows independently then
    interleaves them into prompt, prompt, answer, answer.

    The sort is total — ties fall through to the anchor's id and then to role —
    so the order is stable across calls.
    """
    by_id = {m.id: m for m in messages}

    def anchor(message: Message) -> Message:
        if message.role == Role.ASSISTANT and message.prompt_message_id:
            return by_id.get(message.prompt_message_id, message)
        return message

    def key(message: Message) -> tuple:
        a = anchor(message)
        return (a.submitted_at, a.id, message.role == Role.ASSISTANT)

    return sorted(messages, key=key)
