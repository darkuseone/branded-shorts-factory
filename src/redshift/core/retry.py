"""Retries, backoff and a per-provider circuit breaker (§4.4).

The point of the breaker is not politeness: when a provider starts failing, the
ladder of sources (§9.3) has somewhere else to go, and burning five timeouts per
shot on a dead API is how a run hits its 35-minute wall clock.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .errors import CircuitOpen, ProviderError
from .log import get_logger

log = get_logger("retry")

T = TypeVar("T")

RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
BACKOFF_SCHEDULE = (2.0, 6.0, 18.0)
MAX_RATE_LIMIT_ATTEMPTS = 5
BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_S = 600.0


@dataclass
class CircuitBreaker:
    """Five consecutive failures park a provider for ten minutes."""

    threshold: int = BREAKER_THRESHOLD
    cooldown_s: float = BREAKER_COOLDOWN_S
    failures: dict[str, int] = field(default_factory=dict)
    opened_until: dict[str, float] = field(default_factory=dict)

    def is_open(self, provider: str) -> bool:
        until = self.opened_until.get(provider, 0.0)
        if until and time.monotonic() < until:
            return True
        if until:
            self.opened_until.pop(provider, None)
            self.failures[provider] = 0
        return False

    def guard(self, provider: str) -> None:
        if self.is_open(provider):
            raise CircuitOpen(provider, self.opened_until[provider])

    def record_success(self, provider: str) -> None:
        self.failures[provider] = 0

    def record_failure(self, provider: str) -> None:
        count = self.failures.get(provider, 0) + 1
        self.failures[provider] = count
        if count >= self.threshold:
            self.opened_until[provider] = time.monotonic() + self.cooldown_s
            log.warning(
                "%s tripped the circuit breaker after %d failures; parked for %.0fs",
                provider,
                count,
                self.cooldown_s,
                extra={"stage": "retry"},
            )


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Delay before attempt N (0-based). Honours Retry-After when given."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), 30.0)
    base = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
    return base * (1.0 + random.random() * 0.25)


def with_retries(
    call: Callable[[], T],
    *,
    provider: str,
    breaker: CircuitBreaker | None = None,
    attempts: int = len(BACKOFF_SCHEDULE),
    retry_on: Iterable[type[Exception]] = (ProviderError,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `call`, retrying transient failures with backoff."""
    if breaker is not None:
        breaker.guard(provider)

    retry_types = tuple(retry_on)
    last: Exception | None = None

    for attempt in range(attempts):
        try:
            result = call()
        except retry_types as exc:
            last = exc
            status = getattr(exc, "status", None)
            fatal = status is not None and status not in RETRY_STATUSES
            if fatal or attempt == attempts - 1:
                if breaker is not None:
                    breaker.record_failure(provider)
                raise
            delay = backoff_delay(attempt, getattr(exc, "retry_after", None))
            log.debug(
                "%s failed (%s), retry %d/%d in %.1fs",
                provider,
                exc,
                attempt + 1,
                attempts,
                delay,
                extra={"stage": "retry"},
            )
            sleep(delay)
        else:
            if breaker is not None:
                breaker.record_success(provider)
            return result

    if breaker is not None:
        breaker.record_failure(provider)
    raise last if last else ProviderError(provider, "exhausted retries")


def parse_json_strict(text: str) -> Any:
    """Recover JSON from an LLM reply (§4.4, last two steps).

    Order: parse as-is → strip markdown fences → cut between the first opening
    and last closing bracket. Anything else is the caller's problem.
    """
    import json

    stripped = (text or "").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    if stripped.startswith("```"):
        body = stripped.split("```", 2)
        if len(body) >= 2:
            candidate = body[1]
            candidate = candidate.split("\n", 1)[1] if "\n" in candidate else candidate
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("model reply contains no parsable JSON")
