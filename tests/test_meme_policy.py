"""Meme policy: frequency, forbidden rubrics, irony beats."""

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
    document["hook"] = {
        "id": "hook",
        "text": "Можно ли выжить внутри чёрной дыры?",
        "start": 0,
        "duration": 4.0,
        "emphasis": "high",
        "mode": "full_host",
    }
    document["script"] = [
        {
            "id": "s1",
            "text": "Кажется, горизонт событий — просто тёмная стена. Это миф.",
            "start": 4.0,
            "duration": 5.5,
            "mode": "split",
        },
        {
            "id": "s2",
            "text": "Приливные силы рвут тело ещё до центра. Давление — как миллиард атмосфер.",
            "start": 9.5,
            "duration": 6.0,
            "emphasis": "high",
            "mode": "full_footage",
        },
        {
            "id": "s3",
            "text": "А в центре классическая физика уже не работает.",
            "start": 15.5,
            "duration": 5.5,
            "mode": "split",
        },
        {
            "id": "s4",
            "text": "Так что ответ короткий: выжить нельзя. И это естественно.",
            "start": 21.0,
            "duration": 5.5,
            "mode": "full_host",
        },
    ]
    document["visuals"] = [
        {
            "id": "v1",
            "type": "footage",
            "query": "black hole accretion",
            "start": 0,
            "duration": 2.0,
            "position": "auto",
        },
        {
            "id": "v2",
            "type": "footage",
            "query": "event horizon",
            "start": 2.0,
            "duration": 2.0,
            "position": "auto",
        },
        {
            "id": "v3",
            "type": "infographic",
            "query": "tidal forces diagram",
            "start": 4.0,
            "duration": 2.5,
            "position": "auto",
        },
        {
            "id": "v4",
            "type": "footage",
            "query": "spacetime warp",
            "start": 6.5,
            "duration": 2.5,
            "position": "auto",
        },
        {
            "id": "v5",
            "type": "footage",
            "query": "neutron star crush",
            "start": 9.5,
            "duration": 3.0,
            "position": "fullscreen",
        },
        {
            "id": "v6",
            "type": "footage",
            "query": "gravity well",
            "start": 12.5,
            "duration": 3.0,
            "position": "fullscreen",
        },
        {
            "id": "v7",
            "type": "footage",
            "query": "singularity abstract",
            "start": 15.5,
            "duration": 2.5,
            "position": "auto",
        },
        {
            "id": "v8",
            "type": "footage",
            "query": "event horizon glow",
            "start": 18.0,
            "duration": 3.0,
            "position": "auto",
        },
        {
            "id": "v9",
            "type": "footage",
            "query": "black hole silhouette",
            "start": 21.0,
            "duration": 2.5,
            "position": "auto",
        },
        {
            "id": "v10",
            "type": "footage",
            "query": "accretion disk flare",
            "start": 23.5,
            "duration": 3.0,
            "position": "auto",
        },
    ]
    document["duration_target"] = 28
    document["cta"] = {"text": "Подпишись", "start": 24, "duration": 3.5}
    spec, _ = parse_spec(document)
    return spec


def test_medicine_rubric_forbids_memes():
    spec = _spec(rubric="медицина")
    policy = MemePolicyConfig()
    history = MemeHistory(videos=["a"] * 20, meme_at=[])
    decision = decide_meme(spec, policy, history)
    assert not decision.allowed
    assert "forbids" in decision.reason


def test_science_rubric_allows_irony_memes():
    spec = _spec(rubric="наука")
    policy = MemePolicyConfig(frequency=1)
    history = MemeHistory(videos=["x"] * 5, meme_at=[])
    decision = decide_meme(spec, policy, history)
    assert decision.allowed
    assert decision.beat in {
        "hook_punch",
        "misconception",
        "absurd_scale",
        "deadpan_accept",
        "reveal_twist",
        "context_end",
        "core1_to_core2",
    }


def test_frequency_gate_blocks_until_enough_videos_pass():
    spec = _spec()
    policy = MemePolicyConfig(frequency=4)
    history = MemeHistory(videos=["v1", "v2"], meme_at=["v1"])
    decision = decide_meme(spec, policy, history)
    assert not decision.allowed
    assert "frequency" in decision.reason


def test_frequency_gate_allows_after_gap():
    spec = _spec()
    policy = MemePolicyConfig(frequency=4)
    history = MemeHistory(
        videos=["old"] + [f"v{i}" for i in range(3)],
        meme_at=["old"],
    )
    decision = decide_meme(spec, policy, history)
    assert decision.allowed


def test_hook_punch_is_preferred_for_question_hooks():
    spec = _spec()
    policy = MemePolicyConfig(frequency=1)
    ranked = rank_beats(spec, policy)
    assert ranked
    assert ranked[0][0] == "hook_punch"
    assert ranked[0][1] >= (spec.hook.end if spec.hook else 0)


def test_meme_never_lands_in_climax():
    spec = _spec()
    policy = MemePolicyConfig()
    window = find_meme_window(spec, policy)
    assert window is not None
    beat, start, duration = window
    assert start + duration <= spec.cta.start


def test_ensure_meme_visual_inserts_once():
    spec = _spec()
    policy = MemePolicyConfig(frequency=1)
    history = MemeHistory(videos=["x"] * 5, meme_at=[])
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
    decision = decide_meme(spec, MemePolicyConfig(frequency=4), MemeHistory())
    assert decision.allowed
    assert decision.beat == "hook_punch"
