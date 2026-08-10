"""Per-video budget guard and cost ledger (§4.3, §19.E14).

Every client method is decorated with `@cost(kind=..., n=...)`. Before the call
the guard checks the remaining allowance; after it, the spend and the cache-hit
flag go to `runs/<run_id>/costs.jsonl`. A cache hit costs nothing — that is what
makes the accounting honest about where the money actually went.

Exceeding a limit raises `BudgetExceeded`, which the orchestrator turns into a
degradation level (§4.6). The video always ships.
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from .errors import BudgetExceeded
from .log import get_logger

log = get_logger("budget")

F = TypeVar("F", bound=Callable[..., Any])

#: config key -> human name of the counter it guards.
KIND_LIMITS = {
    "grok_text": "grok_text_calls_max",
    "grok_vision": "grok_vision_calls_max",
    "gemini_vision": "gemini_vision_calls_max",
    "magnific_search": "magnific_search_calls_max",
    "magnific_image": "magnific_image_gen_max",
    "magnific_video": "magnific_video_gen_max",
    "heygen_render": "heygen_renders_max",
    "elevenlabs_chars": "elevenlabs_chars_max",
    "tokens_in": "tokens_in_max",
    "tokens_out": "tokens_out_max",
}


@dataclass
class CostRecord:
    stage: str
    provider: str
    kind: str
    tokens_in: int = 0
    tokens_out: int = 0
    credits: float = 0.0
    seconds: float = 0.0
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "provider": self.provider,
            "kind": self.kind,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "credits": round(self.credits, 4),
            "seconds": round(self.seconds, 3),
            "cache_hit": self.cache_hit,
            "ts": round(time.time(), 3),
        }


@dataclass
class Budget:
    """Counters for one video."""

    limits: dict[str, int]
    used: dict[str, int] = field(default_factory=dict)
    records: list[CostRecord] = field(default_factory=list)
    warn_at_percent: int = 75
    on_exceed: str = "degrade"
    costs_file: Path | None = None
    started_at: float = field(default_factory=time.monotonic)
    wallclock_minutes_max: float = 35.0

    @classmethod
    def from_config(cls, config: Any, costs_file: Path | None = None) -> Budget:
        limits = {kind: int(config.budget(f"per_video.{key}")) for kind, key in KIND_LIMITS.items()}
        return cls(
            limits=limits,
            warn_at_percent=int(config.budget("warn_at_percent", 75)),
            on_exceed=str(config.budget("on_exceed", "degrade")),
            costs_file=costs_file,
            wallclock_minutes_max=float(config.budget("per_video.wallclock_minutes_max", 35)),
        )

    # -- queries ------------------------------------------------------------

    def spent(self, kind: str) -> int:
        return self.used.get(kind, 0)

    def remaining(self, kind: str) -> int:
        return max(0, self.limits.get(kind, 0) - self.spent(kind))

    def can_afford(self, kind: str, n: int = 1) -> bool:
        if kind not in self.limits:
            return True
        return self.spent(kind) + n <= self.limits[kind]

    def out_of_time(self) -> bool:
        return (time.monotonic() - self.started_at) / 60.0 > self.wallclock_minutes_max

    # -- mutation -----------------------------------------------------------

    def check(self, kind: str, n: int = 1) -> None:
        """Raise if this spend would break the limit. Call before the request."""
        if kind not in self.limits:
            return
        if not self.can_afford(kind, n):
            raise BudgetExceeded(kind, self.spent(kind), self.limits[kind])

    def charge(self, kind: str, n: int = 1) -> None:
        if kind not in self.limits:
            return
        self.used[kind] = self.spent(kind) + n
        limit = self.limits[kind]
        if limit and self.used[kind] * 100 / limit >= self.warn_at_percent:
            log.warning(
                "%s at %d/%d (%.0f%% of budget)",
                kind,
                self.used[kind],
                limit,
                self.used[kind] * 100 / limit,
                extra={"stage": "budget"},
            )

    def record(self, entry: CostRecord) -> None:
        self.records.append(entry)
        if self.costs_file is None:
            return
        self.costs_file.parent.mkdir(parents=True, exist_ok=True)
        with self.costs_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def summary(self) -> dict[str, Any]:
        cache_hits = sum(1 for r in self.records if r.cache_hit)
        return {
            "used": dict(self.used),
            "limits": dict(self.limits),
            "calls": len(self.records),
            "cache_hits": cache_hits,
            "cache_hit_rate": round(cache_hits / len(self.records), 3) if self.records else 0.0,
            "tokens_in": sum(r.tokens_in for r in self.records),
            "tokens_out": sum(r.tokens_out for r in self.records),
            "credits": round(sum(r.credits for r in self.records), 3),
            "wallclock_minutes": round((time.monotonic() - self.started_at) / 60.0, 2),
        }


# --------------------------------------------------------------------------- #
# Decorator
# --------------------------------------------------------------------------- #


def cost(kind: str, n: int = 1, provider: str = "") -> Callable[[F], F]:
    """Guard and meter one client method.

    The wrapped method must live on an object exposing `.budget` and `.stage`;
    if it returns a `(value, cache_hit)` pair the hit is recorded and the value
    passed through, so a cached call never charges the budget.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            budget: Budget | None = getattr(self, "budget", None)
            name = provider or getattr(self, "name", func.__module__)
            started = time.monotonic()

            if budget is not None:
                budget.check(kind, n)

            result = func(self, *args, **kwargs)
            cache_hit = bool(getattr(self, "_last_call_cached", False))

            if budget is not None:
                if not cache_hit:
                    budget.charge(kind, n)
                budget.record(
                    CostRecord(
                        stage=getattr(self, "stage", ""),
                        provider=name,
                        kind=kind,
                        tokens_in=int(getattr(self, "_last_tokens_in", 0) or 0),
                        tokens_out=int(getattr(self, "_last_tokens_out", 0) or 0),
                        credits=float(getattr(self, "_last_credits", 0.0) or 0.0),
                        seconds=time.monotonic() - started,
                        cache_hit=cache_hit,
                    )
                )
            _reset_call_meta(self)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _reset_call_meta(client: Any) -> None:
    for attribute in ("_last_call_cached", "_last_tokens_in", "_last_tokens_out", "_last_credits"):
        if hasattr(client, attribute):
            setattr(client, attribute, 0 if attribute != "_last_call_cached" else False)


def rolling_average_cost(history_file: Path, window: int = 10) -> float | None:
    """Mean credit cost of the last `window` videos, for the growth alert (§19.E14)."""
    if not history_file.exists():
        return None
    values: list[float] = []
    for line in history_file.read_text(encoding="utf-8").splitlines():
        try:
            values.append(float(json.loads(line).get("credits", 0.0)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    tail = values[-window:]
    return sum(tail) / len(tail) if tail else None


def dry_run_enabled() -> bool:
    """`make produce DRY=true` — clients answer from fixtures, nothing is spent."""
    return os.environ.get("REDSHIFT_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
