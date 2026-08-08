"""Smart placement: sparse, peak-aligned, density-capped."""

from __future__ import annotations

from pathlib import Path

import pytest

from shorts_factory.assets.library import LibraryItem, SfxLibrary
from shorts_factory.spec import (
    AudioFx,
    AvatarSettings,
    BrandElements,
    CaptionSettings,
    Constraints,
    MemeSettings,
    MusicSettings,
    ScriptSegment,
    Spec,
    Visual,
    VoiceSettings,
)
from shorts_factory.voice.audio_design import (
    align_start,
    apply_voice_guard,
    collect_beats,
    enforce_density,
    library_volume,
    resolve_from_library,
    suggest_audio_fx,
    trim_plan,
)


def _segment(id: str, start: float, duration: float = 5.0, emphasis: str = "normal") -> ScriptSegment:
    return ScriptSegment(id=id, text="слово " * 8, start=start, duration=duration, emphasis=emphasis)


def _visual(
    id: str,
    start: float,
    *,
    duration: float = 5.0,
    type: str = "footage",
    position: str = "fullscreen",
    priority: str = "normal",
) -> Visual:
    return Visual(
        id=id,
        type=type,
        query="planet",
        keywords=[],
        start=start,
        duration=duration,
        position=position,
        priority=priority,
    )


def _spec(*, script=None, visuals=None, avatar_segments=None, duration=40.0) -> Spec:
    hook = _segment("hook", 0.0, 3.0, emphasis="high")
    body = script or [
        _segment("s1", 3.0, 5.0),
        _segment("s2", 8.0, 5.0, emphasis="high"),
        _segment("s3", 13.0, 6.0),
        _segment("s4", 19.0, 6.0, emphasis="high"),
    ]
    visuals = visuals or [
        _visual("v1", 0.0, duration=3.0, priority="critical"),
        _visual("v2", 3.0, duration=5.0),
        _visual("v3", 8.0, duration=5.0, type="motion_graphics", priority="high"),
        _visual("v4", 13.0, duration=6.0),
        _visual("v5", 19.0, duration=6.0, type="infographic", priority="high"),
        _visual("v6", 25.0, duration=5.0),
    ]
    return Spec(
        id="test",
        title="Test",
        duration_target=duration,
        language="ru",
        topic="space",
        hook=hook,
        script=body,
        voice=VoiceSettings(),
        avatar=AvatarSettings(enabled=True, segments=avatar_segments or ["hook", "s4"]),
        visuals=visuals,
        music=MusicSettings(),
        audio_fx=[],
        captions=CaptionSettings(),
        brand=BrandElements(),
        cta=None,
        memes=MemeSettings(),
        constraints=Constraints(),
    )


# --------------------------------------------------------------------------- #
# Beat collection
# --------------------------------------------------------------------------- #


def test_author_effects_are_never_overridden():
    spec = _spec()
    spec.audio_fx = [AudioFx(type="whoosh", at=2.0)]
    assert suggest_audio_fx(spec) == spec.audio_fx


def test_hook_gets_exactly_one_impact():
    beats = collect_beats(_spec())
    impacts = [beat for beat in beats if beat.role == "impact"]
    assert len(impacts) == 1
    assert impacts[0].reason == "hook_hit"


def test_whooshes_only_on_topic_changes_not_every_cut():
    """Six visuals, but only the type/priority jumps earn a whoosh."""
    beats = collect_beats(_spec())
    whooshes = [beat for beat in beats if beat.role == "whoosh"]
    # v2→v3 (motion_graphics) and v4→v5 (infographic) — not the plain footage cuts.
    assert 1 <= len(whooshes) <= 3
    assert all("ring_transition" in beat.reason or "meme_" in beat.reason for beat in whooshes)


def test_emphasis_marks_earn_a_soft_thump():
    beats = collect_beats(_spec())
    thumps = [beat for beat in beats if beat.role == "thump"]
    assert thumps, "high-emphasis segments should pulse the ring"


def test_ring_exit_and_return_when_avatar_leaves_the_frame():
    beats = collect_beats(_spec(avatar_segments=["hook", "s4"]))
    roles = {beat.role for beat in beats}
    assert "power_down" in roles
    assert "power_up" in roles


def test_one_riser_builds_into_the_climax():
    beats = collect_beats(_spec())
    risers = [beat for beat in beats if beat.role == "riser"]
    assert len(risers) == 1
    # Last high-emphasis segment starts at 19s; riser leads by ~2.4s.
    assert 16.0 <= risers[0].at <= 19.0


# --------------------------------------------------------------------------- #
# Density
# --------------------------------------------------------------------------- #


