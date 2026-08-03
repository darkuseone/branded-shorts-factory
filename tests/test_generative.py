from __future__ import annotations

import pytest

from shorts_factory.config import Budgets
from shorts_factory.errors import BudgetExceededError
from shorts_factory.generative.budget import TokenBudget
from shorts_factory.generative.magnific import _extract_url, prompt_for
from shorts_factory.generative.policy import after_library_miss, decide
from shorts_factory.search.aggregator import SearchOutcome
from shorts_factory.search.keywords import build_query_plan
from shorts_factory.search.ranking import RankedCandidate
from shorts_factory.spec import Visual

from .test_search import make_candidate, make_visual

BUDGETS = Budgets(magnific_tokens=10, magnific_reserve=4, magnific_max_per_visual=2)


def outcome_with(score: float, visual: Visual) -> SearchOutcome:
    plan = build_query_plan(visual)
    result = SearchOutcome(visual_id=visual.id, plan=plan)
    if score > 0:
        result.ranked = [
            RankedCandidate(
                candidate=make_candidate(),
                score=score,
                relevance=score,
                quality=score,
                fit=score,
                source_bonus=1.0,
            )
        ]
    return result


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_reserve_is_held_back_for_hero_slots():
    budget = TokenBudget.from_budgets(BUDGETS)
    assert budget.free_pool == 6
    budget.spend("v1", 2, "magnific:premium", "test")
    budget.spend("v2", 2, "magnific:premium", "test")
    budget.spend("v3", 2, "magnific:premium", "test")
    assert budget.free_pool == 0
    allowed, reason = budget.can_spend("v4", 1)
    assert not allowed and "non-reserved" in reason
    hero_allowed, _ = budget.can_spend("v4", 1, hero=True)
    assert hero_allowed


def test_per_visual_cap_is_enforced():
    budget = TokenBudget.from_budgets(BUDGETS)
    budget.spend("v1", 2, "magnific:premium", "first")
    with pytest.raises(BudgetExceededError):
        budget.spend("v1", 1, "magnific:premium", "second")


def test_free_tier_is_ledgered_without_spending():
    budget = TokenBudget.from_budgets(BUDGETS)
    budget.record_free("v1", "magnific:unlimited", "flux")
    assert budget.spent == 0
    assert budget.entries[0].tokens == 0
    assert budget.to_dict()["remaining"] == 10


def test_scenario_budget_override():
    budget = TokenBudget.from_budgets(BUDGETS, override_total=3)
    assert budget.total == 3
    assert budget.reserve <= 3


# --------------------------------------------------------------------------- #
# Escalation policy
# --------------------------------------------------------------------------- #


def test_strong_free_result_is_accepted_without_spending():
    visual = make_visual()
    decision = decide(
        visual,
        outcome_with(0.88, visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=True,
        grok_available=True,
    )
    assert decision.action == "accept_free"


def test_mediocre_free_result_is_accepted_for_normal_priority():
    visual = make_visual()
    decision = decide(
        visual,
        outcome_with(0.70, visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=True,
        grok_available=True,
    )
    assert decision.action == "accept_free"


def test_weak_free_result_escalates_to_the_free_magnific_library_first():
    visual = make_visual()
    decision = decide(
        visual,
        outcome_with(0.35, visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=True,
        grok_available=True,
    )
    assert decision.action == "magnific_library", "the paid tier is never the first escalation"


def test_motion_graphics_escalate_even_on_a_decent_free_score():
    visual = make_visual(type="motion_graphics")
    decision = decide(
        visual,
        outcome_with(0.70, visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=True,
        grok_available=True,
    )
    assert decision.action == "magnific_library"


def test_hero_slot_escalates_where_a_normal_slot_would_not():
    weak = 0.70
    normal = decide(
        make_visual(),
        outcome_with(weak, make_visual()),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=True,
        grok_available=True,
    )
    hero_visual = make_visual(priority="critical")
    hero = decide(
        hero_visual,
        outcome_with(weak, hero_visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=True,
        grok_available=True,
    )
    assert normal.action == "accept_free"
    assert hero.action == "magnific_library" and hero.hero


def test_allow_magnific_false_is_respected():
    visual = make_visual(allow_magnific=False)
    decision = decide(
        visual,
        outcome_with(0.30, visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=True,
        grok_available=True,
    )
    assert decision.action == "accept_free"


def test_nothing_anywhere_becomes_manual_review():
    visual = make_visual(allow_magnific=False)
    decision = decide(
        visual,
        outcome_with(0.0, visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=False,
        grok_available=False,
    )
    assert decision.action == "manual_review"


def test_grok_is_the_reserve_when_magnific_is_unavailable():
    visual = make_visual()
    decision = decide(
        visual,
        outcome_with(0.30, visual),
        TokenBudget.from_budgets(BUDGETS),
        BUDGETS,
        magnific_available=False,
        grok_available=True,
    )
    assert decision.action == "grok_fallback"


def test_library_miss_falls_back_to_grok_when_the_budget_is_gone():
    visual = make_visual()
    budget = TokenBudget.from_budgets(Budgets(magnific_tokens=0, magnific_reserve=0))
    decision = after_library_miss(visual, outcome_with(0.3, visual), budget, hero=False, grok_available=True)
    assert decision.action == "grok_fallback"


def test_library_miss_generates_when_the_budget_allows():
    visual = make_visual()
    decision = after_library_miss(
        visual,
        outcome_with(0.3, visual),
        TokenBudget.from_budgets(BUDGETS),
        hero=False,
        grok_available=True,
    )
    assert decision.action == "magnific_generate"


# --------------------------------------------------------------------------- #
# Prompting and response parsing
# --------------------------------------------------------------------------- #


def test_prompt_anchors_on_the_narration_and_the_9_by_16_frame():
    prompt = prompt_for("black hole", "Свет не может вырваться наружу.", "cinematic render", ["text"])
    assert "black hole" in prompt
    assert "9:16" in prompt
    assert "avoid: text" in prompt


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"output_url": "https://x/a.mp4"}, "https://x/a.mp4"),
        ({"result": {"url": "https://x/b.png"}}, "https://x/b.png"),
        ({"data": ["https://x/c.mp4"]}, "https://x/c.mp4"),
        ({"assets": [{"video_url": "https://x/d.mp4"}]}, "https://x/d.mp4"),
        ({"status": "processing"}, None),
    ],
)
def test_generation_url_extraction_tolerates_shapes(payload, expected):
    assert _extract_url(payload) == expected
