"""Rhythm engine (§8). The rules here are measured from the references, so a
failure means the edit would visibly drift from the target format."""

from __future__ import annotations

from pathlib import Path

import pytest

from redshift.core.config import load_config
from redshift.core.errors import RhythmViolation
from redshift.core.schemas import Script
from redshift.pipeline.s03_shotlist import (
    RhythmConfig,
    assert_rhythm,
    build_shotlist,
    check_rhythm,
    is_enumeration,
    plan_beat,
    section_target,
    split_units,
)

FIXTURE = Path(__file__).parent / "fixtures" / "script_jwst.json"


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def script() -> Script:
    return Script.load(FIXTURE)


@pytest.fixture(scope="module")
def shotlist(script, config):
    return build_shotlist(script, config)


# --------------------------------------------------------------------------- #
# The gate of §8.4
# --------------------------------------------------------------------------- #


def test_generated_shotlist_passes_every_rhythm_rule(shotlist, config):
    assert check_rhythm(shotlist, config) == []
    assert_rhythm(shotlist, config)


def test_median_shot_sits_in_the_reference_band(shotlist, config):
    rhythm = RhythmConfig.from_config(config)
    assert rhythm.median_min <= shotlist.median_shot_s() <= rhythm.median_max


def test_presenter_share_is_inside_the_brandbook_band(shotlist, config):
    rhythm = RhythmConfig.from_config(config)
    assert rhythm.presenter_min <= shotlist.presenter_share() <= rhythm.presenter_max


def test_no_mode_b_shot_outlives_the_ceiling(shotlist, config):
    """Reference V2 has no shot longer than 2.85s — not one."""
    ceiling = RhythmConfig.from_config(config).mode_max["MODE_B"]
    long_b = [s for s in shotlist.shots if s.mode == "MODE_B" and not s.is_burst]
    assert long_b, "expected some MODE_B shots"
    assert max(s.duration for s in long_b) <= ceiling + 0.001


def test_video_never_opens_on_a_split(shotlist):
    # R1: a split opening is a weak hook.
    assert shotlist.shots[0].mode in {"MODE_B", "MODE_C"}


def test_last_two_seconds_are_the_presenter(shotlist, config):
    # R7: the closer is always the face plus SUBSCRIBE.
    closer_len = float(config.rhythm("mode_switching.closer_len_s"))
    boundary = shotlist.total_duration_s - closer_len
    tail = [s for s in shotlist.shots if s.t_end > boundary]
    assert tail and all(s.mode == "MODE_C" for s in tail)


def test_shot_durations_cover_the_narration_exactly(shotlist, script):
    """§8.2 step 7: the sum must match the VO within 0.15s."""
    expected = sum(beat.duration_hint_s for beat in script.beats)
    assert shotlist.total_duration_s == pytest.approx(expected, abs=0.15)


def test_timeline_has_no_gaps_or_overlaps(shotlist):
    for previous, current in zip(shotlist.shots, shotlist.shots[1:], strict=False):
        assert current.t_start == pytest.approx(previous.t_end, abs=0.001)


def test_enumeration_produces_exactly_one_burst(shotlist, config):
    rhythm = RhythmConfig.from_config(config)
    assert 1 <= len(shotlist.bursts) <= rhythm.burst_per_video_max
    low, high = rhythm.burst_len
    burst_shots = [s for s in shotlist.shots if s.is_burst]
    assert burst_shots
    assert all(low - 0.001 <= s.duration <= high + 0.001 for s in burst_shots)


def test_every_shot_carries_a_fallback_intent(shotlist):
    # Insurance for the degradation ladder (§8.4).
    assert all(shot.fallback_intent for shot in shotlist.shots)


def test_data_viz_falls_back_to_something_we_render_ourselves(shotlist):
    for shot in shotlist.shots:
        if shot.visual_intent == "DATA_VIZ":
            assert shot.fallback_intent in {"DATA_VIZ", "PRESENTER_ONLY"}


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


def test_one_entity_one_shot(script):
    """The main lesson of reference V2: a spoken name is on screen."""
    beat = script.beats[0]
    units = split_units(beat.text, beat.entities)
    for entity in beat.entities:
        assert entity in units


def test_three_entities_trigger_a_burst():
    assert is_enumeration("масса, атмосфера, океан", ["масса", "атмосфера", "океан"])
    assert not is_enumeration("одна короткая фраза", ["K2-18b"])


