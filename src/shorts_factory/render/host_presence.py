"""Host presentation modes — SPLIT / FULL_HOST / FULL_FOOTAGE.

Replaces the old Pulse Ring. The presenter is a rectangular video layer whose
layout and visibility follow per-segment ``mode`` values:

* ``split`` — host fixed in the lower 40% band; B-roll owns the upper 60%
* ``full_host`` — host covers the full frame (studio avatar, no brand ornaments)
* ``full_footage`` — host hidden; B-roll is fullscreen

Brand ornaments (logo watermark, orbital semi-ovals) are off by default —
they fought the footage and read as clutter on Shorts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..config import VIDEO_HEIGHT, VIDEO_WIDTH
from ..spec import Spec

HostMode = Literal["split", "full_host", "full_footage"]

HOST_MODES = frozenset({"split", "full_host", "full_footage"})
HOST_ON_MODES = frozenset({"split", "full_host"})

# Lower band = 40% of the 9:16 frame → host always sits under the footage plane.
HOST_LOWER_RATIO = 0.40
HOST_LOWER_HEIGHT = int(round(VIDEO_HEIGHT * HOST_LOWER_RATIO))  # 768
HOST_LOWER_TOP = VIDEO_HEIGHT - HOST_LOWER_HEIGHT  # 1152
SPLIT_UPPER_HEIGHT = HOST_LOWER_TOP  # 1152

FADE_S = 0.28
MAX_MODE_STRETCH_S = 12.0
MODE_DWELL_MIN_S = 4.0
MODE_DWELL_MAX_S = 7.0
SHOT_TARGET_MIN_S = 1.5
SHOT_TARGET_MAX_S = 2.4


@dataclass(frozen=True)
class ModeWindow:
    start: float
    end: float
    mode: HostMode
    segment_id: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class HostPlan:
    """Timed host layout for the composition writer."""

    windows: list[ModeWindow] = field(default_factory=list)
    present: list[tuple[float, float]] = field(default_factory=list)

    @property
    def visible(self) -> bool:
        return bool(self.present)


def plan_host(spec: Spec) -> HostPlan:
    """Derive mode windows from script segments (and optional avatar.segments)."""
    if not spec.avatar.enabled:
        return HostPlan()

    segments = list(spec.all_segments)
    if not segments:
        return HostPlan(
            windows=[ModeWindow(0.0, spec.duration_target, "full_host", "full")],
            present=[(0.0, spec.duration_target)],
        )

    windows: list[ModeWindow] = []
    for segment in segments:
        mode = _segment_mode(segment)
        windows.append(
            ModeWindow(
                start=segment.start,
                end=segment.end,
                mode=mode,
                segment_id=segment.id,
            )
        )

    # Fill tiny gaps between segments with the previous mode so CSS doesn't flicker.
    present = _merge_present([(w.start, w.end) for w in windows if w.mode in HOST_ON_MODES])
    return HostPlan(windows=windows, present=present)


def split_upper_layout() -> dict[str, object]:
    return {
        "top": 0,
        "left": 0,
        "width": VIDEO_WIDTH,
        "height": SPLIT_UPPER_HEIGHT,
        "fit": "cover",
    }


def host_lower_layout() -> dict[str, object]:
    return {
        "top": HOST_LOWER_TOP,
        "left": 0,
        "width": VIDEO_WIDTH,
        "height": HOST_LOWER_HEIGHT,
        "fit": "cover",
        "mode": "split",
    }


def host_fullscreen_layout() -> dict[str, object]:
    return {
        "top": 0,
        "left": 0,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "fit": "cover",
        "mode": "full_host",
    }


def orbital_arcs_svg(*, primary: str = "#E11D48") -> str:
    """Thin orbital arcs — brand accent without neon bloom."""
    return (
        f'<svg class="host-orbitals" viewBox="0 0 1080 920" width="1080" height="920" '
        f'aria-hidden="true">'
        f'<path d="M80 780 C280 620, 800 620, 1000 780" fill="none" stroke="{primary}" '
        f'stroke-width="1.5" opacity="0.45"/>'
        f'<path d="M140 820 C360 700, 720 700, 940 820" fill="none" stroke="{primary}" '
        f'stroke-width="1" opacity="0.28"/>'
        f'<circle cx="220" cy="700" r="2.2" fill="{primary}" opacity="0.55"/>'
        f'<circle cx="860" cy="710" r="1.8" fill="{primary}" opacity="0.4"/>'
        f'<circle cx="540" cy="640" r="1.5" fill="{primary}" opacity="0.35"/>'
        f"</svg>"
    )


def host_hidden_layout() -> dict[str, object]:
    """Collapse the host box — HyperFrames often drops opacity alone."""
    return {
        "top": VIDEO_HEIGHT,
        "left": 0,
        "width": VIDEO_WIDTH,
        "height": 0,
        "fit": "cover",
        "mode": "full_footage",
    }


def host_visibility_css(plan: HostPlan, *, duration: float) -> str:
    """Deprecated shim — prefer ``host_chrome_css`` (single animation)."""
    return host_chrome_css(plan, duration=duration)


def host_layout_css(plan: HostPlan, *, duration: float) -> str:
    """Deprecated shim — layout is folded into ``host_chrome_css``."""
    return ""


def host_chrome_css(plan: HostPlan, *, duration: float) -> str:
    """One animation for host geometry + opacity.

    HyperFrames / Chrome capture keeps only a single ``animation`` on
    ``.host-wrap``. Emitting ``host_presence`` then ``host_layout`` made the
    second rule win, so FULL_FOOTAGE never hid the presenter. Fold both into
    ``host_chrome`` and collapse the box (height:0) during FULL_FOOTAGE so
    the host cannot paint over fullscreen B-roll even if opacity is dropped.
    """
    if duration <= 0 or not plan.windows:
        return (
            "@keyframes host_chrome{0%{opacity:0;top:1920px;left:0px;width:1080px;height:0px}"
            "100%{opacity:0;top:1920px;left:0px;width:1080px;height:0px}}\n"
            ".host-wrap{animation:host_chrome "
            f"{max(duration, 0.001):.3f}s step-end 0s 1 normal both;}}"
        )

    stops: list[tuple[float, str]] = []
    for window in plan.windows:
        if window.mode == "full_footage":
            layout = host_hidden_layout()
            opacity = "opacity:0;"
        elif window.mode == "full_host":
            layout = host_fullscreen_layout()
            opacity = "opacity:1;"
        else:
            layout = host_lower_layout()
            opacity = "opacity:1;"
        body = opacity + _layout_body(layout)
        stops.append((_pct(window.start, duration), body))
        stops.append((_pct(max(window.start, window.end - 0.001), duration), body))

    if not stops:
        return ""
    stops = _dedupe(stops)
    # Ensure 0% / 100% terminals exist for stable capture.
    first_body = stops[0][1]
    last_body = stops[-1][1]
    if stops[0][0] > 0.0:
        stops.insert(0, (0.0, first_body))
    if stops[-1][0] < 100.0:
        stops.append((100.0, last_body))
    stops = _dedupe(stops)
    # step-end: hold geometry until the next keyframe — linear morph between
    # FULL_HOST (1920) and SPLIT (768) makes the presenter "jump" in the band.
    parts = [f"{p:.4f}%{{{body}}}" for p, body in stops]
    return (
        f"@keyframes host_chrome{{{''.join(parts)}}}\n"
        f".host-wrap{{animation:host_chrome {duration:.3f}s step-end 0s 1 normal both;}}"
    )


def mode_at(plan: HostPlan, time: float) -> HostMode:
    for window in plan.windows:
        if window.start - 0.01 <= time < window.end + 0.01:
            return window.mode
    return "full_footage"


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _segment_mode(segment: object) -> HostMode:
    mode = getattr(segment, "mode", None)
    if isinstance(mode, str) and mode in HOST_MODES:
        return mode  # type: ignore[return-value]
    # Legacy: on_camera True → split; False with avatar → still may be on via segments
    if getattr(segment, "on_camera", False):
        return "full_host"
    return "split"


def _merge_present(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not windows:
        return []
    ordered = sorted(windows, key=lambda pair: pair[0])
    merged: list[list[float]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 0.35:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _layout_body(layout: dict[str, object]) -> str:
    return (
        f"top:{layout['top']}px;left:{layout['left']}px;"
        f"width:{layout['width']}px;height:{layout['height']}px;"
    )


def _pct(time: float, duration: float) -> float:
    return max(0.0, min(100.0, (time / duration) * 100.0 if duration else 0.0))


def _dedupe(stops: list[tuple[float, str]]) -> list[tuple[float, str]]:
    seen: set[str] = set()
    out: list[tuple[float, str]] = []
    for percent, body in sorted(stops, key=lambda stop: stop[0]):
        key = f"{percent:.4f}"
        if key in seen:
            out = [(p, b) for p, b in out if f"{p:.4f}" != key]
        seen.add(key)
        out.append((percent, body))
    return out
