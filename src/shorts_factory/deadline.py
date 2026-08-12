"""A wall-clock budget for one run (§19: `wallclock_minutes_max`).

Every other budget in this project counts calls or tokens. None of them counts
time, and time is the one that actually kills a run: a single Magnific
generation polls for up to seven minutes, and a scenario with twenty-five
slots can queue enough of those to sail past the workflow's own timeout. When
that happens the job is killed from outside — no artifact, no report, no
partial output, and a log that just stops.

The degradation ladder already says the video always ships. This is the same
rule applied to the clock: when time runs short the run stops reaching for the
slow, optional tiers and finishes with what it has. A slightly weaker video
that exists beats a perfect one that got killed at minute ninety.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .logging_utils import get_logger

log = get_logger("deadline")

#: Below this much remaining, an optional slow step is not worth starting.
#: One Magnific generation polls up to 420s, so anything under that is a
#: coin flip on being cut off mid-call.
SLOW_STEP_S = 420.0


@dataclass
class Deadline:
    """How much wall clock this run may still spend."""

    limit_s: float
    started: float = field(default_factory=time.monotonic)
    #: Set once, so the report can say the run degraded rather than guessing.
    exceeded_at: float | None = None

    @classmethod
    def from_minutes(cls, minutes: float) -> Deadline:
        return cls(limit_s=max(0.0, float(minutes) * 60.0))

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        if self.limit_s <= 0:
            return float("inf")
        return self.limit_s - self.elapsed

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    def allows(self, cost_s: float) -> bool:
        """Whether a step expected to take `cost_s` should be started at all."""
        remaining = self.remaining
        if remaining == float("inf"):
            return True
        if remaining <= cost_s:
            if self.exceeded_at is None:
                self.exceeded_at = self.elapsed
                log.warning(
                    "wall clock nearly spent (%.0fs of %.0fs used) — "
                    "skipping optional slow steps from here on",
                    self.elapsed,
                    self.limit_s,
                    extra={"stage": "budget"},
                )
            return False
        return True

    def allows_slow_step(self) -> bool:
        return self.allows(SLOW_STEP_S)

    def cap(self, timeout_s: float) -> float:
        """Shrink a call's own timeout so it cannot outlive the run."""
        remaining = self.remaining
        if remaining == float("inf"):
            return timeout_s
        return max(1.0, min(timeout_s, remaining))

    def to_dict(self) -> dict[str, float | bool | None]:
        return {
            "limit_s": round(self.limit_s, 1),
            "elapsed_s": round(self.elapsed, 1),
            "degraded_at_s": round(self.exceeded_at, 1) if self.exceeded_at is not None else None,
        }
