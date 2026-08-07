"""The escalation policy: when is free stock good enough?

Illustration / motion / generated slots prefer Magnific (library → unlimited)
before burning a huge free-stock fan-out. Literal news / footage still tries
free stock first, then escalates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..config import Budgets
from ..search.aggregator import SearchOutcome
from ..spec import Visual
from .budget import TokenBudget

Action = Literal[
    "accept_free",
    "magnific_library",
    "magnific_generate",
    "magnific_first",
    "grok_fallback",
    "manual_review",
]

#: Types that may start on Magnific — NOT news plates (footage/screen first).
MAGNIFIC_FIRST_TYPES = frozenset({"motion_graphics", "generated"})


@dataclass
class Decision:
    action: Action
    reason: str
    hero: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"action": self.action, "reason": self.reason, "hero": self.hero}


def decide(
    visual: Visual,
    outcome: SearchOutcome,
    budget: TokenBudget,
    budgets: Budgets,
    *,
    magnific_available: bool,
    grok_available: bool,
    min_score: float | None = None,
) -> Decision:
    """Pick the next step for one visual slot after free-stock search."""
    hero = visual.is_hero
    best = outcome.best_score
    accept_at = max(budgets.accept_above_score, visual.quality_floor)
    escalate_below = min_score if min_score is not None else budgets.escalate_below_score
    magnific_first = visual.type in MAGNIFIC_FIRST_TYPES and visual.allow_magnific

    # Illustration slots: skip "accept mediocre stock" — go Magnific library/unlimited.
    if magnific_first and magnific_available:
        if best >= accept_at and not hero and visual.type == "image":
            return Decision("accept_free", f"free stock scored {best:.2f} ≥ {accept_at:.2f}", hero)
        return Decision(
            "magnific_library",
            f"magnific-first type={visual.type}; free best {best:.2f}",
            hero,
        )

    if best >= accept_at:
        return Decision("accept_free", f"free stock scored {best:.2f} ≥ {accept_at:.2f}", hero)

    premium_native = visual.type in {"motion_graphics", "infographic", "generated"}

    if best >= escalate_below and not premium_native and not hero:
        return Decision(
            "accept_free",
            f"free stock scored {best:.2f}, above the {escalate_below:.2f} escalation line",
            hero,
        )

    if not visual.allow_magnific:
        if best > 0:
            return Decision("accept_free", f"magnific disabled for this visual; best free {best:.2f}", hero)
        return Decision("manual_review", "magnific disabled and free search found nothing", hero)

    if magnific_available:
        return Decision(
            "magnific_library",
            (
                f"free stock scored {best:.2f} (< {accept_at:.2f})"
                + (", premium-native visual type" if premium_native else "")
            ),
            hero,
        )

    if grok_available and visual.allow_generative:
        return Decision("grok_fallback", "magnific unavailable, using the reserve generator", hero)

    if best > 0:
        return Decision("accept_free", f"nothing better available; best free {best:.2f}", hero)
    return Decision("manual_review", "no usable material from any source", hero)


def after_library_miss(
    visual: Visual,
    outcome: SearchOutcome,
    budget: TokenBudget,
    *,
    hero: bool,
    grok_available: bool,
) -> Decision:
    """Follow-up decision when the Magnific library came back empty."""
    if not visual.allow_generative:
        return _fallback(outcome, hero, "generation disabled for this visual")

    cost_hint = 2 if visual.type in {"footage", "motion_graphics"} else 1
    allowed, why = budget.can_spend(visual.id, cost_hint, hero=hero)
    if allowed:
        return Decision("magnific_generate", "library empty, generating", hero)
    if grok_available:
        return Decision("grok_fallback", f"{why}; using the reserve generator", hero)
    return _fallback(outcome, hero, why)


def _fallback(outcome: SearchOutcome, hero: bool, reason: str) -> Decision:
    if outcome.best_score > 0:
        return Decision(
            "accept_free", f"{reason}; falling back to best free ({outcome.best_score:.2f})", hero
        )
    return Decision("manual_review", reason, hero)
