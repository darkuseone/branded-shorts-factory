"""Your own sound bank beats generation: cheaper, instant, consistent."""

from __future__ import annotations

from pathlib import Path

import pytest

from shorts_factory.assets.library import SfxLibrary
from shorts_factory.spec import AudioFx
from shorts_factory.voice.audio_design import fx_volume, resolve_from_library


@pytest.fixture
def bank(tmp_path: Path) -> SfxLibrary:
    for name in (
        "whoosh-fast-01.wav",
        "whoosh-fast-02.wav",
        "boom-impact-deep.mp3",
        "riser-tension.wav",
        "notes.txt",  # ignored: not audio
    ):
        (tmp_path / name).write_bytes(b"\x00" * 128)
    return SfxLibrary(tmp_path)


def test_only_audio_files_are_indexed(bank):
    assert {item.path.name for item in bank.items} == {
        "whoosh-fast-01.wav",
        "whoosh-fast-02.wav",
        "boom-impact-deep.mp3",
        "riser-tension.wav",
    }


def test_type_matches_by_filename_without_any_index(bank):
    assert bank.pick("riser").path.name == "riser-tension.wav"


def test_synonyms_match_a_differently_named_file(bank):
    """A pack calls it "boom"; the scenario asks for an "impact"."""
    assert bank.pick("impact").path.name == "boom-impact-deep.mp3"


def test_repeated_requests_rotate_between_equal_matches(bank):
    picks = [bank.pick("whoosh").path.name for _ in range(4)]
    assert picks[0] != picks[1], "six identical whooshes is the sound we are avoiding"
    assert picks[:2] == picks[2:], "rotation cycles rather than drifting"


def test_missing_type_returns_nothing_rather_than_something_wrong(bank):
    assert bank.pick("glitch") is None


def test_index_json_tags_win_over_filenames(tmp_path):
    (tmp_path / "sound_a.wav").write_bytes(b"\x00" * 128)
    (tmp_path / "index.json").write_text(
        '{"items": [{"file": "sound_a.wav", "title": "Signature hit", '
        '"tags": ["impact", "brand"], "duration": 1.2}]}',
        encoding="utf-8",
    )
    library = SfxLibrary(tmp_path)
    item = library.pick("impact")
    assert item is not None and item.title == "Signature hit"
    assert item.duration == pytest.approx(1.2)


def test_library_covers_what_it_can_and_reports_the_rest(bank):
    effects = [
        AudioFx(type="whoosh", at=1.0, intensity=0.5),
        AudioFx(type="impact", at=2.0, intensity=0.8),
        AudioFx(type="glitch", at=3.0, intensity=0.4),
    ]
    clips, missing = resolve_from_library(effects, bank)

    assert [clip.source for clip in clips] == ["library", "library"]
    assert [fx.type for fx in missing] == ["glitch"], "only the gap needs generating"
    assert clips[1].duration > 0
    assert clips[0].start == 1.0


def test_an_empty_bank_sends_everything_to_generation(tmp_path):
    effects = [AudioFx(type="whoosh", at=1.0)]
    clips, missing = resolve_from_library(effects, SfxLibrary(tmp_path / "nope"))
    assert clips == []
    assert missing == effects


def test_effect_volume_stays_under_the_narration():
    assert fx_volume(0.0) == pytest.approx(0.15)
    assert fx_volume(1.0) == pytest.approx(0.45)
    assert fx_volume(5.0) == pytest.approx(0.45), "clamped, not scaled"
