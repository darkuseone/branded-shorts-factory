from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from shorts_factory.media.download import LocalAsset
from shorts_factory.qa.gate import VisualQA
from shorts_factory.render.composition import CompositionWriter
from shorts_factory.render.timeline import (
    SAFE_MARGIN,
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


# --------------------------------------------------------------------------- #
# On-screen defects the client reported after watching the first cut
# --------------------------------------------------------------------------- #


def test_the_presenter_is_never_blown_up_by_default(minimal_spec, fake_media, tmp_path):
    """A standing scale(1.08) on top of a 1080x1920 → 1080x768 crop is how a
    head-and-shoulders render came out as forehead and half a face."""
    minimal_spec.avatar.enabled = True
    minimal_spec.avatar.avatar_id = "avatar-123"
    avatar = AvatarClip(path=fake_media, duration=12.0)
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), avatar=avatar)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    host_block = html.split(".host-video video")[-1].split("}")[0] if ".host-video video" in html else ""
    assert "scale(" not in host_block, f"presenter carries a standing zoom: {host_block}"


def test_the_presenter_is_framed_on_the_face(minimal_spec, fake_media, tmp_path):
    from shorts_factory.render.host_presence import HOST_FOCUS

    minimal_spec.avatar.enabled = True
    minimal_spec.avatar.avatar_id = "avatar-123"
    avatar = AvatarClip(path=fake_media, duration=12.0)
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), avatar=avatar)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")
    assert f"object-position: {HOST_FOCUS}" in html or f"object-position:{HOST_FOCUS}" in html


def test_an_emphasis_push_stays_within_fifteen_percent():
    """The client's own limit: never enlarge the presenter, 10-15% at most."""
    from shorts_factory.render.host_presence import HOST_EMPHASIS_ZOOM

    assert 1.0 < HOST_EMPHASIS_ZOOM <= 1.15


def test_captions_stay_inside_the_safe_area(minimal_spec, fake_media, tmp_path):
    """A line as wide as the frame puts its first and last glyph on the bezel."""
    cues = [
        CaptionCue(
            text="ExploitGym сверхдлинноесловокотороеневлезает",
            start=1.0,
            end=2.0,
            words=[CaptionWord("ExploitGym", 1.0, 1.5), CaptionWord("сверхдлинное", 1.5, 2.0)],
        )
    ]
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    caption_css = html.split(".caption {")[1].split("}")[0]
    assert "overflow-wrap: anywhere" in caption_css, "an unbreakable word runs off the edge"
    assert f"padding: 0 calc({SAFE_MARGIN}px * 2)" in caption_css, f"text runs to the bezel: {caption_css}"


def test_a_caption_is_centred_by_its_box_not_by_a_transform(minimal_spec, fake_media, tmp_path):
    """Why the subtitles drifted off frame.

    Every element carries one full-length animation, attached with fill:both,
    and that animation writes `transform`. A `translateX(-50%)` living in the
    class is therefore overwritten the instant the element is animated — which
    left the block's *left* edge at mid-frame with the text hanging off the
    right side. Centring has to come from something an animation cannot touch.
    """
    cues = [CaptionCue(text="слово", start=1.0, end=2.0, words=[CaptionWord("слово", 1.0, 2.0)])]
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    for selector in (".caption {", ".text {"):
        block = re.sub(r"/\*.*?\*/", "", html.split(selector)[1].split("}")[0], flags=re.S)
        assert "translateX(-50%)" not in block, f"{selector} centring is one transform away from gone"
        assert "left: 0" in block and "right: 0" in block, f"{selector} is not pinned to both edges"


def test_word_style_shows_one_word_at_a_time(minimal_spec):
    """The requested subtitle: a single word, changing with the voice."""
    from shorts_factory.spec import ScriptSegment
    from shorts_factory.voice.elevenlabs import VoiceClip, WordTiming

    minimal_spec.captions.style = "word"
    segment = ScriptSegment(id="s1", text="искусственный интеллект уже здесь", start=0.0, duration=2.0)
    clip = VoiceClip(
        segment_id="s1",
        path=Path("voice.mp3"),
        start=0.0,
        duration=2.0,
        text=segment.text,
        words=[
            WordTiming("искусственный", 0.0, 0.6),
            WordTiming("интеллект", 0.6, 1.1),
            WordTiming("уже", 1.1, 1.25),
            WordTiming("здесь", 1.25, 2.0),
        ],
    )
    cues = build_cues([segment], [clip], minimal_spec.captions)

    assert [cue.text for cue in cues] == ["искусственный", "интеллект", "уже", "здесь"]
    assert all(len(cue.words) == 1 for cue in cues), "a cue carries more than one word"
    for earlier, later in zip(cues, cues[1:], strict=False):
        assert earlier.end <= later.start + 1e-6, (
            f"'{earlier.text}' is still on screen when '{later.text}' arrives"
        )
    # "уже" is spoken in 0.15s; it must still be readable.
    assert min(cue.duration for cue in cues) >= 0.2


