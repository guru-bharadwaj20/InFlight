"""Locate the labelled eval cases, in both layouts these scripts run under.

docker-compose mounts ./eval to /app/eval next to /app/scripts, so in the
container the data sits one level above this file. In a plain checkout (how CI
runs them) the scripts live under backend/ while eval/ stays at the repo root,
which is two levels up. Probing for the directory keeps `python -m scripts.eval_*`
working either way instead of only inside the container.
"""

from pathlib import Path

_HERE = Path(__file__).resolve()
# Slice rather than index: never raises if this file is somewhere shallow.
_CANDIDATES = tuple(parent / "eval" for parent in _HERE.parents[1:3])


def eval_dir() -> Path:
    for candidate in _CANDIDATES:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not find the eval case directory. Looked in: "
        + ", ".join(str(c) for c in _CANDIDATES)
    )


def case_file(name: str) -> Path:
    return eval_dir() / name
