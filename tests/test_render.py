from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from shorts_factory.media.download import LocalAsset
from shorts_factory.qa.gate import VisualQA
from shorts_factory.render.composition import CompositionWriter
from shorts_factory.render.timeline import (
    TRACK_AUDIO_MUSIC,
    TRACK_AVATAR,
    TRACK_BROLL,
    TRACK_CAPTIONS,
    build_timeline,
    use_mixed_audio,
)
from shorts_factory.resolver import ResolvedVisual
from shorts_factory.spec import Cta
from shorts_factory.voice.captions import CaptionCue, CaptionWord, build_cues
from shorts_factory.voice.elevenlabs import SfxClip, VoiceClip, WordTiming, _words_from_alignment
from shorts_factory.voice.heygen import AvatarClip

from .test_search import make_candidate


@pytest.fixture
def fake_media(tmp_path: Path) -> Path:
    """A real, tiny clip the writer can copy *and* probe.

    Placeholder bytes used to be enough, which quietly made these tests pass
    only on machines without ffmpeg: with ffmpeg present the probe rejected the
    file and the asset was dropped. A one-second black clip exercises the real
    path either way.
    """
    path = tmp_path / "clip.mp4"
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=black:s=270x480:d=1", "-r", "12",
             "-pix_fmt", "yuv420p", str(path)],
            check=False, capture_output=True, timeout=60,
        )  # fmt: skip
    if not path.exists() or path.stat().st_size < 512:
        path.write_bytes(b"\x00" * 2048)
    return path


def resolved_for(spec, fake_media: Path, outcome: str = "accepted") -> list[ResolvedVisual]:
    items = []
    for visual in spec.visuals:
        asset = LocalAsset(
            candidate=make_candidate(external_id=visual.id, source="pexels", license="Pexels License"),
            path=fake_media,
        )
        items.append(
            ResolvedVisual(
                visual=visual,
                qa=VisualQA(visual_id=visual.id, outcome=outcome, asset=asset),
                search=None,
            )
        )
    return items


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


def test_timeline_places_every_visual_on_a_video_track(minimal_spec, fake_media):
    from shorts_factory.render.timeline import TRACK_MEME, TRACK_OVERLAY

    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    placed = [
        element
        for element in timeline.elements
        if element.track in {TRACK_BROLL, TRACK_OVERLAY, TRACK_MEME} and element.kind in {"video", "image"}
    ]
    assert len(placed) == len(minimal_spec.visuals)
    assert timeline.width == 1080 and timeline.height == 1920
    assert timeline.duration == minimal_spec.duration_target


def test_unfilled_visuals_are_dropped_with_a_warning(minimal_spec, fake_media):
    resolved = resolved_for(minimal_spec, fake_media)
    resolved[0].qa.outcome = "rejected"
    resolved[0].qa.asset = None
    timeline = build_timeline(minimal_spec, resolved)
    ids = {element.id for element in timeline.elements}
    assert "v1" not in ids
    assert any("v1" in warning for warning in timeline.warnings)


def test_short_source_clip_is_marked_for_looping(minimal_spec, fake_media):
    resolved = resolved_for(minimal_spec, fake_media)
    resolved[0].qa.asset.candidate.duration = 0.8  # slot is 2.0s
    timeline = build_timeline(minimal_spec, resolved)
    element = next(e for e in timeline.elements if e.id == "v1")
    assert element.props["loop"] is True


def test_avatar_and_captions_land_on_their_own_tracks(minimal_spec, fake_media):
    minimal_spec.avatar.enabled = True
    minimal_spec.avatar.avatar_id = "avatar-123"
    avatar = AvatarClip(path=fake_media, duration=12.0, transparent=True)
    cues = [CaptionCue(text="hello", start=0.0, end=1.0, words=[CaptionWord("hello", 0.0, 1.0)])]
    timeline = build_timeline(
        minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues, avatar=avatar
    )
    assert any(e.track == TRACK_AVATAR for e in timeline.elements)
    assert any(e.track == TRACK_CAPTIONS for e in timeline.elements)