def test_long_beat_becomes_more_shots_not_longer_ones(config):
    """An 11s beat may not become one 11s shot — that drops the ceiling."""
    rhythm = RhythmConfig.from_config(config)
    plan = plan_beat(11.0, ["a", "b"], section_target("BODY", rhythm), rhythm, burst=False)
    assert sum(length for _, length, _ in plan) == pytest.approx(11.0, abs=0.02)
    assert all(length <= rhythm.mode_max["MODE_B"] + 0.001 for _, length, _ in plan)
    assert len(plan) >= 4


def test_burst_is_punctuation_inside_a_beat_not_the_whole_beat(config):
    """A burst takes its share; the rest of the beat is edited normally."""
    rhythm = RhythmConfig.from_config(config)
    plan = plan_beat(9.0, ["a", "b", "c"], section_target("BODY", rhythm), rhythm, burst=True)
    burst_time = sum(length for _, length, is_burst in plan if is_burst)
    assert sum(length for _, length, _ in plan) == pytest.approx(9.0, abs=0.02)
    assert 0 < burst_time <= 9.0 / 3 + 0.01, "a burst may not eat the whole beat"
    assert any(not is_burst for _, _, is_burst in plan)


def test_section_target_comes_from_the_rhythm_profile(config):
    rhythm = RhythmConfig.from_config(config)
    # HOOK is the densest section, CLOSER the calmest (§2.3).
    assert section_target("HOOK", rhythm) < section_target("BODY", rhythm)
    assert section_target("CLOSER", rhythm) > section_target("CLIMAX", rhythm)


# --------------------------------------------------------------------------- #
# The checker itself must catch a bad edit
# --------------------------------------------------------------------------- #


def test_checker_catches_an_overlong_mode_b_shot(shotlist, config):
    broken = shotlist.model_copy(deep=True)
    victim = next(s for s in broken.shots if s.mode == "MODE_B" and not s.is_burst)
    victim.t_end = round(victim.t_start + 5.0, 3)
    problems = check_rhythm(broken, config)
    assert any("ceiling" in problem for problem in problems)


def test_checker_catches_a_missing_presenter(shotlist, config):
    broken = shotlist.model_copy(deep=True)
    for shot in broken.shots:
        shot.mode = "MODE_B"
    problems = check_rhythm(broken, config)
    assert any("presenter share" in problem for problem in problems)


def test_checker_catches_a_split_opening(shotlist, config):
    broken = shotlist.model_copy(deep=True)
    broken.shots[0].mode = "MODE_A"
    assert any("R1" in problem for problem in check_rhythm(broken, config))


def test_assert_rhythm_raises_on_a_broken_edit(shotlist, config):
    broken = shotlist.model_copy(deep=True)
    for shot in broken.shots:
        shot.mode = "MODE_B"
    with pytest.raises(RhythmViolation):
        assert_rhythm(broken, config)


def test_shotlist_survives_a_round_trip(shotlist, tmp_path):
    path = shotlist.save(tmp_path / "shotlist.json")
    reloaded = type(shotlist).load(path)
    assert reloaded.total_duration_s == shotlist.total_duration_s
    assert len(reloaded.shots) == len(shotlist.shots)


# --------------------------------------------------------------------------- #
# Retiming the shipping pipeline (§8 applied to shorts_factory)
# --------------------------------------------------------------------------- #


def _spec_with(segments, visuals):
    """A spec built straight from the pieces retiming cares about."""
    from shorts_factory.spec import (
        AvatarSettings,
        BrandElements,
        CaptionSettings,
        Constraints,
        MemeSettings,
        MusicSettings,
        Spec,
        VoiceSettings,
    )

    return Spec(
        id="rhythm",
        title="rhythm",
        duration_target=sum(segment.duration for segment in segments),
        language="ru",
        topic="rhythm",
        hook=segments[0],
        script=list(segments[1:]),
        voice=VoiceSettings(),
        avatar=AvatarSettings(),
        visuals=list(visuals),
        music=MusicSettings(),
        audio_fx=[],
        captions=CaptionSettings(),
        brand=BrandElements(),
        cta=None,
        memes=MemeSettings(),
        constraints=Constraints(),
    )


def _segment(id_, text, start, duration):
    from shorts_factory.spec import ScriptSegment

    return ScriptSegment(id=id_, text=text, start=start, duration=duration)


def _visual(id_, segment_ref, start, duration, type_="footage"):
    from shorts_factory.spec import Visual

    return Visual(
        id=id_,
        type=type_,
        query="anything",
        keywords=["anything"],
        start=start,
        duration=duration,
        segment_ref=segment_ref,
    )