def test_word_captions_are_white_with_a_thin_black_outline(minimal_spec, fake_media, tmp_path):
    """The look that was asked for, in the stylesheet rather than in a promise."""
    import re

    minimal_spec.captions.style = "word"
    cues = [CaptionCue(text="слово", start=1.0, end=1.4, words=[CaptionWord("слово", 1.0, 1.4)])]
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    caption_css = html.split(".caption {")[1].split("}")[0]
    assert "--caption-color: #FFFFFF" in html, "the word is not white"
    stroke = re.search(r"-webkit-text-stroke:\s*(\d+)px\s*#000000", caption_css)
    assert stroke, f"no black outline: {caption_css}"
    assert 2 <= int(stroke.group(1)) <= 4, "an outline this heavy is a slab, not a line"
    assert minimal_spec.brand.color_primary not in caption_css, "brand glow tints white glyphs"


def test_one_word_replaces_the_next_without_a_dissolve(minimal_spec, fake_media, tmp_path):
    """Cue N ends where cue N+1 starts, so a cross-fade overlaps two words."""
    minimal_spec.captions.style = "word"
    cues = [
        CaptionCue(text="раз", start=1.0, end=1.4, words=[CaptionWord("раз", 1.0, 1.4)]),
        CaptionCue(text="два", start=1.4, end=1.9, words=[CaptionWord("два", 1.4, 1.9)]),
    ]
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    frames = html.split("@keyframes anim_cap_000")[1].split("@keyframes")[0]
    on = [
        float(percent)
        for percent, opacity in re.findall(r"([\d.]+)%\{opacity:([\d.]+)", frames)
        if float(opacity) == 1.0
    ]
    assert on, f"the word never becomes visible: {frames}"
    # Full opacity from the cue's own first frame — a caption is animated on
    # the clip-local clock, so that is 0%. Anything later is a fade-in, which
    # would overlap the word that is still leaving.
    assert min(on) == pytest.approx(0.0, abs=0.05), frames


def test_a_spoken_word_hands_the_highlight_back(minimal_spec, fake_media, tmp_path):
    """Karaoke marks the current word. Holding the highlight to the end left
    every spoken word crimson, so whole lines finished red."""
    cues = [
        CaptionCue(
            text="раз два",
            start=1.0,
            end=2.0,
            words=[CaptionWord("раз", 1.0, 1.5), CaptionWord("два", 1.5, 2.0)],
        )
    ]
    minimal_spec.captions.style = "karaoke"
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    frames = html.split("@keyframes kw_cap_000_0")[1].split("}\n")[0]
    assert frames.rstrip().rstrip("}").endswith("color:var(--caption-color);"), (
        f"the word never returns to the base colour: {frames}"
    )


def test_a_clip_that_kept_its_audio_is_caught_not_shipped(minimal_spec, tmp_path):
    """The timeline mix is the only soundtrack.

    Clips are stripped on the way in and marked muted, so this should be
    unreachable — and the first cut still came back with two voices, which
    means one of those steps failed silently. Checking the result catches
    every route, including ones not thought of yet.
    """
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is what makes this checkable")

    noisy = tmp_path / "noisy.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=270x480:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-shortest", "-pix_fmt", "yuv420p", str(noisy)],
        check=False, capture_output=True, timeout=60,
    )  # fmt: skip
    if not noisy.exists():
        pytest.skip("could not build a clip with audio")

    from shorts_factory.media.ffmpeg import probe

    assert probe(noisy).has_audio, "fixture must actually carry audio"

    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, noisy))
    writer = CompositionWriter(minimal_spec, timeline, tmp_path / "comp")
    result = writer.write()

    for path in (result.directory / "media").glob("*.mp4"):
        info = probe(path)
        assert info is None or not info.has_audio, f"{path.name} reached the composition with audio"


