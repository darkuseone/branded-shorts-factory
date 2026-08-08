"""The master timeline: one flat, ordered list of everything on screen.

Building this structure separately from the HTML keeps the renderer dumb and
makes the whole plan inspectable — `plan` writes it to JSON, tests assert on it,
and the composition writer just walks it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import FPS, VIDEO_HEIGHT, VIDEO_WIDTH
from ..logging_utils import get_logger
from ..resolver import ResolvedVisual
from ..spec import Spec
from ..voice.captions import CaptionCue
from ..voice.elevenlabs import SfxClip, VoiceClip
from ..voice.heygen import AvatarClip

log = get_logger("timeline")

Kind = Literal["video", "image", "avatar", "caption", "text", "logo", "shape", "audio"]

# Track layout (higher index = drawn later = on top).
TRACK_BACKGROUND = 0
TRACK_BROLL = 1
TRACK_OVERLAY = 2
TRACK_AVATAR = 3
TRACK_CAPTIONS = 4
TRACK_BRAND = 5
TRACK_CTA = 6
TRACK_MEME = 7
TRACK_DATA_CHIP = 8
TRACK_AUDIO_VOICE = 10
TRACK_AUDIO_SFX = 11
TRACK_AUDIO_MUSIC = 12

# YouTube's own UI eats the bottom of the frame; keep content above it.
SAFE_MARGIN = 96
BOTTOM_UI_RESERVE = 340


@dataclass
class Element:
    """One timed thing on the timeline."""

    id: str
    kind: Kind
    start: float
    duration: float
    track: int
    src: Path | None = None
    text: str = ""
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def end(self) -> float:
        return self.start + self.duration

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "track": self.track,
            "src": str(self.src) if self.src else None,
            "text": self.text,
            "props": self.props,
        }


@dataclass
class Timeline:
    composition_id: str
    title: str
    duration: float
    width: int = VIDEO_WIDTH
    height: int = VIDEO_HEIGHT
    fps: int = FPS
    elements: list[Element] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, element: Element) -> None:
        self.elements.append(element)

    def by_track(self) -> list[Element]:
        return sorted(self.elements, key=lambda e: (e.track, e.start, e.id))

    @property
    def visual_elements(self) -> list[Element]:
        return [e for e in self.by_track() if e.kind != "audio"]

    @property
    def audio_elements(self) -> list[Element]:
        return [e for e in self.by_track() if e.kind == "audio"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "composition_id": self.composition_id,
            "title": self.title,
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "warnings": self.warnings,
            "elements": [element.to_dict() for element in self.by_track()],
        }


_POSITION_LAYOUT: dict[str, dict[str, Any]] = {
    "fullscreen": {"top": 0, "left": 0, "width": VIDEO_WIDTH, "height": VIDEO_HEIGHT, "fit": "cover"},
    "background": {"top": 0, "left": 0, "width": VIDEO_WIDTH, "height": VIDEO_HEIGHT, "fit": "cover"},
    "top": {"top": 0, "left": 0, "width": VIDEO_WIDTH, "height": 1056, "fit": "cover"},
    "split_upper": {"top": 0, "left": 0, "width": VIDEO_WIDTH, "height": 1056, "fit": "cover"},
    "center": {"top": 520, "left": 0, "width": VIDEO_WIDTH, "height": 880, "fit": "cover"},
    "bottom": {"top": 1056, "left": 0, "width": VIDEO_WIDTH, "height": 864, "fit": "cover"},
    "left": {"top": 480, "left": 0, "width": 700, "height": 960, "fit": "cover"},
    "right": {"top": 480, "left": 380, "width": 700, "height": 960, "fit": "cover"},
    "pip": {"top": 220, "left": 620, "width": 380, "height": 380, "fit": "cover", "radius": 32},
}

_AVATAR_LAYOUT: dict[str, dict[str, Any]] = {
    "bottom_right": {"anchor": "split", "mode": "split"},
    "bottom_left": {"anchor": "split", "mode": "split"},
    "bottom_center": {"anchor": "split", "mode": "split"},
    "top_right": {"anchor": "split", "mode": "split"},
    "center": {"anchor": "full_host", "mode": "full_host"},
    "fullscreen": {"anchor": "full_host", "mode": "full_host"},
    "split": {"anchor": "split", "mode": "split"},
}


def build_timeline(
    spec: Spec,
    resolved: list[ResolvedVisual],
    *,
    voice_clips: list[VoiceClip] | None = None,
    sfx_clips: list[SfxClip] | None = None,
    captions: list[CaptionCue] | None = None,
    avatar: AvatarClip | None = None,
    avatar_start: float = 0.0,
    music_path: Path | None = None,
) -> Timeline:
    """Assemble every resolved piece into one renderable timeline."""
    timeline = Timeline(
        composition_id=_safe_id(spec.id),
        title=spec.title,
        duration=spec.duration_target,
    )

    _add_background(timeline, spec)
    _add_visuals(timeline, spec, resolved)
    _add_avatar(timeline, spec, avatar, avatar_start)
    _add_captions(timeline, spec, captions or [])
    from .data_chips import add_data_chips  # lazy: avoid circular import with Element

    add_data_chips(timeline, spec)
    _add_brand(timeline, spec)
    _add_cta(timeline, spec)
    _add_audio(timeline, spec, voice_clips or [], sfx_clips or [], music_path)

    covered = sum(
        element.duration
        for element in timeline.elements
        if element.track in {TRACK_BACKGROUND, TRACK_BROLL} and element.kind in {"video", "image"}
    )
    if covered < spec.duration_target * 0.9:
        timeline.warnings.append(
            f"only {covered:.1f}s of {spec.duration_target:.1f}s has b-roll; "
            "the brand backdrop fills the rest"
        )
    return timeline


# --------------------------------------------------------------------------- #
# Track builders
# --------------------------------------------------------------------------- #


def _add_background(timeline: Timeline, spec: Spec) -> None:
    """A branded backdrop under everything, so gaps never render as black."""
    timeline.add(
        Element(
            id="backdrop",
            kind="shape",
            start=0.0,
            duration=spec.duration_target,
            track=TRACK_BACKGROUND,
            props={
                "role": "backdrop",
                "color": spec.brand.color_background,
                "accent": spec.brand.color_primary,
            },
        )
    )


def _add_visuals(timeline: Timeline, spec: Spec, resolved: list[ResolvedVisual]) -> None:
    for item in resolved:
        if not item.usable or item.asset is None:
            timeline.warnings.append(f"visual {item.visual.id} has no usable asset and was dropped")
            continue

        visual = item.visual
        asset = item.asset
        position = _resolve_visual_position(spec, visual)
        layout = dict(_POSITION_LAYOUT.get(position, _POSITION_LAYOUT["fullscreen"]))
        # SPLIT upper-band and other main planes share TRACK_BROLL (timed, no overlap).
        # PiP/side overlays keep TRACK_OVERLAY. Data chips use TRACK_DATA_CHIP.
        is_main = position in {
            "fullscreen",
            "background",
            "split_upper",
            "top",
            "center",
            "bottom",
            "auto",
        }
        if visual.type == "meme":
            track = TRACK_MEME
        elif is_main:
            track = TRACK_BROLL
        else:
            track = TRACK_OVERLAY

        props: dict[str, Any] = {
            "layout": layout,
            "motion": visual.motion,
            "source": asset.candidate.source,
            "license": asset.candidate.license,
            "credit": asset.candidate.author,
            "review": item.qa.outcome == "manual_review",
            "position": position,
        }
        # Dim/hide b-roll under FULL_HOST so the presenter owns the frame.
        if visual.segment_ref:
            segment = spec.segment_by_id(visual.segment_ref)
        else:
            segment = spec.segment_at(visual.start)
        if segment is not None and segment.mode == "full_host" and visual.type != "meme":
            props["dim"] = True
        if asset.is_video:
            # Loop or trim to the slot; the renderer honours playbackRate/loop.
            props["loop"] = bool(asset.duration and asset.duration + 0.2 < visual.duration)
            props["source_duration"] = round(asset.duration, 3)
        if visual.type == "meme":
            props["meme"] = True
            # Punch trim from the catalog (resolver encodes it in visual.notes).
            for chunk in (visual.notes or "").split("|"):
                if chunk.startswith("trim_start="):
                    with contextlib.suppress(ValueError):
                        props["trim_start"] = float(chunk.split("=", 1)[1])
                if chunk.startswith("max_use="):
                    with contextlib.suppress(ValueError):
                        props["playback_duration"] = float(chunk.split("=", 1)[1])

        timeline.add(
            Element(
                id=visual.id,
                kind="video" if asset.is_video else "image",
                start=visual.start,
                duration=visual.duration,
                track=track,
                src=asset.path,
                props=props,
            )
        )


def _resolve_visual_position(spec: Spec, visual: Any) -> str:
    """Map auto/fullscreen slots to split_upper when the spoken mode is SPLIT."""
    position = visual.position or "fullscreen"
    segment = spec.segment_by_id(visual.segment_ref) if visual.segment_ref else spec.segment_at(visual.start)
    mode = getattr(segment, "mode", None) if segment else None
    if position in {"auto", "fullscreen", "background", "top"} and mode == "split":
        return "split_upper"
    if position == "auto":
        return "fullscreen" if mode == "full_footage" else "split_upper"
    return position


def _add_avatar(timeline: Timeline, spec: Spec, avatar: AvatarClip | None, start: float = 0.0) -> None:
    """Place the presenter; layout/visibility are driven by host mode CSS."""
    if avatar is None or not spec.avatar.enabled:
        return
    layout = dict(_AVATAR_LAYOUT.get(spec.avatar.position, _AVATAR_LAYOUT["split"]))
    layout["scale"] = spec.avatar.scale
    duration = avatar.duration or spec.spoken_duration or spec.duration_target
    start = max(0.0, min(start, spec.duration_target))
    timeline.add(
        Element(
            id="avatar",
            kind="avatar",
            start=start,
            duration=min(duration, spec.duration_target - start),
            track=TRACK_AVATAR,
            src=avatar.path,
            props={
                "layout": layout,
                "transparent": avatar.transparent,
                "muted": True,
            },
        )
    )


def _add_captions(timeline: Timeline, spec: Spec, cues: list[CaptionCue]) -> None:
    if not spec.captions.enabled or not cues:
        return
    ordered = sorted(cues, key=lambda cue: cue.start)
    for index, cue in enumerate(ordered):
        start = round(cue.start, 3)
        end = round(cue.end, 3)
        if index + 1 < len(ordered):
            # Snap to the next cue so rounded float tails never overlap.
            end = min(end, round(ordered[index + 1].start, 3))
        duration = max(end - start, 0.05)
        timeline.add(
            Element(
                id=f"cap_{index:03d}",
                kind="caption",
                start=start,
                duration=duration,
                track=TRACK_CAPTIONS,
                text=cue.text,
                props={
                    "style": spec.captions.style,
                    "words": [
                        {"text": word.text, "start": round(word.start, 3), "end": round(word.end, 3)}
                        for word in cue.words
                    ],
                    "segment_id": cue.segment_id,
                },
            )
        )


def _add_brand(timeline: Timeline, spec: Spec) -> None:
    brand = spec.brand
    if brand.logo and brand.logo_position != "none":
        logo_path = Path(brand.logo)
        timeline.add(
            Element(
                id="logo",
                kind="logo",
                start=0.0,
                duration=spec.duration_target,
                track=TRACK_BRAND,
                src=logo_path,
                props={"position": brand.logo_position, "opacity": brand.watermark_opacity},
            )
        )
        if not logo_path.exists():
            timeline.warnings.append(f"brand logo not found: {logo_path}")

    if brand.lower_third:
        timeline.add(
            Element(
                id="lower_third",
                kind="text",
                start=0.6,
                duration=min(4.0, spec.duration_target),
                track=TRACK_BRAND,
                text=spec.title,
                props={"role": "lower_third", "color": brand.color_primary},
            )
        )

    if brand.outro_card:
        length = min(2.5, max(1.2, spec.duration_target * 0.08))
        timeline.add(
            Element(
                id="outro",
                kind="text",
                start=max(0.0, spec.duration_target - length),
                duration=length,
                # CTA track — logo owns TRACK_BRAND for the full duration.
                track=TRACK_CTA,
                text=spec.title,
                props={"role": "outro", "color": brand.color_primary, "accent": brand.color_accent},
            )
        )


def _add_cta(timeline: Timeline, spec: Spec) -> None:
    if not spec.cta or not spec.cta.text:
        return
    timeline.add(
        Element(
            id="cta",
            kind="text",
            start=spec.cta.start,
            duration=spec.cta.duration,
            track=TRACK_CTA,
            text=spec.cta.text,
            props={
                "role": "cta",
                "style": spec.cta.style,
                "color": spec.brand.color_accent,
                "url": spec.cta.url,
            },
        )
    )


def _add_audio(
    timeline: Timeline,
    spec: Spec,
    voice_clips: list[VoiceClip],
    sfx_clips: list[SfxClip],
    music_path: Path | None,
) -> None:
    for clip in voice_clips:
        timeline.add(
            Element(
                id=f"vo_{clip.segment_id}",
                kind="audio",
                start=clip.start,
                duration=clip.duration,
                track=TRACK_AUDIO_VOICE,
                src=clip.path,
                props={"role": "narration", "volume": 1.0},
            )
        )

    for clip in sfx_clips:
        timeline.add(
            Element(
                id=clip.fx_id,
                kind="audio",
                start=clip.start,
                duration=clip.duration,
                track=TRACK_AUDIO_SFX,
                src=clip.path,
                props={"role": "sfx", "volume": clip.volume},
            )
        )

    if music_path is not None:
        music = spec.music
        # Duck under narration rather than riding a single flat level.
        duck_windows = (
            [
                {"start": round(clip.start, 3), "end": round(clip.start + clip.duration, 3)}
                for clip in voice_clips
            ]
            if music.ducking
            else []
        )
        timeline.add(
            Element(
                id="music",
                kind="audio",
                start=music.start_at,
                duration=max(0.5, spec.duration_target - music.start_at),
                track=TRACK_AUDIO_MUSIC,
                src=music_path,
                props={
                    "role": "music",
                    "volume": music.volume,
                    "duck_to": music.duck_to,
                    "fade_in": music.fade_in,
                    "fade_out": music.fade_out,
                    "duck_windows": duck_windows,
                    "loop": True,
                },
            )
        )


def use_mixed_audio(timeline: Timeline, mix_path: Path, duration: float) -> None:
    """Swap the separate audio stems for one pre-mixed track.

    Called when FFmpeg produced the mix (with real ducking and loudness
    normalisation); the renderer then only has to play a single file.
    """
    timeline.elements = [element for element in timeline.elements if element.kind != "audio"]
    timeline.add(
        Element(
            id="mix",
            kind="audio",
            start=0.0,
            duration=duration,
            track=TRACK_AUDIO_VOICE,
            src=mix_path,
            props={"role": "mix", "volume": 1.0},
        )
    )


def _safe_id(raw: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in raw).strip("_")
    return cleaned or "short"
