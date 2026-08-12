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
    """Same asset either way, so the difference measured is the penalty.

    This used to compare two different titles, one of which was simply a
    better subject match — which measured subject coverage, not the
    must_include penalty, and inverted as soon as coverage was recalibrated.
    """
    visual = make_visual(must_include=["temperature"])
    plan = build_query_plan(visual)
    with_term = check_native(
        asset_for(title="planet venus in space", tags=["temperature"]), visual, plan, "Венера"
    )
    without_term = check_native(
        asset_for(title="planet venus in space", tags=["surface"]), visual, plan, "Венера"
    )
    assert with_term.score > without_term.score
    assert any("temperature" in issue for issue in without_term.issues)
    assert not any("temperature" in issue for issue in with_term.issues)


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


def test_asset_without_metadata_is_never_shipped_unseen():
    """It may pass level 1, but only on the promise that level 2 looks at it.

    The first cut opened with a hair salon. Nothing in the pipeline had ever
    looked at that frame: the metadata was too thin to judge, level 1 waved it
    through "deferring to the vision gate", and in that run the vision gate
    never ran. Deferring to something that is not there is just accepting.
    """
    visual = make_visual()
    plan = build_query_plan(visual)
    verdict = check_native(asset_for(title="", tags=[]), visual, plan, "Венера, планета")
    assert verdict.passed
    assert verdict.needs_vision
    assert combine(verdict, None, require_vision=False, manual_review_ok=True) == "rejected"
    assert (
        combine(
            verdict,
            VisionVerdict(verdict="manual", skipped=True),
            require_vision=False,
            manual_review_ok=True,
        )
        == "rejected"
    )
    assert (
        combine(verdict, VisionVerdict(verdict="pass"), require_vision=False, manual_review_ok=True)
        == "accepted"
    )


#: Titles in the shape stock libraries actually return, for the two slots that
#: opened the first cut: `v01 "openai office sign"` and `v02 "code repository
#: screen"`. The rejects are the class of thing that got on screen — a salon,
#: a stranger's face — and the passes are what those slots are for. Both halves
#: matter: a gate that rejects everything also produces a video of backdrops.
OFF_TOPIC_TITLES = [
    "woman at hair salon",
    "a woman getting her hair done in a salon",
    "barber trimming a beard in a barbershop",
    "family having dinner at a restaurant kitchen table",
    "wedding couple dancing at a party",
]

#: Paired with the slot query they answer — a title is only on-topic for a
#: question, and judging all of them against one merged query measures nothing.
ON_TOPIC_TITLES = [
    ("openai office sign", "modern tech company office sign"),
    ("openai office sign", "openai logo on a building facade"),
    ("code repository screen", "programmer writing code on a screen"),
    ("code repository screen", "source code scrolling on a monitor"),
    ("data center corridor", "rows of servers in a data center"),
    # Right subject, no shared words. A lexical gate cannot settle these, so
    # they have to reach the gate that can see the frame rather than being
    # thrown out — eleven slots went unfilled in one run for exactly this.
    ("code repository screen", "portrait of a developer at a workstation"),
    ("security operations center", "smiling engineer working on a laptop"),
]


@pytest.mark.parametrize("title", OFF_TOPIC_TITLES)
def test_a_domestic_scene_never_illustrates_an_it_story(title):
    visual = make_visual(query="openai office sign")
    plan = build_query_plan(visual)
    verdict = check_native(
        asset_for(title=title, tags=title.split()),
        visual,
        plan,
        "OpenAI модель сама взломала Hugging Face. Не хакер. Модель.",
    )
    assert not verdict.passed, f"{title!r} scored {verdict.score:.3f} and would go on screen"


@pytest.mark.parametrize(("query", "title"), ON_TOPIC_TITLES)
def test_the_gate_still_lets_the_right_footage_through(query, title):
    visual = make_visual(query=query)
    plan = build_query_plan(visual)
    verdict = check_native(
        asset_for(title=title, tags=title.split()),
        visual,
        plan,
        "OpenAI модель сама взломала Hugging Face.",
    )
    assert verdict.passed, f"{title!r} scored only {verdict.score:.3f}: {verdict.issues}"


