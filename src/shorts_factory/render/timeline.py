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
#: Data chips share no track with top tablets — HyperFrames rejects same-track overlap.
TRACK_CHIPS = 8
#: Subscribe badge — own track so absolute-clock presence never overlaps CTA text.
TRACK_SUBSCRIBE = 9
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
    # Hard 50/50 split (commentary Shorts): footage owns the top half only.
    "split": {"top": 0, "left": 0, "width": VIDEO_WIDTH, "height": VIDEO_HEIGHT // 2, "fit": "cover"},
    # Action Stage — above the bottom-center oval (oval top ≈ 927 with ratio 0.34).
    "top": {"top": 48, "left": 0, "width": VIDEO_WIDTH, "height": 820, "fit": "cover", "radius": 0},
    "center": {"top": 520, "left": 0, "width": VIDEO_WIDTH, "height": 880, "fit": "cover"},
    "bottom": {"top": 900, "left": 0, "width": VIDEO_WIDTH, "height": 700, "fit": "cover"},
    "left": {"top": 480, "left": 0, "width": 700, "height": 960, "fit": "cover"},
    "right": {"top": 480, "left": 380, "width": 700, "height": 960, "fit": "cover"},
    "pip": {"top": 220, "left": 620, "width": 380, "height": 380, "fit": "cover", "radius": 32},
}