def test_a_clip_animation_is_written_on_the_clips_own_clock(minimal_spec, fake_media, tmp_path):
    """Why captions rendered blank while looking perfect in a browser.

    HyperFrames seeks the CSS animation on a `.clip` element in *clip-local*
    time — its contract is "give timed elements a data-start so local animation
    time matches the clip". Keyframes written against the composition clock sit
    at ~0% for the element's entire life, so it never appears in a render even
    though a browser, running the same CSS on the wall clock, shows it.
    """
    cues = [CaptionCue(text="слово", start=30.0, end=30.6, words=[CaptionWord("слово", 30.0, 30.6)])]
    minimal_spec.duration_target = 46.0
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    rule = re.search(r"#el_cap_000\{animation:anim_cap_000 ([\d.]+)s", html)
    assert rule, "the caption carries no animation"
    assert float(rule.group(1)) == pytest.approx(0.6, abs=0.05), (
        "the caption animates over the whole composition; the runtime will seek it to ~0%"
    )

    frames = html.split("@keyframes anim_cap_000")[1].split("@keyframes")[0]
    visible = [
        float(percent)
        for percent, opacity in re.findall(r"([\d.]+)%\{opacity:([\d.]+)", frames)
        if float(opacity) > 0
    ]
    assert visible and max(visible) > 50, (
        f"nothing is visible in the second half of the cue's own window: {frames}"
    )


def test_the_subtitle_paints_above_the_footage_and_the_presenter(minimal_spec, fake_media, tmp_path):
    """The other half of the invisible-caption bug.

    Media shells and the host wrap carry a z-index; captions and text carried
    none, which puts them under every positioned layer that has one. Over an
    opaque clip that is invisible, under a dimmed one it is a washed-out grey —
    "the subtitles sometimes appear".
    """
    from shorts_factory.render.composition import Z_CAPTION, Z_CHIP, Z_HOST, Z_MEDIA, Z_TEXT

    assert Z_CAPTION > Z_HOST > Z_MEDIA, "the subtitle can be covered by the presenter or the footage"
    assert Z_TEXT > Z_HOST and Z_CHIP > Z_MEDIA

    cues = [CaptionCue(text="слово", start=1.0, end=2.0, words=[CaptionWord("слово", 1.0, 2.0)])]
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media), captions=cues)
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    caption_css = html.split(".caption {")[1].split("}")[0]
    assert f"z-index: {Z_CAPTION}" in caption_css, f"the caption has no layer of its own: {caption_css}"


# --------------------------------------------------------------------------- #
# Camera grammar
# --------------------------------------------------------------------------- #


def test_every_shot_travels_at_the_same_speed():
    """The vector law's "matched speed", made true by construction.

    Entry velocity has to equal the previous shot's exit velocity or the eye's
    momentum dies at the cut. Rather than tune pairs of shots, every shot moves
    at one speed and covers ground in proportion to its length — so any two
    shots, in any order, already match.
    """
    from shorts_factory.render.camera import MAX_TRAVEL, MIN_TRAVEL, travel_for

    speeds = []
    for duration in (0.9, 1.2, 1.7, 2.3, 2.6):
        travel = travel_for(duration)
        assert MIN_TRAVEL <= travel <= MAX_TRAVEL
        speeds.append(travel / duration)
    assert max(speeds) - min(speeds) < 1e-6, f"shots move at different speeds: {speeds}"
    # Outside the band the clamps take over — a four-second shot would
    # otherwise travel far enough to read as a whip.
    assert travel_for(0.2) == MIN_TRAVEL
    assert travel_for(9.0) == MAX_TRAVEL


def test_ordinary_shots_never_mirror_each_other():
    """A drift right after a drift left is the most common vector violation."""
    import re

    from shorts_factory.render.camera import move_for

    def signed(duration: float) -> float:
        move = move_for(duration=duration)
        starts = [float(x) for x in re.findall(r"translateX\(([-\d.]+)%\)", move.start)]
        ends = [float(x) for x in re.findall(r"translateX\(([-\d.]+)%\)", move.end)]
        return ends[0] - starts[0]

    directions = {signed(duration) < 0 for duration in (1.0, 1.5, 2.0, 2.6)}
    assert directions == {True}, "the current reverses between ordinary shots"


def test_a_graphic_holds_still_and_the_last_shot_rises():
    """Reserved vectors: stillness to read, upward for a conclusion."""
    from shorts_factory.render.camera import move_for

    card = move_for(duration=2.4, readable=True)
    assert card.still and card.start == card.end, "the card drifts while it is being read"

    closing = move_for(duration=2.4, closing=True)
    assert "translateY" in closing.start, "the film does not end on the reserved vector"
    assert "translateX" not in closing.start