def test_a_slide_show_becomes_an_edit():
    """The defect, stated as a test: equal durations everywhere.

    The scenario hand-wrote 1.8s for every slot, which is why the first cut
    changed picture on a metronome. After retiming the lengths have to differ,
    and differ for a reason — the frame carrying the number is the long one.
    """
    from shorts_factory.render.rhythm import retime_visuals

    segments = [
        _segment("hook", "Модель сама взломала чужой сервис.", 0.0, 4.0),
        _segment("s1", "Их тестировали на девятьсот задач по взлому.", 4.0, 6.0),
    ]
    visuals = [
        _visual("v1", "hook", 0.0, 2.0),
        _visual("v2", "hook", 2.0, 2.0),
        _visual("v3", "s1", 4.0, 2.0),
        _visual("v4", "s1", 6.0, 2.0),
        _visual("v5", "s1", 8.0, 2.0),
    ]
    spec = _spec_with(segments, visuals)

    report = retime_visuals(spec, [])

    lengths = [visual.duration for visual in spec.visuals]
    assert report.retimed == 5
    assert len(set(lengths)) > 1, f"every shot is still {lengths[0]}s: {lengths}"
    assert report.spread_s >= 0.25, f"the cut would still read as a slide show: {lengths}"


def test_the_median_lands_in_the_measured_band():
    """1.2-2.2s is the reference band from config/rhythm.yaml, not a taste.

    Measured on the real scenario, because the median is as much a property of
    how many slots the author wrote as of how the seconds are shared out: N
    slots over M seconds have a median of M/N whatever the retimer does.
    """
    from pathlib import Path as _Path

    from shorts_factory.render.rhythm import load_band, retime_visuals
    from shorts_factory.spec import load_spec

    scenario = _Path(__file__).resolve().parents[1] / "jobs" / "openai-huggingface-hack.json"
    spec, _issues = load_spec(scenario)
    band = load_band()

    report = retime_visuals(spec, [])

    assert band.median_min <= report.median_s <= band.median_max, report.to_dict()
    assert report.spread_s > 0.5, f"the shipping scenario still cuts like a slide show: {report.to_dict()}"
    assert not report.notes, report.notes


def test_an_impossible_median_is_reported_rather_than_faked():
    """Three slots over nine seconds cannot average two seconds a shot."""
    from shorts_factory.render.rhythm import retime_visuals

    segments = [_segment("s1", "Очень длинный кусок текста без единой паузы.", 0.0, 9.0)]
    visuals = [_visual("v1", "s1", 0.0, 3.0), _visual("v2", "s1", 3.0, 3.0), _visual("v3", "s1", 6.0, 3.0)]
    spec = _spec_with(segments, visuals)

    report = retime_visuals(spec, [])

    assert report.notes, "a median this far off the band has to be said out loud"
    assert "slots" in report.notes[0]


def test_a_frame_that_has_to_be_read_is_held_longer():
    """An infographic is not cut at the speed of b-roll — it carries numbers."""
    from shorts_factory.render.rhythm import retime_visuals

    segments = [_segment("s1", "Здесь приводится статистика по инциденту.", 0.0, 6.0)]
    visuals = [
        _visual("v1", "s1", 0.0, 2.0),
        _visual("v2", "s1", 2.0, 2.0, type_="infographic"),
        _visual("v3", "s1", 4.0, 2.0),
    ]
    spec = _spec_with(segments, visuals)

    retime_visuals(spec, [])

    chart = next(visual for visual in spec.visuals if visual.type == "infographic")
    others = [visual.duration for visual in spec.visuals if visual.type != "infographic"]
    assert chart.duration > max(others), "the chart is gone before it can be read"


def test_the_voice_keeps_its_place():
    """Retiming may reshape a segment; it may not slide it off the narration."""
    from shorts_factory.render.rhythm import retime_visuals

    segments = [
        _segment("hook", "Первый кусок текста.", 0.0, 4.0),
        _segment("s1", "Второй кусок текста, немного длиннее.", 4.0, 6.0),
    ]
    visuals = [
        _visual("v1", "hook", 0.0, 2.0),
        _visual("v2", "hook", 2.0, 2.0),
        _visual("v3", "s1", 4.0, 3.0),
        _visual("v4", "s1", 7.0, 3.0),
    ]
    spec = _spec_with(segments, visuals)

    retime_visuals(spec, [])

    for segment in segments:
        slots = [visual for visual in spec.visuals if visual.segment_ref == segment.id]
        assert slots[0].start == pytest.approx(segment.start, abs=0.01)
        last = slots[-1]
        assert last.start + last.duration == pytest.approx(segment.start + segment.duration, abs=0.05)
        for earlier, later in zip(slots, slots[1:], strict=False):
            assert earlier.start + earlier.duration == pytest.approx(later.start, abs=0.01), (
                "a gap or an overlap opened between two shots"
            )