_AVATAR_LAYOUT: dict[str, dict[str, Any]] = {
    "bottom_right": {"anchor": "bottom-right", "right": SAFE_MARGIN, "bottom": BOTTOM_UI_RESERVE},
    "bottom_left": {"anchor": "bottom-left", "left": SAFE_MARGIN, "bottom": BOTTOM_UI_RESERVE},
    "bottom_center": {"anchor": "bottom-center", "bottom": BOTTOM_UI_RESERVE},
    "top_right": {"anchor": "top-right", "right": SAFE_MARGIN, "top": 200},
    "center": {"anchor": "center"},
    "fullscreen": {"anchor": "fullscreen"},
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
    # Aleko commentary style (no ring): skip data chips — reference has clean captions only.
    if spec.ring.enabled:
        from .data_chips import add_data_chips  # lazy: avoid circular import with Element

        add_data_chips(timeline, spec)
    _add_brand(timeline, spec)
    _add_cta(timeline, spec)
    _add_audio(timeline, spec, voice_clips or [], sfx_clips or [], music_path)

    covered = sum(
        element.duration
        for element in timeline.elements
        if element.track in {TRACK_BACKGROUND, TRACK_BROLL, TRACK_OVERLAY}
        and element.kind in {"video", "image"}
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
    """Place resolved media. Ring-on stage forces top band; aleko mode honors fullscreen/split."""
    from .ring import RingConfig, action_stage_box

    stage = action_stage_box(RingConfig(), ring_enabled=spec.ring.enabled)
    # Only rewrite fullscreen→top when the Pulse Ring oval owns the lower third.
    force_top = _stage_layout_job(spec) and bool(spec.ring.enabled)

    for item in resolved:
        if not item.usable or item.asset is None:
            timeline.warnings.append(f"visual {item.visual.id} has no usable asset and was dropped")
            continue

        visual = item.visual
        asset = item.asset
        position = visual.position or "top"
        # Reference oval stage: fullscreen under the oval is forbidden except short memes.
        if force_top and visual.type != "meme" and position in {"fullscreen", "background"}:
            position = "top"
        layout = dict(_POSITION_LAYOUT.get(position, _POSITION_LAYOUT["top"]))
        if position == "top" and force_top:
            layout = {
                "top": stage["top"],
                "left": stage["left"],
                "width": stage["width"],
                "height": stage["height"],
                "fit": "cover",
            }
        elif position == "split":
            layout = dict(_POSITION_LAYOUT["split"])
        is_full = position in {"fullscreen", "background"}
        track = TRACK_MEME if visual.type == "meme" else (TRACK_BROLL if is_full else TRACK_OVERLAY)

        props: dict[str, Any] = {
            "layout": layout,
            "motion": visual.motion,
            "source": asset.candidate.source,
            "license": asset.candidate.license,
            "credit": asset.candidate.author,
            "review": item.qa.outcome == "manual_review",
            "stage": "action" if position in {"top", "split"} else position,
        }
        if asset.is_video:
            props["loop"] = bool(asset.duration and asset.duration + 0.2 < visual.duration)
            props["source_duration"] = round(asset.duration, 3)
        if visual.type == "meme":
            props["meme"] = True
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


def _stage_layout_job(spec: Spec) -> bool:
    """True when this Short uses the reference Action Stage (oval bottom, action top)."""
    rubric = (getattr(spec, "rubric", None) or spec.topic or "").strip().lower()
    tokens = ("ai", "it", "tech", "news", "технологии", "наука", "openai", "huggingface")
    return any(token in rubric for token in tokens)


def _add_avatar(timeline: Timeline, spec: Spec, avatar: AvatarClip | None, start: float = 0.0) -> None:
    """Place the presenter — Pulse Ring, or aleko-style fullscreen / split windows."""
    if avatar is None or not spec.avatar.enabled:
        return
    from dataclasses import replace

    from .ring import RingConfig, plan_ring

    ring_on = bool(spec.ring.enabled)
    cfg = replace(RingConfig(), enabled=ring_on, continuous_on_vo=True)
    if spec.ring.diameter_ratio is not None:
        cfg = replace(cfg, diameter_ratio=spec.ring.diameter_ratio)
    if spec.ring.anchor:
        cfg = replace(cfg, default_anchor=spec.ring.anchor)

    if not ring_on:
        _add_avatar_aleko_windows(timeline, spec, avatar, start)
        return

    # Timing always uses a continuous VO window; geometry depends on ring_on.
    timing = plan_ring(spec, replace(cfg, enabled=True, continuous_on_vo=True))
    if timing.windows:
        win_start = timing.windows[0][0]
        win_end = timing.windows[-1][1]
        if start > win_start + 0.05 and start < win_end - 0.05:
            duration = win_end - start
        else:
            start = win_start
            duration = win_end - start
    else:
        duration = avatar.duration or spec.spoken_duration or spec.duration_target
        start = max(0.0, min(start, spec.duration_target))

    position = spec.avatar.position if spec.avatar.position != "bottom_right" else "bottom_center"
    if _stage_layout_job(spec):
        position = "bottom_center"
    layout = dict(_AVATAR_LAYOUT.get(position, _AVATAR_LAYOUT["bottom_center"]))
    layout["scale"] = spec.avatar.scale
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


def _add_avatar_aleko_windows(
    timeline: Timeline,
    spec: Spec,
    avatar: AvatarClip,
    start: float,
) -> None:
    """One avatar clip per host beat: fullscreen or bottom-half split."""
    wanted = set(spec.avatar.segments) if spec.avatar.segments else None
    if wanted is not None:
        host_segs = [s for s in spec.all_segments if s.id in wanted]
    else:
        on_cam = [s for s in spec.all_segments if s.on_camera]
        host_segs = on_cam or list(spec.all_segments)

    for index, segment in enumerate(host_segs):
        seg_start = max(segment.start, start) if index == 0 else segment.start
        seg_end = segment.end
        if seg_end - seg_start < 0.35:
            continue
        layout = _aleko_host_layout(spec, segment)
        timeline.add(
            Element(
                id=f"avatar_{segment.id}",
                kind="avatar",
                start=seg_start,
                duration=min(seg_end - seg_start, spec.duration_target - seg_start),
                track=TRACK_AVATAR,
                src=avatar.path,
                props={
                    "layout": layout,
                    "transparent": avatar.transparent,
                    "muted": True,
                    "no_ring": True,
                    "trim_start": max(0.0, seg_start),  # keep VO lip-sync vs source timeline
                },
            )
        )


def _aleko_host_layout(spec: Spec, segment: Any) -> dict[str, Any]:
    """Fullscreen host unless a split/top visual overlaps this beat."""
    from .ring import host_bottom_box, host_fullscreen_box

    overlapping = [
        visual
        for visual in spec.visuals
        if visual.start < segment.end - 0.05
        and (visual.start + visual.duration) > segment.start + 0.05
        and visual.position in {"split", "top"}
    ]
    layout = host_bottom_box() if overlapping else host_fullscreen_box()
    layout["scale"] = spec.avatar.scale
    return layout


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
        # Reference stage: last ~2s are the subscribe badge — skip title outro.
        pass


def _add_cta(timeline: Timeline, spec: Spec) -> None:
    """CTA text then a subscribe badge image in the last ~2s (no same-track overlap).

    HyperFrames often drops late CSS-only text chips; a pre-baked PNG with an
    explicit layout box paints reliably in the last two seconds.
    """
    sub_start = max(0.0, spec.duration_target - 2.0)
    if spec.cta and spec.cta.text:
        cta_start = spec.cta.start
        # Keep CTA off the subscribe window so HyperFrames does not see overlap.
        cta_end = min(spec.cta.end, sub_start)
        cta_duration = cta_end - cta_start
        if cta_duration >= 0.5:
            timeline.add(
                Element(
                    id="cta",
                    kind="text",
                    start=cta_start,
                    duration=cta_duration,
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
        else:
            # CTA lands in the last 2s — show only subscribe badge.
            sub_start = min(sub_start, cta_start)

    sub_duration = max(1.2, spec.duration_target - sub_start)
    badge = Path(__file__).resolve().parents[3] / "brand" / "subscribe-badge.png"
    badge_w, badge_h = 440, 100
    if badge.is_file():
        timeline.add(
            Element(
                id="subscribe",
                kind="image",
                start=sub_start,
                duration=sub_duration,
                track=TRACK_SUBSCRIBE,
                src=badge,
                props={
                    "role": "subscribe",
                    "absolute_clock": True,
                    "layout": {
                        "top": VIDEO_HEIGHT - BOTTOM_UI_RESERVE - badge_h - 28,
                        "left": (VIDEO_WIDTH - badge_w) // 2,
                        "width": badge_w,
                        "height": badge_h,
                        "fit": "contain",
                    },
                },
            )
        )
        return

    timeline.add(
        Element(
            id="subscribe",
            kind="text",
            start=sub_start,
            duration=sub_duration,
            track=TRACK_SUBSCRIBE,
            text="Подписаться",
            props={
                "role": "subscribe",
                "style": "badge",
                "absolute_clock": True,
                "color": spec.brand.color_primary,
                "accent": spec.brand.color_accent,
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
