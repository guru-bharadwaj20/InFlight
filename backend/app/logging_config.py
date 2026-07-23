"""Structured JSON logging with request/job correlation IDs.

Plain log lines are useless the moment several generations interleave in one
worker: whose "job failed" was that? Every record here carries two ids pulled
from context — the HTTP request that set things in motion, and the job currently
running — so a line can always be tied back to one request and one generation,
and cross-referenced with the same job_id in `/traces`.

The ids ride on `contextvars`, so they follow the async task automatically:
`asyncio.create_task` copies the current context, which means a job spawned inside
a request inherits that request's id for free, and `run_job` stamps its own
job_id on top. JSON output by default (one object per line, ready for any log
pipeline); set LOG_JSON=false for readable lines in a terminal.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
job_id_ctx: ContextVar[str] = ContextVar("job_id", default="-")


class ContextFilter(logging.Filter):
    """Attach the current request/job ids to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        record.job_id = job_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "job_id": getattr(record, "job_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable fallback that still shows the ids."""

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", "-")
        jid = getattr(record, "job_id", "-")
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
        base += f"[req={rid} job={jid}] {record.name}: {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    """Install the correlation-aware handler on the `app` logger tree.

    Scoped to `app.*` (propagate off) so it does not fight uvicorn's own access
    logging; every module's `getLogger(__name__)` inherits it.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if as_json else PlainFormatter())
    handler.addFilter(ContextFilter())

    app_logger = logging.getLogger("app")
    app_logger.handlers.clear()
    app_logger.addHandler(handler)
    app_logger.setLevel(level.upper())
    app_logger.propagate = False