def test_cuts_land_between_words_when_the_alignment_is_real():
    """With measured word timings the picture never changes mid-word."""
    from pathlib import Path as _Path

    from shorts_factory.render.rhythm import retime_visuals
    from shorts_factory.voice.elevenlabs import VoiceClip, WordTiming

    segments = [_segment("s1", "раз два три четыре пять шесть", 0.0, 6.0)]
    visuals = [_visual("v1", "s1", 0.0, 2.0), _visual("v2", "s1", 2.0, 2.0), _visual("v3", "s1", 4.0, 2.0)]
    spec = _spec_with(segments, visuals)
    edges = [1.0, 2.1, 3.05, 4.0, 5.2, 6.0]
    clip = VoiceClip(
        segment_id="s1",
        path=_Path("voice.mp3"),
        start=0.0,
        duration=6.0,
        text=segments[0].text,
        words=[
            WordTiming(word, edges[index - 1] if index else 0.0, edge)
            for index, (word, edge) in enumerate(zip(segments[0].text.split(), edges, strict=True))
        ],
    )

    retime_visuals(spec, [clip])

    cuts = [round(visual.start + visual.duration, 3) for visual in spec.visuals[:-1]]
    for cut in cuts:
        assert any(abs(cut - edge) < 0.001 for edge in edges), f"cut at {cut}s falls inside a word"


def test_retiming_never_leaves_two_shots_on_the_same_instant():
    """The failure mode that killed a render outright.

    Each segment is retimed against its own narration, and a synthesised
    segment can overrun the window the scenario gave it — the voice takes as
    long as it takes. Two segments stretched that way put two clips on one
    track at the same moment, and HyperFrames refuses to lint that:
    "overlapping_clips_same_track", no video produced.
    """
    from pathlib import Path as _Path

    from shorts_factory.render.rhythm import retime_visuals
    from shorts_factory.voice.elevenlabs import VoiceClip

    segments = [
        _segment("hook", "Первый кусок, короткий.", 0.0, 4.0),
        _segment("s1", "Второй кусок, который диктор читает дольше.", 4.0, 6.0),
    ]
    visuals = [
        _visual("v1", "hook", 0.0, 2.0),
        _visual("v2", "hook", 2.0, 2.0),
        _visual("v3", "s1", 4.0, 3.0),
        _visual("v4", "s1", 7.0, 3.0),
    ]
    spec = _spec_with(segments, visuals)
    # The hook's voice ran 2.1s past the window the scenario allowed for it.
    clips = [
        VoiceClip(segment_id="hook", path=_Path("a.mp3"), start=0.0, duration=6.1, text=segments[0].text),
        VoiceClip(segment_id="s1", path=_Path("b.mp3"), start=4.0, duration=6.0, text=segments[1].text),
    ]

    report = retime_visuals(spec, clips)

    ordered = sorted(spec.visuals, key=lambda visual: visual.start)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier.start + earlier.duration <= later.start + 0.001, (
            f"{earlier.id} runs into {later.id}: "
            f"{earlier.start:.2f}+{earlier.duration:.2f} vs {later.start:.2f}"
        )
    assert any("trimmed" in note for note in report.notes), "the trim happened silently"


def test_the_shipping_scenario_has_no_overlapping_shots():
    """Measured on the real file, because that is the one that has to render."""
    from pathlib import Path as _Path

    from shorts_factory.render.rhythm import retime_visuals
    from shorts_factory.spec import load_spec

    scenario = _Path(__file__).resolve().parents[1] / "jobs" / "openai-huggingface-hack.json"
    spec, _issues = load_spec(scenario)
    retime_visuals(spec, [])

    ordered = sorted(spec.visuals, key=lambda visual: visual.start)
    overlaps = [
        (earlier.id, later.id)
        for earlier, later in zip(ordered, ordered[1:], strict=False)
        if earlier.start + earlier.duration > later.start + 0.001
    ]
    assert not overlaps, f"overlapping shots would fail hyperframes lint: {overlaps}"