def test_one_generic_word_in_common_is_not_a_match():
    """ "office" is shared by an OpenAI headquarters and by a stranger's corridor.

    A single shared noun used to clear the bar outright, helped over it by a
    domain bonus awarded for the same one word. It may now go no further than
    the gate that can see the frame.
    """
    visual = make_visual(query="openai office sign")
    plan = build_query_plan(visual)
    verdict = check_native(
        asset_for(title="people walking in an office corridor", tags=["office", "corridor"]),
        visual,
        plan,
        "OpenAI модель сама взломала Hugging Face.",
    )
    assert not verdict.passed or verdict.needs_vision
    assert combine(verdict, None, require_vision=False, manual_review_ok=True) == "rejected"


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


# --------------------------------------------------------------------------- #
# Calibration — the gate must not argue with its own boilerplate
# --------------------------------------------------------------------------- #


def test_subject_coverage_saturates_instead_of_demanding_every_word():
    """A stock title is a few words; a six-word query cannot be echoed whole.

    Nineteen of twenty-five slots came back empty with an empty issue list —
    nothing was wrong with the assets, the arithmetic simply could not reach
    the threshold for a multi-word query.
    """
    visual = make_visual(query="security operations center", keywords=[])
    plan = build_query_plan(visual)
    verdict = check_native(
        asset_for(title="Security Room With Monitors", tags=["security", "operations", "monitor"]),
        visual,
        plan,
        "Центр мониторинга безопасности.",
    )
    assert verdict.passed, f"score {verdict.score:.3f}, issues {verdict.issues}"


def test_expander_modifiers_are_not_part_of_the_bar():
    """The search layer appends "well lit laboratory" to footage queries.

    Scoring assets against that made an AI-security script read as a science
    lab, after which every honest tech clip was rejected for "reading as
    tech".
    """
    visual = make_visual(query="hacker laptop dark", keywords=["penetration testing"], type="footage")
    plan = build_query_plan(visual)
    assert any("laboratory" in q for q in plan.queries), "modifier still expected in the fan"
    assert not any("laboratory" in term for term in plan.author_terms)

    verdict = check_native(
        asset_for(title="Man Typing On Laptop In Dark Room", tags=["laptop", "hacker", "dark"]),
        visual,
        plan,
        "Их тестировали на бенчмарке по взлому.",
    )
    assert verdict.passed, f"score {verdict.score:.3f}, issues {verdict.issues}"


def test_the_lexicon_cannot_veto_an_asset_that_matches_the_query():
    """One stray word swings the classifier: "chart" reads as finance."""
    visual = make_visual(query="venus temperature", keywords=[])
    plan = build_query_plan(visual)
    verdict = check_native(
        asset_for(title="venus temperature chart", tags=["venus", "temperature"]),
        visual,
        plan,
        "Венера",
    )
    assert not any("reads as" in issue for issue in verdict.issues)


def test_an_off_topic_asset_is_still_rejected():
    """The calibration must not have turned the gate into a rubber stamp."""
    visual = make_visual(query="security operations center", keywords=[])
    plan = build_query_plan(visual)
    for title, tags in (
        ("Woman Doing Yoga On The Beach", ["yoga", "beach", "wellness"]),
        ("Fresh Vegetables On A Wooden Table", ["food", "cooking", "kitchen"]),
    ):
        verdict = check_native(asset_for(title=title, tags=tags), visual, plan, "Центр мониторинга.")
        assert not verdict.passed, f"{title} scored {verdict.score:.3f}"


def test_a_person_in_a_tech_scene_is_not_a_domestic_scene():
    """The stop-list must not stop the footage we are looking for.

    "portrait", "smiling" and "posing" were on it for one run, and eleven
    slots came back empty: stock titles for technology b-roll are full of
    "portrait of a developer" and "smiling engineer at a workstation". The
    veto now needs two words that name a domestic scene outright.
    """
    visual = make_visual(query="security operations center")
    plan = build_query_plan(visual)
    for title in ("portrait of a developer at a workstation", "smiling engineer working on a laptop"):
        verdict = check_native(
            asset_for(title=title, tags=title.split()), visual, plan, "ИИ-агент атакует инфраструктуру."
        )
        assert not any("never illustrates" in issue for issue in verdict.issues), (
            f"{title!r} was vetoed as a domestic scene: {verdict.issues}"
        )


# --------------------------------------------------------------------------- #
# Vision transport
# --------------------------------------------------------------------------- #


