"""Collision-resistant IDs in Prisma's `cuid()` shape.

The schema declares `@default(cuid())`, but Prisma generates those client-side,
so Postgres has no column default. The Python backend is the only writer, so it
mints IDs itself — this keeps every row's ID in the same format as anything
created through Prisma Studio or a Prisma seed script.
"""

import os
import secrets
import threading
import time

_BLOCK_SIZE = 4
_BASE = 36
_DISCRETE_VALUES = _BASE**_BLOCK_SIZE

_counter = 0
_counter_lock = threading.Lock()


def _to_base36(number: int, pad: int = 0) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        out = "0"
    else:
        out = ""
        while number > 0:
            number, remainder = divmod(number, _BASE)
            out = digits[remainder] + out
    return out.rjust(pad, "0")[-pad:] if pad else out


def _next_counter() -> int:
    global _counter
    with _counter_lock:
        _counter = (_counter + 1) % _DISCRETE_VALUES
        return _counter


def _fingerprint() -> str:
    pid = _to_base36(os.getpid(), _BLOCK_SIZE)[-2:]
    host = _to_base36(sum(ord(c) for c in os.uname().nodename) if hasattr(os, "uname") else 0, 2)
    return (pid + host)[:4].rjust(4, "0")


_FINGERPRINT = _fingerprint()


def new_id() -> str:
    timestamp = _to_base36(int(time.time() * 1000))
    counter = _to_base36(_next_counter(), _BLOCK_SIZE)
    random_block = _to_base36(secrets.randbelow(_DISCRETE_VALUES), _BLOCK_SIZE)
    random_block += _to_base36(secrets.randbelow(_DISCRETE_VALUES), _BLOCK_SIZE)
    return f"c{timestamp}{counter}{_FINGERPRINT}{random_block}"
