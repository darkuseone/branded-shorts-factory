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