def test_a_model_name_the_provider_rejects_is_not_fatal():
    """One wrong model name cost a whole run its footage.

    The default was `grok-4-vision`, which xAI has never had. Every call
    answered "Model not found", so every slot that needed a look at the frame
    was left empty — eleven of them — and the video came out with half its
    b-roll missing. The name of somebody else's model is not something this
    pipeline can know for certain, so it moves down a ladder instead.
    """
    from shorts_factory.config import Settings
    from shorts_factory.errors import ProviderError
    from shorts_factory.qa.vision import MODEL_LADDER, GrokVisionGate

    settings = Settings.from_env({"GROK_API_KEY": "test-key", "GROK_VISION_MODEL": "made-up-model"})
    gate = GrokVisionGate(settings)
    tried: list[str] = []

    def fake_post(url, body, **kwargs):
        tried.append(body["model"])
        if body["model"] != MODEL_LADDER[1]:
            raise ProviderError("grok_vision", 'Bad Request: {"error":"Model not found: x"} (HTTP 400)')
        return {"choices": [{"message": {"content": '{"verdict": "pass"}'}}]}

    gate.client.post_json = fake_post

    reply = gate._post([{"type": "text", "text": "hello"}])

    assert reply["choices"], "the ladder never produced an answer"
    assert tried[0] == "made-up-model", "the configured model must be tried first"
    assert gate.model == MODEL_LADDER[1], "the working model is not remembered for the next call"
    assert len(tried) > 1


def test_an_ordinary_failure_still_raises():
    """The ladder is for unknown model names, not for every error."""
    from shorts_factory.config import Settings
    from shorts_factory.errors import ProviderError
    from shorts_factory.qa.vision import GrokVisionGate

    gate = GrokVisionGate(Settings.from_env({"GROK_API_KEY": "test-key"}))
    calls: list[str] = []

    def fake_post(url, body, **kwargs):
        calls.append(body["model"])
        raise ProviderError("grok_vision", "Unauthorized (HTTP 401)")

    gate.client.post_json = fake_post

    with pytest.raises(ProviderError):
        gate._post([{"type": "text", "text": "hello"}])
    assert len(calls) == 1, "a 401 must not walk the whole ladder"


# --------------------------------------------------------------------------- #
# Chroma-key assets
# --------------------------------------------------------------------------- #


def _solid(colour: str, target: Path) -> bool:
    """One flat-colour still, written with ffmpeg. False when it cannot be."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        return False
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c={colour}:s=320x568", "-frames:v", "1", str(target)],
        check=False, capture_output=True, timeout=60,
    )  # fmt: skip
    return target.exists()


def test_a_chroma_key_asset_is_refused(tmp_path):
    """A drawing on flat key green is not footage; it is unfinished material.

    Nothing in the metadata gives this away — the title is on topic, the tags
    match, the resolution is fine — so level 1 passes it and a slab of green
    fills half the screen. Only the frame tells the truth.
    """
    from shorts_factory.qa.chroma import looks_keyed

    frame = tmp_path / "keyed.png"
    if not _solid("0x00b140", frame):
        pytest.skip("needs ffmpeg to build the fixture")

    refused, share = looks_keyed(frame)
    assert refused, f"a full chroma-green frame passed the gate (share {share:.2f})"
    assert share > 0.9


def test_ordinary_footage_is_not_mistaken_for_a_key(tmp_path):
    """The test has to be strict enough that real colour survives it.

    Foliage, a green-lit room and a dark studio are all 'greenish' in the
    loose sense. A gate that refused them would empty the video to protect it.
    """
    from shorts_factory.qa.chroma import looks_keyed

    for colour in ("0x2f4f2f", "0x1a1a2e", "0x8fbc8f", "gray"):
        frame = tmp_path / f"{colour}.png"
        if not _solid(colour, frame):
            pytest.skip("needs ffmpeg to build the fixture")
        refused, share = looks_keyed(frame)
        assert not refused, f"{colour} was read as a chroma key (share {share:.2f})"


def test_an_unreadable_file_is_left_to_the_download_gate(tmp_path):
    """A frame that cannot be decoded is somebody else's failure to report."""
    from shorts_factory.qa.chroma import looks_keyed

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    refused, share = looks_keyed(broken)
    assert not refused and share == 0.0
