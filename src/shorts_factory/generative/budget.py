"""Token budgeting for paid generation.

Magnific tokens are the scarce resource in this pipeline. The rules encoded
here are simple and strict: free tiers never touch the budget, a reserve is
held back for high-priority slots, and no single visual may eat more than its
share. Every decision is logged to a ledger that ends up in the run report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Budgets
from ..errors import BudgetExceededError
from ..logging_utils import get_logger

log = get_logger("budget")


@dataclass
class LedgerEntry:
    visual_id: str
    tier: str
    tokens: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"visual_id": self.visual_id, "tier": self.tier, "tokens": self.tokens, "reason": self.reason}


@dataclass
class TokenBudget:
    """A spend-tracking ledger with a reserve for hero shots."""

    total: int
    reserve: int = 0
    max_per_visual: int = 2
    spent: int = 0
    entries: list[LedgerEntry] = field(default_factory=list)
    _per_visual: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_budgets(cls, budgets: Budgets, override_total: int | None = None) -> TokenBudget:
        total = budgets.magnific_tokens if override_total is None else override_total
        return cls(
            total=max(0, total),
            reserve=min(max(0, budgets.magnific_reserve), max(0, total)),
            max_per_visual=max(1, budgets.magnific_max_per_visual),
        )

    # -- queries ------------------------------------------------------------

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.spent)

    @property
    def free_pool(self) -> int:
        """Tokens available to normal-priority work (reserve excluded)."""
        return max(0, self.remaining - self.reserve)

    def spent_on(self, visual_id: str) -> int:
        return self._per_visual.get(visual_id, 0)

    def can_spend(self, visual_id: str, tokens: int, *, hero: bool = False) -> tuple[bool, str]:
        """Check a hypothetical spend. Returns ``(allowed, reason_if_not)``."""
        if tokens <= 0:
            return True, ""
        if self.spent_on(visual_id) + tokens > self.max_per_visual:
            return False, (
                f"per-visual cap reached ({self.spent_on(visual_id)}/{self.max_per_visual} tokens)"
            )
        pool = self.remaining if hero else self.free_pool
        if tokens > pool:
            scope = "run budget" if hero else "non-reserved budget"
            return False, f"{scope} exhausted ({pool} left, need {tokens})"
        return True, ""

    # -- mutation -----------------------------------------------------------

    def spend(self, visual_id: str, tokens: int, tier: str, reason: str, *, hero: bool = False) -> None:
        allowed, why = self.can_spend(visual_id, tokens, hero=hero)
        if not allowed:
            raise BudgetExceededError(f"{visual_id}: {why}")
        self.spent += tokens
        self._per_visual[visual_id] = self.spent_on(visual_id) + tokens
        self.entries.append(LedgerEntry(visual_id, tier, tokens, reason))
        if tokens:
            log.info(
                "%s: spent %d token(s) on %s (%d/%d used)",
                visual_id,
                tokens,
                tier,
                self.spent,
                self.total,
                extra={"stage": "budget"},
            )

    def record_free(self, visual_id: str, tier: str, reason: str) -> None:
        """Log a zero-cost generation so the report shows what the tier saved."""
        self.entries.append(LedgerEntry(visual_id, tier, 0, reason))

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "spent": self.spent,
            "remaining": self.remaining,
            "reserve": self.reserve,
            "max_per_visual": self.max_per_visual,
            "ledger": [entry.to_dict() for entry in self.entries],
        }
