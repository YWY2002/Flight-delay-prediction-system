"""Structured logging setup.

Why structured rather than formatted strings: a poller emits one line per poll,
forever. The questions you actually ask of those lines are aggregations, not
reads. "What was the median poll latency for KJFK yesterday", "how many polls
returned zero aircraft", "when did credits start dropping faster than usual".

Answering those from `"fetched 214 aircraft in 0.42s"` means writing regexes
against prose that changes whenever someone edits a message. Answering them from
`{"event": "poll.completed", "airport": "KJFK", "aircraft": 214, ...}` is a
filter on a field. The log line becomes a record rather than a sentence.

Stdlib `logging` calls from the rest of the codebase (and from httpx) are routed
through the same renderer, so output stays uniform without every module having
to adopt structlog.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Configure structlog and route stdlib logging through it.

    Idempotent: safe to call more than once, which matters because tests and
    entry points both call it.

    Args:
        level: Standard level name, e.g. "INFO" or "DEBUG".
        json_logs: JSON output when True (machine-readable, for containers and
            log shippers), coloured key-value output when False (readable, for
            local development). The processor chain is identical either way, so
            the two differ only in rendering, never in content.
    """
    # Processors shared by structlog loggers and stdlib records, so both produce
    # the same fields regardless of which API a module happens to use.
    shared_processors: list[Any] = [
        # Lets a caller bind context once (bind_contextvars) and have it appear
        # on every log line beneath, without threading it through signatures.
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        # UTC, always. Local timestamps in logs are unreadable the moment two
        # machines in different zones write to the same place.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            # Hands off to ProcessorFormatter rather than rendering here, so
            # structlog and stdlib records converge on one formatter.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Runs only on records from stdlib logging, bringing them up to the same
        # shape as structlog's own.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace rather than append: repeated configuration would otherwise stack
    # handlers and duplicate every line once per call.
    root.handlers = [handler]
    root.setLevel(level.upper())

    # httpx logs every request at INFO. With a 90 second poll across three
    # airports that is pure noise drowning the lines we actually want.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.stdlib.get_logger(name)
