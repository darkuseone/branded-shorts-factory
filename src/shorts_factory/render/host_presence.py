"""Host presentation modes — SPLIT / FULL_HOST / FULL_FOOTAGE.

Replaces the old Pulse Ring. The presenter is a rectangular video layer whose
layout and visibility follow per-segment ``mode`` values:

* ``split`` — host in the lower ~45% band; B-roll fills the upper band
* ``full_host`` — host covers the full frame
* ``full_footage`` — host hidden; B-roll is fullscreen

Thin orbital arcs (not a neon pulse ring) sit around the host in SPLIT /
FULL_HOST as a light brand accent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..config import VIDEO_HEIGHT, VIDEO_WIDTH
from ..spec import Spec

HostMode = Literal["split", "full_host", "full_footage"]

HOST_MODES = frozenset({"split", "full_host", "full_footage"})
HOST_ON_MODES = frozenset({"split", "full_host"})

# Lower band ≈ 45% of the 9:16 frame.
HOST_LOWER_RATIO = 0.45
HOST_LOWER_HEIGHT = int(round(VIDEO_HEIGHT * HOST_LOWER_RATIO))  # 864
HOST_LOWER_TOP = VIDEO_HEIGHT - HOST_LOWER_HEIGHT  # 1056
SPLIT_UPPER_HEIGHT = HOST_LOWER_TOP  # 1056

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


def host_visibility_css(plan: HostPlan, *, duration: float) -> str:
    """Full-span keyframes: host opacity follows present windows."""
    if duration <= 0 or not plan.present:
        return (
            "@keyframes host_presence{0%{opacity:0}100%{opacity:0}}\n"
            ".host-wrap{animation:host_presence "
            f"{max(duration, 0.001):.3f}s linear 0s 1 normal both;}}"
        )

    stops: list[tuple[float, str]] = [(0.0, "opacity:0;")]
    for start, end in plan.present:
        fade = min(FADE_S, max(0.12, (end - start) * 0.15))
        stops.append((_pct(max(0.0, start - fade), duration), "opacity:0;"))
        stops.append((_pct(start, duration), "opacity:1;"))
        stops.append((_pct(end, duration), "opacity:1;"))
        stops.append((_pct(min(duration, end + fade), duration), "opacity:0;"))
    stops.append((100.0, "opacity:0;"))
    stops = _dedupe(stops)
    parts = [f"{p:.4f}%{{{body}}}" for p, body in stops]
    return (
        f"@keyframes host_presence{{{''.join(parts)}}}\n"
        f".host-wrap{{animation:host_presence {duration:.3f}s linear 0s 1 normal both;}}"
    )


def host_layout_css(plan: HostPlan, *, duration: float) -> str:
    """Keyframe the host box between split lower-band and fullscreen."""
    if duration <= 0 or not plan.windows:
        return ""

    stops: list[tuple[float, str]] = []
    for window in plan.windows:
        if window.mode == "full_footage":
            body = _layout_body(host_lower_layout())  # unused while opacity 0
        elif window.mode == "full_host":
            body = _layout_body(host_fullscreen_layout())
        else:
            body = _layout_body(host_lower_layout())
        stops.append((_pct(window.start, duration), body))
        stops.append((_pct(max(window.start, window.end - 0.001), duration), body))

    if not stops:
        return ""
    stops = _dedupe(stops)
    parts = [f"{p:.4f}%{{{body}}}" for p, body in stops]
    return (
        f"@keyframes host_layout{{{''.join(parts)}}}\n"
        f".host-frame{{animation:host_layout {duration:.3f}s linear 0s 1 normal both;}}"
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
