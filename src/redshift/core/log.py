"""Structured logging: one line = one event, always carrying run_id and stage.

Two sinks. The console sink is readable by a human watching an Actions log; the
JSONL sink is what `runs/<run_id>/events.jsonl` gets so a failed run can be
reconstructed after the fact.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOGGER_NAME = "redshift"

_LEVEL_TINT = {
    "DEBUG": "\033[2;37m",
    "INFO": "\033[0;36m",
    "WARNING": "\033[0;33m",
    "ERROR": "\033[0;31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"

#: Anything whose name looks like a secret never reaches a sink (§0.8).
_SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "api_key")


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            clean[key] = "***"
        elif isinstance(value, dict):
            clean[key] = _redact(value)
        else:
            clean[key] = value
    return clean


class _Console(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__("%(message)s")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", "")
        prefix = f"[{stage}] " if stage else ""
        text = f"{prefix}{record.getMessage()}"
        if self.color:
            text = f"{_LEVEL_TINT.get(record.levelname, '')}{text}{_RESET}"
        if record.exc_info:
            text = f"{text}\n{self.formatException(record.exc_info)}"
        return text


class _Jsonl(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for field in ("run_id", "stage"):
            value = getattr(record, field, None)
            if value:
                event[field] = value
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            event["data"] = _redact(data)
        return json.dumps(event, ensure_ascii=False)


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def setup_logging(*, verbose: bool = False, events_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_Console(sys.stderr.isatty() and os.environ.get("NO_COLOR") is None))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console)

    if events_file is not None:
        events_file.parent.mkdir(parents=True, exist_ok=True)
        jsonl = logging.FileHandler(events_file, encoding="utf-8")
        jsonl.setFormatter(_Jsonl())
        jsonl.setLevel(logging.DEBUG)
        logger.addHandler(jsonl)
    return logger


@contextmanager
def stage_timer(stage: str, run_id: str = "", logger: logging.Logger | None = None) -> Iterator[dict]:
    """Time a stage and report how it ended. The yielded dict is scratch space."""
    log = logger or get_logger()
    extra = {"stage": stage, "run_id": run_id}
    payload: dict[str, Any] = {}
    started = time.monotonic()
    log.info("start", extra=extra)
    try:
        yield payload
    except Exception:
        log.error("failed after %.1fs", time.monotonic() - started, extra={**extra, "data": payload})
        raise
    summary = " ".join(f"{k}={v}" for k, v in payload.items())
    log.info(
        "done in %.1fs %s",
        time.monotonic() - started,
        summary,
        extra={**extra, "data": payload},
    )
