from __future__ import annotations

from pathlib import Path

import pytest

from shorts_factory.media.download import LocalAsset
from shorts_factory.qa.gate import QAReport, VisualQA, combine
from shorts_factory.qa.native import NativeVerdict, check_native, domains_of
from shorts_factory.qa.vision import VisionVerdict, _parse
from shorts_factory.search.keywords import build_query_plan

from .test_search import make_candidate, make_visual


def asset_for(**kwargs) -> LocalAsset:
    return LocalAsset(candidate=make_candidate(**kwargs), path=Path("/tmp/does-not-matter.mp4"))


# --------------------------------------------------------------------------- #
# Level 1 — native
# --------------------------------------------------------------------------- #


def test_matching_asset_passes():
    visual = make_visual()
    plan = build_query_plan(visual)
    verdict = check_native(asset_for(), visual, plan, "Венера — планета, которая убивает.")
    assert verdict.passed
    assert verdict.score > 0.5


def test_juice_can_is_rejected_for_a_space_script():
    """The canonical failure this gate exists to catch."""
    visual = make_visual(query="planet venus space")
    plan = build_query_plan(visual)
    juice = asset_for(
        external_id="juice",
        title="fresh orange juice can on a kitchen table",
        tags=["juice", "drink", "beverage", "food", "kitchen"],
    )
    verdict = check_native(juice, visual, plan, "Венера — самая жестокая планета Солнечной системы.")
    assert not verdict.passed
    assert any("reads as" in issue for issue in verdict.issues)


def test_banned_subject_is_fatal():
    visual = make_visual(must_avoid=["cartoon"])
    plan = build_query_plan(visual)
    verdict = check_native(asset_for(title="cartoon planet venus in space"), visual, plan, "Планета Венера.")
    assert not verdict.passed
    assert verdict.score == 0.0


def test_missing_required_subject_costs_score():
    visual = make_visual(must_include=["temperature"])
    plan = build_query_plan(visual)
    with_term = check_native(
        asset_for(title="venus temperature chart", tags=["temperature", "venus"]), visual, plan, "Венера"
    )
    without_term = check_native(asset_for(), visual, plan, "Венера")
    assert with_term.score > without_term.score


def test_low_resolution_is_flagged():
    visual = make_visual()
    plan = build_query_plan(visual)
    verdict = check_native(asset_for(width=640, height=360), visual, plan, "Венера, планета")
    assert any("resolution" in issue for issue in verdict.issues)


def test_ultrawide_source_is_flagged():
    visual = make_visual()
    plan = build_query_plan(visual)
    verdict = check_native(asset_for(width=3840, height=1080), visual, plan, "Венера, планета")
    assert any("ultra-wide" in issue for issue in verdict.issues)


def test_asset_without_metadata_defers_instead_of_blocking():
    visual = make_visual()
    plan = build_query_plan(visual)
    verdict = check_native(asset_for(title="", tags=[]), visual, plan, "Венера, планета")
    assert verdict.passed
    assert any("deferring" in note for note in verdict.notes)


def test_abstract_motion_never_conflicts():
    visual = make_visual(type="motion_graphics", query="abstract particles loop")
    plan = build_query_plan(visual)
    verdict = check_native(
        asset_for(title="abstract particles loop gradient", tags=["abstract", "particles", "loop"]),
        visual,
        plan,
        "Причина — углекислый газ в атмосфере.",
    )
    assert verdict.passed


@pytest.mark.parametrize(
    "text,expected",
    [
        ("planet venus orbit space", "space"),
        ("orange juice bottle drink", "food"),
        ("server data network cyber", "tech"),
    ],
)
def test_domain_detection(text, expected):
    assert expected in domains_of(text)


def test_russian_narration_maps_into_the_lexicon():
    assert "space" in domains_of("Венера — планета, орбита, космос")


# --------------------------------------------------------------------------- #
# Level 2 — vision parsing
# --------------------------------------------------------------------------- #


def test_vision_reply_is_parsed_from_a_code_fence():
    reply = """```json
    {"on_topic": 0.9, "distractors": [], "quality": 0.8, "coherent": true,
     "verdict": "pass", "reason": "Venus surface, on topic"}
    ```"""
    verdict = _parse(reply)
    assert verdict.verdict == "pass"
    assert verdict.on_topic == pytest.approx(0.9)
    assert verdict.score > 0.8


def test_non_json_reply_becomes_manual_not_pass():
    verdict = _parse("Looks fine to me!")
    assert verdict.verdict == "manual"
    assert not verdict.passed


def test_unknown_verdict_value_becomes_manual():
    verdict = _parse('{"verdict": "looks-good", "on_topic": 1}')
    assert verdict.verdict == "manual"


def test_out_of_range_scores_are_clamped():
    verdict = _parse('{"verdict": "pass", "on_topic": 5, "quality": -3}')
    assert verdict.on_topic == 1.0
    assert verdict.quality == 0.0


# --------------------------------------------------------------------------- #
# Combining the two levels
# --------------------------------------------------------------------------- #


def test_native_failure_short_circuits():
    outcome = combine(
        NativeVerdict(passed=False, score=0.1),
        VisionVerdict(verdict="pass", on_topic=1.0),
        require_vision=True,
        manual_review_ok=True,
    )
    assert outcome == "rejected"


def test_both_levels_passing_accepts():
    outcome = combine(
        NativeVerdict(passed=True, score=0.9),
        VisionVerdict(verdict="pass"),
        require_vision=True,
        manual_review_ok=True,
    )
    assert outcome == "accepted"


def test_vision_replace_rejects_even_after_a_native_pass():
    outcome = combine(
        NativeVerdict(passed=True, score=0.9),
        VisionVerdict(verdict="replace", reason="a juice can is in frame"),
        require_vision=True,
        manual_review_ok=True,
    )
    assert outcome == "rejected"


def test_unavailable_vision_holds_for_review_when_required():
    outcome = combine(
        NativeVerdict(passed=True, score=0.9),
        VisionVerdict(verdict="manual", skipped=True, reason="no key"),
        require_vision=True,
        manual_review_ok=True,
    )
    assert outcome == "manual_review"


def test_unavailable_vision_accepts_when_not_required():
    outcome = combine(
        NativeVerdict(passed=True, score=0.9),
        VisionVerdict(verdict="manual", skipped=True),
        require_vision=False,
        manual_review_ok=True,
    )
    assert outcome == "accepted"


def test_manual_review_disabled_turns_into_rejection():
    outcome = combine(
        NativeVerdict(passed=True, score=0.9),
        VisionVerdict(verdict="manual"),
        require_vision=True,
        manual_review_ok=False,
    )
    assert outcome == "rejected"


def test_report_summary_counts_every_bucket():
    report = QAReport()
    report.add(VisualQA(visual_id="v1", outcome="accepted", vision=VisionVerdict(verdict="pass")))
    report.add(VisualQA(visual_id="v2", outcome="manual_review"))
    report.add(VisualQA(visual_id="v3", outcome="rejected"))
    assert len(report.accepted) == 1
    assert len(report.manual_review) == 1
    assert len(report.rejected) == 1
    assert report.vision_checked == 1
    assert not report.is_renderable()
    assert "1 accepted" in report.summary()