def test_density_cap_scales_with_duration_and_never_exceeds_ceiling():
    beats = [
        __import__("shorts_factory.voice.audio_design", fromlist=["Beat"]).Beat(
            at=float(index), role="whoosh" if index % 2 else "thump", intensity=0.5
        )
        for index in range(20)
    ]
    kept = enforce_density(beats, duration=30.0)
    assert len(kept) <= 8
    assert len(kept) <= int(30 / 6) + 1


def test_same_type_cannot_stack_within_four_seconds():
    from shorts_factory.voice.audio_design import Beat

    beats = [
        Beat(at=1.0, role="whoosh"),
        Beat(at=2.5, role="whoosh"),
        Beat(at=8.0, role="whoosh"),
    ]
    kept = enforce_density(beats, duration=40.0)
    times = [beat.at for beat in kept if beat.role == "whoosh"]
    assert times == [1.0, 8.0]


# --------------------------------------------------------------------------- #
# Peak alignment and trim
# --------------------------------------------------------------------------- #


def test_align_start_puts_the_peak_on_the_cut():
    # A whoosh whose peak sits 0.82s in must start 0.82s before the cut.
    assert align_start(8.0, 0.82) == pytest.approx(7.18)


def test_align_start_allows_a_tiny_negative_for_early_cuts():
    assert align_start(0.2, 0.8) < 0
    assert align_start(0.2, 0.8) >= -0.05


def test_long_tail_is_trimmed_around_the_peak():
    item = LibraryItem(
        path=Path("diamond.mp3"),
        duration=10.48,
        analysis={"peak_at": 1.88, "attack": 1.62, "tail": 5.52, "lufs": -10.4, "peak_dbtp": 1.1},
    )
    start, play = trim_plan(item, role="whoosh", requested=0.8)
    assert start >= 0.0
    assert play <= 3.5, "a 10s cinematic hit must not play in full under narration"
    assert start + play <= item.duration


def test_riser_is_cut_so_it_finishes_on_the_climax():
    item = LibraryItem(
        path=Path("airflow.mp3"),
        duration=3.94,
        analysis={"peak_at": 2.27, "attack": 2.0, "tail": 1.07, "lufs": -12.6, "peak_dbtp": -2.3},
    )
    start, play = trim_plan(item, role="riser", requested=2.4)
    # Peak should sit near the end of the played window.
    assert start + play >= item.peak_at
    assert play <= 3.0


def test_library_volume_normalises_a_hot_file_down():
    hot = LibraryItem(
        path=Path("doppler.mp3"),
        analysis={"lufs": -6.4, "peak_dbtp": 0.6},
    )
    quiet = LibraryItem(
        path=Path("kalimba.wav"),
        analysis={"lufs": -45.0, "peak_dbtp": -22.6},
    )
    assert library_volume(hot, "whoosh", 0.6) < library_volume(quiet, "ui", 0.6)


def test_resolve_aligns_library_hits_by_peak(tmp_path):
    (tmp_path / "whoosh-slow.wav").write_bytes(b"\x00" * 128)
    (tmp_path / "index.json").write_text(
        '{"items": [{"file": "whoosh-slow.wav", "tags": ["whoosh"], "duration": 2.0, '
        '"analysis": {"peak_at": 0.8, "attack": 0.5, "tail": 0.7, "lufs": -18.0, '
        '"peak_dbtp": -3.0, "shape": "swell", "band": "mid"}}]}',
        encoding="utf-8",
    )
    clips, missing = resolve_from_library(
        [AudioFx(type="whoosh", at=8.0, intensity=0.5, duration=0.8)],
        SfxLibrary(tmp_path),
    )
    assert not missing
    assert clips[0].start == pytest.approx(7.2)
    assert clips[0].source == "library"


def test_voice_guard_pushes_beats_past_segment_open():
    from shorts_factory.voice.audio_design import Beat

    spec = _spec()
    beats = [Beat(at=3.1, role="whoosh", reason="early")]  # s1 starts at 3.0
    guarded = apply_voice_guard(beats, spec, guard=0.4)
    assert guarded[0].at == pytest.approx(3.4)


def test_slow_attack_is_trimmed_from_the_head():
    item = LibraryItem(
        path=Path("swell.mp3"),
        duration=2.5,
        analysis={"peak_at": 1.6, "attack": 1.5, "tail": 0.4, "lufs": -18.0, "peak_dbtp": -3.0},
    )
    start, play = trim_plan(item, role="whoosh", requested=0.8)
    assert start > 0.0
    assert start <= item.peak_at
    assert play <= item.duration - start


def test_meme_visual_earns_branded_whoosh_in_and_out():
    spec = _spec()
    spec.visuals.append(
        Visual(
            id="meme-auto",
            type="meme",
            query="later",
            keywords=["later"],
            start=8.8,
            duration=1.2,
            position="fullscreen",
        )
    )
    beats = collect_beats(spec)
    meme_whooshes = [beat for beat in beats if beat.reason.startswith("meme_")]
    assert len(meme_whooshes) == 2