def test_b_roll_cuts_rather_than_dissolves(minimal_spec, fake_media, tmp_path):
    """A cross-fade settles the outgoing shot and starts the next from rest.

    That is a dead beat on both sides of every cut — twenty-five of them read
    as a slide show however well the lengths were chosen. The shot has to be at
    full opacity from its own first frame to its last.
    """
    import re

    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    # A shot hidden under the presenter is held at opacity 0 on purpose;
    # measuring the cut needs one that is actually on screen.
    element = next(
        e
        for e in timeline.elements
        if e.kind == "video" and not e.props.get("hidden_under_host") and not e.props.get("dim")
    )
    frames = html.split(f"@keyframes anim_{element.id}")[1].split("@keyframes")[0]
    stops = [
        (float(percent), float(opacity))
        for percent, opacity in re.findall(r"([\d.]+)%\{opacity:([\d.]+)", frames)
    ]
    visible = [percent for percent, opacity in stops if opacity > 0]
    start = _pct_of(element.start, timeline.duration)
    end = _pct_of(element.end, timeline.duration)
    assert min(visible) == pytest.approx(start, abs=0.05), f"the shot fades in: {stops}"
    assert max(visible) == pytest.approx(end, abs=0.05), f"the shot fades out: {stops}"


def _pct_of(seconds: float, duration: float) -> float:
    return max(0.0, min(100.0, seconds / duration * 100.0))


def test_the_composition_declares_that_it_has_no_gsap_timeline(minimal_spec, fake_media, tmp_path):
    """Two costs avoided by saying so outright.

    A composition animated entirely in CSS registers no GSAP timeline. Without
    `data-no-timeline` the producer polls for one for 45 seconds before giving
    up — on every render. And the tempting workaround, registering a look-alike
    object under `window.__timelines`, hands the runtime something it may try
    to drive as a timeline; the key belongs to GSAP, one per composition.
    """
    timeline = build_timeline(minimal_spec, resolved_for(minimal_spec, fake_media))
    CompositionWriter(minimal_spec, timeline, tmp_path / "comp").write()
    html = (tmp_path / "comp" / "index.html").read_text(encoding="utf-8")

    assert "data-no-timeline" in html
    assert "window.__timelines[" not in html, "the composition registers a GSAP timeline it does not have"


# --------------------------------------------------------------------------- #
# The render pipeline's gates
# --------------------------------------------------------------------------- #


def test_check_reports_but_does_not_block_the_render(tmp_path, monkeypatch):
    """A quality alarm must not be able to cost a ninety-minute render.

    `check` runs a browser on a runner with no GPU. When it failed there, the
    run raised and no video came out at all — trading a finished video for a
    warning. Lint stays the gate: deterministic, fast, and the one that catches
    the structural faults a render genuinely cannot survive.
    """
    from shorts_factory.config import Settings
    from shorts_factory.render.hyperframes import HyperFramesRunner, StepResult

    settings = Settings.from_env({})
    runner = HyperFramesRunner(settings)
    monkeypatch.setattr(runner, "is_available", lambda: True)
    monkeypatch.setattr(runner, "lint", lambda d: StepResult("lint", True, 0))
    monkeypatch.setattr(
        runner,
        "check",
        lambda d: StepResult("check", False, 1, stdout="✗ contrast: 3 error(s)", stderr="WebGL unavailable"),
    )

    output = tmp_path / "out.mp4"

    def fake_render(project_dir, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 32)
        return StepResult("render", True, 0)

    monkeypatch.setattr(runner, "render", fake_render)
    monkeypatch.setenv("HYPERFRAMES_SKIP_CHECK", "0")

    result = runner.run_pipeline(tmp_path, output)

    assert result.ok, "a check failure stopped the render"
    assert any("check failed" in warning for warning in result.warnings), (
        "the failure was swallowed instead of reported"
    )


def test_lint_still_stops_the_render(tmp_path, monkeypatch):
    """Overlapping clips are not a matter of taste; the renderer refuses them."""
    from shorts_factory.config import Settings
    from shorts_factory.errors import RenderError
    from shorts_factory.render.hyperframes import HyperFramesRunner, StepResult

    runner = HyperFramesRunner(Settings.from_env({}))
    monkeypatch.setattr(runner, "is_available", lambda: True)
    monkeypatch.setattr(
        runner, "lint", lambda d: StepResult("lint", False, 1, stdout="✗ overlapping_clips_same_track")
    )

    with pytest.raises(RenderError):
        runner.run_pipeline(tmp_path, tmp_path / "out.mp4")


def test_a_failed_step_shows_what_it_found_not_just_browser_noise():
    """The findings go to stdout; the browser complains on stderr.

    Preferring stderr made a failed check unreadable: the run died with a page
    of "WebGL unavailable" and no hint of what had been found.
    """
    from shorts_factory.render.hyperframes import StepResult

    step = StepResult("check", False, 1, stdout="✗ caption_overflow at t=3s", stderr="WebGL unavailable")

    assert "caption_overflow" in step.tail
    assert "WebGL" in step.tail
