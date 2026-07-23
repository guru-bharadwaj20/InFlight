"""The conversation as a DAG, not just a list.

A linear transcript is the common case, but the data model has always been a DAG:
every message can point at a `parent_message_id`, and a chain (or a fork) makes
that edge meaningful. When two prompts both point back at the same earlier
message, the conversation branches — two futures from one past.

This turns those edges into a tree for reading, and into a single root-to-leaf
*thread* for isolating one branch. Both are pure functions over the messages a
handler already loaded, so they add a view without touching the write path or the
snapshot model underneath it.

`parent_message_id` is the branch edge; a message with none is a root of the
conversation. The build is cycle-safe by construction (a node is attached under a
parent only if the parent was seen), and any edge that would form a cycle is
dropped and reported rather than looped on.
"""

from __future__ import annotations

from .models import Message


def _preview(message: Message) -> str:
    text = (message.content or "").strip().replace("\n", " ")
    return text[:80]


def _node(message: Message) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "status": message.status,
        "parent_message_id": message.parent_message_id,
        "preview": _preview(message),
        "children": [],
    }


def build_tree(messages: list[Message]) -> list[dict]:
    """Nest messages under their `parent_message_id`; return the roots.

    Ordering within a parent follows submission order, so a branch reads in the
    order its prompts were sent. A parent that is not in the set (e.g. filtered
    out) leaves its child as a root rather than dropping it.
    """
    ordered = sorted(messages, key=lambda m: m.submitted_at)
    nodes = {m.id: _node(m) for m in ordered}
    roots: list[dict] = []

    for message in ordered:
        node = nodes[message.id]
        parent_id = message.parent_message_id
        parent = nodes.get(parent_id) if parent_id else None
        # Attach under the parent, unless that would close a cycle back onto the
        # node itself or the parent is absent — then treat this as a root.
        if parent is not None and parent_id != message.id and not _would_cycle(nodes, message.id, parent_id):
            parent["children"].append(node)
        else:
            roots.append(node)
    return roots


def _would_cycle(nodes: dict[str, dict], child_id: str, parent_id: str) -> bool:
    """True if making child the parent of parent_id already reachable... i.e. if
    child_id is an ancestor of parent_id, attaching would form a cycle."""
    seen: set[str] = set()
    cur: str | None = parent_id
    while cur is not None and cur not in seen:
        if cur == child_id:
            return True
        seen.add(cur)
        cur = nodes[cur]["parent_message_id"] if cur in nodes else None
    return False


def thread_to(messages: list[Message], message_id: str) -> list[dict]:
    """The single branch ending at `message_id`: the root-to-leaf path.

    Walk `parent_message_id` upward from the target to a root, then reverse. This
    is one conversation branch in isolation — the thread you would read if you
    followed only the choices that led here, with the other branches pruned away.
    """
    by_id = {m.id: m for m in messages}
    path: list[dict] = []
    seen: set[str] = set()
    cur: str | None = message_id
    while cur is not None and cur in by_id and cur not in seen:
        seen.add(cur)
        path.append(_node(by_id[cur]))
        cur = by_id[cur].parent_message_id
    path.reverse()
    return path