def test_avatar_starts_at_the_window_it_was_rendered_for(minimal_spec, fake_media):
    minimal_spec.avatar.enabled = True
    minimal_spec.avatar.avatar_id = "avatar-123"
    avatar = AvatarClip(path=fake_media, duration=9.0)
    timeline = build_timeline(
        minimal_spec, resolved_for(minimal_spec, fake_media), avatar=avatar, avatar_start=10.5
    )
    element = next(e for e in timeline.elements if e.track == TRACK_AVATAR)
    assert element.start == pytest.approx(10.5)
    assert element.end <= minimal_spec.duration_target + 0.001


def test_music_element_carries_ducking_windows(minimal_spec, fake_media):
    voice = [VoiceClip(segment_id="s1", path=fake_media, start=2.5, duration=8.0, text="x")]
    timeline = build_timeline(
        minimal_spec, resolved_for(minimal_spec, fake_media), voice_clips=voice, music_path=fake_media
    )
    music = next(e for e in timeline.elements if e.track == TRACK_AUDIO_MUSIC)
    assert music.props["duck_windows"] == [{"start": 2.5, "end": 10.5}]


def test_mixed_audio_replaces_the_separate_stems(minimal_spec, fake_media):
    voice = [VoiceClip(segment_id="s1", path=fake_media, start=0.0, duration=8.0, text="x")]
    sfx = [SfxClip(fx_id="fx1", path=fake_media, start=1.0, duration=0.5, volume=0.3)]
    timeline = build_timeline(
        minimal_spec,
        resolved_for(minimal_spec, fake_media),
        voice_clips=voice,
        sfx_clips=sfx,
        music_path=fake_media,
    )
    assert len(timeline.audio_elements) == 3
    use_mixed_audio(timeline, fake_media, minimal_spec.duration_target)
    assert len(timeline.audio_elements) == 1
    assert timeline.audio_elements[0].id == "mix"


def test_timeline_serialises_to_json(minimal_spec, fake_media):
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    payload = json.dumps(timeline.to_dict())
    assert "composition_id" in payload


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def test_composition_is_self_contained(minimal_spec, fake_media, tmp_path):
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    result = CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()

    html = result.index_html.read_text(encoding="utf-8")
    assert result.index_html.exists()
    assert (result.directory / "media").is_dir()
    assert 'data-width="1080"' in html and 'data-height="1920"' in html
    assert 'src="media/' in html, "assets must be referenced relatively for --docker renders"
    assert "http://" not in html and "https://" not in html, "no external requests during render"


def test_composition_marks_clips_with_hyperframes_attributes(minimal_spec, fake_media, tmp_path):
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")
    assert 'class="clip video"' in html
    assert "data-start=" in html and "data-duration=" in html and "data-track-index=" in html


def test_captions_become_per_word_spans_with_keyframes(minimal_spec, fake_media, tmp_path):
    cues = [
        CaptionCue(
            text="hello world",
            start=1.0,
            end=2.0,
            words=[CaptionWord("hello", 1.0, 1.5), CaptionWord("world", 1.5, 2.0)],
        )
    ]
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")
    assert 'id="w_cap_000_0"' in html and 'id="w_cap_000_1"' in html
    assert "@keyframes kw_cap_000_0" in html


def test_composition_writes_a_manifest_with_credits(minimal_spec, fake_media, tmp_path):
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    result = CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["spec_id"] == minimal_spec.id
    assert manifest["credits"][0]["source"] == "pexels"
    assert (result.directory / "DESIGN.md").exists()


def test_missing_asset_files_are_skipped_not_crashed(minimal_spec, tmp_path):
    resolved = resolved_for(minimal_spec, tmp_path / "gone.mp4")
    timeline = build_timeline(minimal_spec, resolved)
    result = CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    assert result.media_files == 0
    assert "<video" not in result.index_html.read_text(encoding="utf-8")


def test_keyframe_percentages_stay_within_bounds(minimal_spec, fake_media, tmp_path):
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")
    percentages = [
        float(token.split("%")[0])
        for token in html.split("{")
        if token[:1].isdigit() and "%" in token.split("{")[0][:12]
    ]
    assert percentages, "expected keyframe stops in the generated CSS"
    assert all(0.0 <= value <= 100.0 for value in percentages)


