"""Meme policy: frequency, forbidden rubrics, allowed beats."""

from __future__ import annotations

from shorts_factory.assets.meme_policy import (
    MemeHistory,
    MemePolicyConfig,
    decide_meme,
    ensure_meme_visual,
    find_meme_window,
)
from shorts_factory.spec import parse_spec

from .conftest import minimal_document


def _spec(**overrides):
    base = {
        "rubric": "IT",
        "memes": {"enabled": True, "tags": ["shock"], "max_duration": 1.5},
    }
    base.update(overrides)
    document = minimal_document(**base)
    # Need enough body segments for core1_to_core2 / context_end windows.
    document["duration_target"] = 30
    document["script"] = [
        {"id": "s1", "text": "Context beat sets the scene for the viewer.", "start": 2.5, "duration": 6},
        {"id": "s2", "text": "Core one delivers the first hard fact.", "start": 8.5, "duration": 6},
        {"id": "s3", "text": "Core two flips the expectation hard.", "start": 14.5, "duration": 6},
        {"id": "s4", "text": "Payoff lands before the call to action.", "start": 20.5, "duration": 5},
    ]
    document["visuals"] = [
        {"id": "v1", "type": "footage", "query": "server room", "start": 0, "duration": 10},
        {"id": "v2", "type": "footage", "query": "chip closeup", "start": 10, "duration": 10},
        {"id": "v3", "type": "footage", "query": "data center", "start": 20, "duration": 10},
    ]
    document["cta"] = {"text": "Подпишись", "start": 26, "duration": 3}
    spec, _ = parse_spec(document)
    return spec


def test_science_rubric_forbids_memes():
    spec = _spec(rubric="наука")
    policy = MemePolicyConfig()
    history = MemeHistory(videos=["a"] * 20, meme_at=[])
    decision = decide_meme(spec, policy, history)
    assert not decision.allowed
    assert "forbids" in decision.reason


def test_frequency_gate_blocks_until_enough_videos_pass():
    spec = _spec()
    policy = MemePolicyConfig(frequency=10)
    history = MemeHistory(videos=["v1", "v2", "v3"], meme_at=["v1"])
    decision = decide_meme(spec, policy, history)
    assert not decision.allowed
    assert "frequency" in decision.reason


def test_frequency_gate_allows_after_gap():
    spec = _spec()
    policy = MemePolicyConfig(frequency=10)
    # 8 videos since last meme → min_gap = 8 → allow
    history = MemeHistory(
        videos=["old"] + [f"v{i}" for i in range(8)],
        meme_at=["old"],
    )
    decision = decide_meme(spec, policy, history)
    assert decision.allowed
    assert decision.beat in {"context_end", "core1_to_core2"}


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
    history = MemeHistory(videos=["x"] * 5, meme_at=[])
    decision = decide_meme(spec, policy, history)
    assert decision.allowed
    visual = ensure_meme_visual(spec, decision)
    assert visual is not None
    assert visual.type == "meme"
    assert sum(1 for item in spec.visuals if item.type == "meme") == 1
    # Second call is a no-op when a meme already exists.
    assert ensure_meme_visual(spec, decision) is None


def test_disabled_scenario_memes_are_skipped():
    spec = _spec(memes={"enabled": False, "tags": ["shock"]})
    decision = decide_meme(spec, MemePolicyConfig(), MemeHistory(videos=["a"] * 20))
    assert not decision.allowed
