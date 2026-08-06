"""Meme policy: frequency ~1/10, forbidden science+medicine, seam-only beats."""

from __future__ import annotations

from shorts_factory.assets.meme_policy import (
    MemeHistory,
    MemePolicyConfig,
    decide_meme,
    ensure_meme_visual,
    find_meme_window,
    rank_beats,
)
from shorts_factory.spec import parse_spec

from .conftest import minimal_document


def _spec(**overrides):
    base = {
        "rubric": "космос",
        "memes": {"enabled": True, "tags": ["шок"], "max_duration": 1.4},
    }
    base.update(overrides)
    document = minimal_document(**base)
    document["duration_target"] = 45
    document["hook"] = {
        "id": "hook",
        "text": "Можно ли выжить внутри чёрной дыры?",
        "start": 0,
        "duration": 2.8,
        "emphasis": "high",
    }
    document["script"] = [
        {
            "id": "s1",
            "text": "Кажется, горизонт событий — просто тёмная стена. Это миф.",
            "start": 2.8,
            "duration": 6,
        },
        {
            "id": "s2",
            "text": "Приливные силы рвут тело ещё до центра. Давление — как миллиард атмосфер.",
            "start": 8.8,
            "duration": 7,
            "emphasis": "high",
        },
        {
            "id": "s3",
            "text": "А в центре классическая физика уже не работает.",
            "start": 15.8,
            "duration": 6,
        },
        {
            "id": "s4",
            "text": "Так что ответ короткий: выжить нельзя. И это естественно.",
            "start": 21.8,
            "duration": 6,
        },
    ]
    document["visuals"] = [
        {"id": "v1", "type": "footage", "query": "black hole accretion", "start": 0, "duration": 10},
        {"id": "v2", "type": "infographic", "query": "tidal forces diagram", "start": 10, "duration": 10},
        {"id": "v3", "type": "footage", "query": "spacetime warp", "start": 20, "duration": 12},
        {"id": "v4", "type": "footage", "query": "event horizon glow", "start": 32, "duration": 13},
    ]
    document["cta"] = {"text": "Подпишись", "start": 40, "duration": 4}
    spec, _ = parse_spec(document)
    return spec


def test_medicine_rubric_forbids_memes():
    spec = _spec(rubric="медицина")
    policy = MemePolicyConfig()
    history = MemeHistory(videos=["a"] * 20, meme_at=[])
    decision = decide_meme(spec, policy, history)
    assert not decision.allowed
    assert "forbids" in decision.reason


def test_science_rubric_forbids_memes_by_default():
    spec = _spec(rubric="наука")
    policy = MemePolicyConfig(frequency=1)
    history = MemeHistory(videos=["x"] * 20, meme_at=[])
    decision = decide_meme(spec, policy, history)
    assert not decision.allowed
    assert "forbids" in decision.reason


def test_frequency_gate_blocks_until_enough_videos_pass():
    spec = _spec()
    policy = MemePolicyConfig(frequency=10)
    history = MemeHistory(videos=["v1", "v2"], meme_at=["v1"])
    decision = decide_meme(spec, policy, history)
    assert not decision.allowed
    assert "frequency" in decision.reason


def test_frequency_gate_allows_after_gap():
    spec = _spec()
    policy = MemePolicyConfig(frequency=10)
    history = MemeHistory(
        videos=["old"] + [f"v{i}" for i in range(9)],
        meme_at=["old"],
    )
    decision = decide_meme(spec, policy, history)
    assert decision.allowed


def test_only_context_or_core_seam_beats():
    spec = _spec()
    policy = MemePolicyConfig(frequency=1)
    ranked = rank_beats(spec, policy)
    assert ranked
    assert ranked[0][0] in {"context_end", "core1_to_core2"}
    assert ranked[0][1] >= (spec.hook.end if spec.hook else 0)


def test_meme_never_lands_in_hook_or_climax():
    spec = _spec()
    policy = MemePolicyConfig()
    window = find_meme_window(spec, policy)
    assert window is not None
    beat, start, duration = window
    assert start >= (spec.hook.end if spec.hook else 0)
    assert start + duration <= spec.cta.start


def test_ensure_meme_visual_inserts_once():
    spec = _spec()
    policy = MemePolicyConfig(frequency=1)
    history = MemeHistory(videos=["x"] * 20, meme_at=[])
    decision = decide_meme(spec, policy, history)
    assert decision.allowed
    visual = ensure_meme_visual(spec, decision)
    assert visual is not None
    assert visual.type == "meme"
    assert "auto-irony" in visual.notes
    assert sum(1 for item in spec.visuals if item.type == "meme") == 1
    assert ensure_meme_visual(spec, decision) is None


def test_disabled_scenario_memes_are_skipped():
    spec = _spec(memes={"enabled": False, "tags": ["шок"]})
    decision = decide_meme(spec, MemePolicyConfig(), MemeHistory(videos=["a"] * 20))
    assert not decision.allowed


def test_cold_start_allows_meme_on_empty_history():
    spec = _spec()
    decision = decide_meme(spec, MemePolicyConfig(frequency=10), MemeHistory())
    assert decision.allowed
    assert decision.beat in {"context_end", "core1_to_core2"}