# --------------------------------------------------------------------------- #
# Captions and audio design
# --------------------------------------------------------------------------- #


def test_cues_use_real_word_timings_when_available(minimal_spec, fake_media):
    clip = VoiceClip(
        segment_id="hook",
        path=fake_media,
        start=0.0,
        duration=2.5,
        text=minimal_spec.hook.text,
        words=[WordTiming("Telescopes", 0.0, 0.8), WordTiming("found", 0.8, 1.2)],
    )
    cues = build_cues([minimal_spec.hook], [clip], minimal_spec.captions)
    assert cues[0].words[0].start == pytest.approx(0.0)
    assert cues[0].words[1].end == pytest.approx(1.2)


def test_cues_are_estimated_without_alignment(minimal_spec):
    cues = build_cues(minimal_spec.all_segments, [], minimal_spec.captions)
    assert cues
    assert all(cue.end > cue.start for cue in cues)
    assert cues[0].start == pytest.approx(0.0, abs=0.01)


def test_cue_lines_respect_the_character_budget(minimal_spec):
    minimal_spec.captions.max_chars_per_line = 12
    cues = build_cues(minimal_spec.all_segments, [], minimal_spec.captions)
    assert all(len(cue.text) <= 24 for cue in cues), "lines should stay near the budget"


def test_captions_disabled_produces_nothing(minimal_spec):
    minimal_spec.captions.enabled = False
    assert build_cues(minimal_spec.all_segments, [], minimal_spec.captions) == []


def test_alignment_folding_builds_words():
    alignment = {
        "characters": list("hi yo"),
        "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4],
        "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5],
    }
    words = _words_from_alignment(alignment)
    assert [word.word for word in words] == ["hi", "yo"]
    assert words[1].start == pytest.approx(0.3)


def test_alignment_folding_rejects_mismatched_input():
    assert _words_from_alignment({"characters": ["a"], "character_start_times_seconds": []}) == []


# --------------------------------------------------------------------------- #
# Track hygiene
# --------------------------------------------------------------------------- #


def test_cta_and_outro_never_share_the_last_seconds(minimal_spec, fake_media):
    """Both live on the CTA track, and a CTA that stops early is pointless —
    so without reconciliation they always collide once both are enabled."""
    from shorts_factory.render.timeline import TRACK_CTA

    minimal_spec.brand.outro_card = True
    minimal_spec.cta = Cta(text="Подпишись", start=minimal_spec.duration_target - 5.0, duration=5.0)

    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    on_track = sorted((e for e in timeline.elements if e.track == TRACK_CTA), key=lambda e: e.start)
    for earlier, later in zip(on_track, on_track[1:], strict=False):
        assert later.start >= earlier.end - 0.001, f"{earlier.id} overlaps {later.id}"


def test_a_cta_too_short_to_read_keeps_the_badge_and_drops_the_card(minimal_spec, fake_media):
    minimal_spec.brand.outro_card = True
    # Starting almost at the outro leaves no room to trim into.
    minimal_spec.cta = Cta(text="Подпишись", start=minimal_spec.duration_target - 2.6, duration=2.6)

    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    ids = {e.id for e in timeline.elements}
    assert "cta" in ids, "the CTA is the element with a job to do"
    assert "outro" not in ids
    assert any("outro card dropped" in w for w in timeline.warnings)


def test_no_two_elements_ever_overlap_on_any_track(minimal_spec, fake_media):
    import collections

    minimal_spec.brand.outro_card = True
    minimal_spec.cta = Cta(text="Подпишись", start=minimal_spec.duration_target - 5.0, duration=5.0)
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    by_track = collections.defaultdict(list)
    for element in timeline.elements:
        by_track[element.track].append(element)
    for track, elements in by_track.items():
        elements.sort(key=lambda e: e.start)
        for earlier, later in zip(elements, elements[1:], strict=False):
            assert later.start >= earlier.end - 0.001, (
                f"track {track}: {earlier.id} [{earlier.start:.2f},{earlier.end:.2f}] "
                f"overlaps {later.id} [{later.start:.2f},{later.end:.2f}]"
            )
