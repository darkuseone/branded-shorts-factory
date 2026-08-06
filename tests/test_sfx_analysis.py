"""The classification rules, exercised without touching FFmpeg.

Every case here is a real file from the bank, described by the numbers the
analyser measured from it. That is deliberate: these are the sounds the rules
got wrong at least once, so they are the ones worth pinning down.
"""

from __future__ import annotations

from pathlib import Path

from shorts_factory.voice.sfx_analysis import (
    AudioProfile,
    classify_band,
    classify_shape,
    describe_envelope,
    estimate_level,
    gain_db_for,
    profile_from_metrics,
    roles_for,
)


def ramp(start: float, end: float, count: int) -> list[float]:
    step = (end - start) / max(1, count - 1)
    return [start + step * index for index in range(count)]


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


def test_peak_position_is_measured_not_assumed():
    """A slow whoosh peaks late; that offset is what aligns the hit to the cut."""
    levels = ramp(-60, -6, 40) + ramp(-6, -50, 40)
    described = describe_envelope(levels, duration=2.0)
    assert 0.9 < described["peak_at"] < 1.1, "peak sits halfway through a 2s swell"
    # Onset is the first window within 20 dB of the peak, so a linear ramp
    # from -60 to -6 reports about a third of a second of attack — not the
    # whole climb. What matters is that the attack is non-zero and real.
    assert described["attack"] > 0.2


def test_a_one_shot_reports_no_attack():
    levels = [-6.0] + ramp(-8, -70, 40)
    described = describe_envelope(levels, duration=1.0)
    assert described["peak_at"] == 0.0
    assert described["attack"] == 0.0


def test_silence_does_not_crash_the_description():
    described = describe_envelope([-120.0] * 10, duration=1.0)
    assert described["peak_at"] == 0.0
    assert described["spread"] == 0.0


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_a_short_clap_is_not_a_drone():
    """Regression: a 0.23s clap measured flat and was classified as atmosphere."""
    shape = classify_shape(duration=0.23, peak_at=0.02, attack=0.0, spread=6.9, rise=0.08)
    assert shape == "oneshot"


def test_an_air_conditioner_hum_is_a_drone():
    shape = classify_shape(duration=6.03, peak_at=4.29, attack=0.0, spread=2.1, rise=0.71)
    assert shape == "drone"


def test_a_long_track_is_a_bed_even_though_it_has_dynamics():
    """A guitar loop peaks late and runs 18s: that is music, not a riser."""
    shape = classify_shape(duration=18.31, peak_at=15.23, attack=0.0, spread=9.0, rise=0.83)
    assert shape == "bed"


def test_a_long_cinematic_hit_is_not_mistaken_for_music():
    """Same length as a track, but the energy arrives early and decays.

    Real measurement on `diamond_tunes`: peak at 1.88s of 10.48s, attack 1.62s —
    that is a swell, never a bed, even though the file runs past BED_SECONDS.
    """
    shape = classify_shape(duration=10.48, peak_at=1.88, attack=1.62, spread=15.9, rise=0.18)
    assert shape == "swell"


def test_a_build_is_a_riser_while_it_stays_short_enough_to_use():
    assert classify_shape(duration=3.9, peak_at=3.4, attack=3.0, spread=20.0, rise=0.87) == "riser"
    assert classify_shape(duration=14.0, peak_at=12.0, attack=10.0, spread=20.0, rise=0.86) == "bed"


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


def test_impacts_only_come_from_the_bottom_of_the_spectrum():
    """Regression: a ringing kalimba note was being offered as an impact."""
    kalimba = roles_for(shape="oneshot", band="high", duration=2.45, tail=1.02, position=0.0)
    assert "impact" not in kalimba
    assert "ui" in kalimba

    bass_drum = roles_for(shape="oneshot", band="sub", duration=3.27, tail=3.2, position=0.0)
    assert "impact" in bass_drum
    assert "thump" in bass_drum


def test_a_very_short_tick_can_drive_the_ring_leaving_frame():
    roles = roles_for(shape="oneshot", band="high", duration=0.16, tail=0.07, position=0.0)
    assert {"power_down", "power_up", "click"} <= set(roles)


def test_a_swell_with_a_late_peak_doubles_as_a_riser():
    """Placement aligns by peak, so this works as a pre-climax build."""
    roles = roles_for(shape="swell", band="mid", duration=3.94, tail=1.07, position=0.58)
    assert "riser" in roles
    assert "whoosh" in roles


def test_an_early_peaking_swell_is_not_offered_as_a_riser():
    roles = roles_for(shape="swell", band="mid", duration=10.0, tail=7.4, position=0.07)
    assert "riser" not in roles
    assert "long_tail" in roles, "the montage needs to know it must trim this"


def test_music_beds_are_never_offered_as_accents():
    assert roles_for(shape="bed", band="low", duration=18.3, tail=3.0) == []
    assert roles_for(shape="drone", band="mid", duration=6.0, tail=1.7) == ["ambience"]


def test_usable_as_accent_follows_the_roles():
    bed = AudioProfile(path=Path("loop.mp3"), duration=18.3, shape="bed")
    hit = AudioProfile(path=Path("bd.wav"), duration=3.3, shape="oneshot", roles=["impact"])
    assert not bed.usable_as_accent
    assert hit.usable_as_accent


# --------------------------------------------------------------------------- #
# Level
# --------------------------------------------------------------------------- #


def test_short_files_get_an_estimated_level_instead_of_negative_infinity():
    """`loudnorm` reports -inf below ~3s, which made every hit look silent."""
    levels = [-12.0, -14.0, -20.0, -120.0, -120.0]
    profile = profile_from_metrics(
        Path("claves.wav"),
        duration=0.06,
        lufs=float("-inf"),
        peak_dbtp=-3.0,
        lra=0.0,
        levels=levels,
        centroid=2651.0,
    )
    assert profile.lufs_estimated
    assert -25.0 < profile.lufs < -5.0


def test_a_long_silent_tail_does_not_make_a_loud_hit_look_quiet():
    loud_then_silent = [-8.0] + [-120.0] * 60
    assert estimate_level(loud_then_silent) > -12.0


def test_gain_never_pushes_a_file_into_clipping():
    hot = AudioProfile(path=Path("doppler.mp3"), lufs=-6.4, peak_dbtp=0.6)
    quiet = AudioProfile(path=Path("kalimba.wav"), lufs=-45.0, peak_dbtp=-22.6)
    assert gain_db_for(hot, "whoosh") < 0, "a hot file gets cut, not boosted"
    assert gain_db_for(quiet, "ui") > 0
    for profile in (hot, quiet):
        assert profile.peak_dbtp + gain_db_for(profile, "whoosh") <= -1.0 + 1e-9


def test_band_edges():
    assert classify_band(132.0) == "sub"
    assert classify_band(821.0) == "low"
    assert classify_band(2651.0) == "mid"
    assert classify_band(8480.0) == "high"
