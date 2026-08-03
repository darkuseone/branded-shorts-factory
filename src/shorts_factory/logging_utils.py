"""Logging helpers: readable console output plus a machine-readable run log."""

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

LOGGER_NAME = "shorts_factory"

_LEVEL_COLORS = {
    "DEBUG": "\033[2;37m",
    "INFO": "\033[0;36m",
    "WARNING": "\033[0;33m",
    "ERROR": "\033[0;31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class _ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__("%(message)s")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", None)
        prefix = f"[{stage}] " if stage else ""
        message = f"{prefix}{record.getMessage()}"
        if self.color:
            tint = _LEVEL_COLORS.get(record.levelname, "")
            message = f"{tint}{message}{_RESET}"
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return message


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        stage = getattr(record, "stage", None)
        if stage:
            payload["stage"] = stage
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = extra
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the shared project logger (or a child of it)."""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> logging.Logger:
    """Configure console (and optional JSONL) logging exactly once per process."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_ConsoleFormatter(color))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        jsonl = logging.FileHandler(log_file, encoding="utf-8")
        jsonl.setFormatter(_JsonlFormatter())
        jsonl.setLevel(logging.DEBUG)
        logger.addHandler(jsonl)

    return logger


@contextmanager
def stage(name: str, logger: logging.Logger | None = None) -> Iterator[dict[str, Any]]:
    """Time a pipeline stage and report how it ended.

    The yielded dict is free-form scratch space; whatever the caller stores in
    it is attached to the closing log record.
    """
    log = logger or get_logger()
    payload: dict[str, Any] = {}
    started = time.monotonic()
    log.info("start", extra={"stage": name})
    try:
        yield payload
    except Exception:
        elapsed = time.monotonic() - started
        log.error("failed after %.1fs", elapsed, extra={"stage": name, "data": payload})
        raise
    elapsed = time.monotonic() - started
    summary = " ".join(f"{k}={v}" for k, v in payload.items()) if payload else ""
    log.info("done in %.1fs %s", elapsed, summary, extra={"stage": name, "data": payload})
