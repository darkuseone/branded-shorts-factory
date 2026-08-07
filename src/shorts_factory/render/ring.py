"""Pulse Ring — the REDSHIFT presenter frame.

The ring is not decoration. It is the channel's signature: a Signal Red circle
around the talking head that breathes while you speak, pulses on an emphasised
number, expands into a wipe on a topic change, and collapses to a point when
the frame needs the whole screen for a payoff.

Four states, matching the brand book:

* IDLE — slow ±5% breath, ~2.4s cycle
* EMPHASIS — double pulse +18%, 0.25s each
* TRANSITION — expand to 140% then settle (topic change)
* EXIT / RETURN — CRT collapse to a point at the nearest edge, and reverse

The avatar itself is clipped to a circle inside the ring. Motion graphics that
"touch" the ring are drawn as a separate overlay whose mask is the ring's
exterior, so particles read as coming *from* the circle rather than floating
over it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import VIDEO_HEIGHT, VIDEO_WIDTH
from ..logging_utils import get_logger
from ..spec import Spec

log = get_logger("ring")

RingState = Literal["idle", "emphasis", "transition", "exit", "return_", "hidden"]

DEFAULT_DIAMETER_RATIO = 0.30
DEFAULT_STROKE = "#FF2A3C"
DEFAULT_GLOW = "#FF4D63"
DEFAULT_IDLE_CYCLE = 2.4
DEFAULT_EMPHASIS_PULSE = 0.18
DEFAULT_TRANSITION_EXPAND = 1.4
DEFAULT_FACE_ZOOM = 1.0
DEFAULT_FACE_POSITION = "center 72%"
DEFAULT_SCALE_Y = 1.12  # slight vertical stretch (reference oval)


@dataclass(frozen=True)
class RingConfig:
    diameter_ratio: float = DEFAULT_DIAMETER_RATIO
    default_anchor: str = "bottom_right"
    cta_anchor: str = "bottom_center"
    stroke: str = DEFAULT_STROKE
    glow: str = DEFAULT_GLOW
    #: Zoom the avatar video inside the circle (1.0 = fit, 1.2 = 20% closer).
    face_zoom: float = DEFAULT_FACE_ZOOM
    #: CSS object-position for the face crop inside the circle.
    face_position: str = DEFAULT_FACE_POSITION
    #: Vertical stretch of the ring wrapper (>1 = taller oval).
    scale_y: float = DEFAULT_SCALE_Y
    #: Draw CSS lightning web rising from the ring top.
    lightning: bool = True
    idle_pulse: float = 0.05
    idle_cycle_s: float = DEFAULT_IDLE_CYCLE
    emphasis_pulse: float = DEFAULT_EMPHASIS_PULSE
    emphasis_duration_s: float = 0.25
    emphasis_repeats: int = 2
    transition_expand: float = DEFAULT_TRANSITION_EXPAND
    transition_duration_s: float = 0.35
    exit_duration_s: float = 0.25

    @classmethod
    def from_brandbook(cls, data: dict[str, Any] | None) -> RingConfig:
        """Read `ring` from brandbook.json. Missing keys keep the defaults."""
        raw = data or {}
        states = raw.get("states") if isinstance(raw.get("states"), dict) else {}
        idle = states.get("idle") if isinstance(states.get("idle"), dict) else {}
        emphasis = states.get("emphasis") if isinstance(states.get("emphasis"), dict) else {}
        transition = states.get("transition") if isinstance(states.get("transition"), dict) else {}
        exit_ = states.get("exit") if isinstance(states.get("exit"), dict) else {}
        return cls(
            diameter_ratio=float(raw.get("diameter_ratio") or DEFAULT_DIAMETER_RATIO),
            default_anchor=str(raw.get("default_anchor") or "bottom_right"),
            cta_anchor=str(raw.get("cta_anchor") or "bottom_center"),
            stroke=str(raw.get("stroke") or DEFAULT_STROKE),
            glow=str(raw.get("glow") or DEFAULT_GLOW),
            face_zoom=float(raw.get("face_zoom") or DEFAULT_FACE_ZOOM),
            face_position=str(raw.get("face_position") or DEFAULT_FACE_POSITION),
            scale_y=float(raw.get("scale_y") or DEFAULT_SCALE_Y),
            lightning=bool(raw.get("lightning", True)),
            idle_pulse=float(idle.get("pulse") or 0.05),
            idle_cycle_s=float(idle.get("cycle_s") or DEFAULT_IDLE_CYCLE),
            emphasis_pulse=float(emphasis.get("pulse") or DEFAULT_EMPHASIS_PULSE),
            emphasis_duration_s=float(emphasis.get("duration_s") or 0.25),
            emphasis_repeats=int(emphasis.get("repeats") or 2),
            transition_expand=float(transition.get("expand") or DEFAULT_TRANSITION_EXPAND),
            transition_duration_s=float(transition.get("duration_s") or 0.35),
            exit_duration_s=float(exit_.get("duration_s") or 0.25),
        )

    @property
    def diameter_px(self) -> int:
        return int(round(VIDEO_HEIGHT * self.diameter_ratio))


@dataclass(frozen=True)
class RingEvent:
    """One state change on the ring timeline."""

    at: float
    state: RingState
    duration: float
    reason: str = ""


@dataclass
class RingPlan:
    """Everything the composition writer needs to draw the ring."""

    config: RingConfig
    events: list[RingEvent] = field(default_factory=list)
    windows: list[tuple[float, float]] = field(default_factory=list)
    #: Anchor may flip to bottom-center for the CTA beat.
    anchor_overrides: list[tuple[float, float, str]] = field(default_factory=list)

    @property
    def visible(self) -> bool:
        return bool(self.windows)


def plan_ring(spec: Spec, config: RingConfig | None = None) -> RingPlan:
    """Derive ring state changes from the scenario.

    The ring is on whenever the avatar is on. Gaps between `avatar.segments`
    are EXIT → hidden → RETURN. High-emphasis segments get EMPHASIS pulses.
    Topic-changing visuals get a TRANSITION expand.
    """
    cfg = config or RingConfig()
    if not spec.avatar.enabled:
        return RingPlan(config=cfg)

    events: list[RingEvent] = []
    windows: list[tuple[float, float]] = []

    wanted = set(spec.avatar.segments) if spec.avatar.segments else None
    segments = [s for s in spec.all_segments if wanted is None or s.id in wanted]
    if not segments:
        # Avatar on for the whole video.
        windows.append((0.0, spec.duration_target))
        events.append(RingEvent(0.0, "idle", spec.duration_target, "full run"))
    else:
        # Merge contiguous / near-contiguous segments into windows.
        window_start = segments[0].start
        window_end = segments[0].end
        for previous, current in zip(segments, segments[1:], strict=False):
            gap = current.start - previous.end
            if gap > 1.2:
                windows.append((window_start, window_end))
                events.append(
                    RingEvent(window_end, "exit", cfg.exit_duration_s, f"leave after {previous.id}")
                )
                events.append(
                    RingEvent(current.start, "return_", cfg.exit_duration_s, f"return for {current.id}")
                )
                window_start = current.start
            window_end = max(window_end, current.end)
        windows.append((window_start, window_end))

    for segment in spec.all_segments:
        if segment.emphasis != "high":
            continue
        if not _inside_any(segment.start, windows):
            continue
        at = segment.start + min(0.35, max(0.15, segment.duration * 0.12))
        events.append(
            RingEvent(
                at,
                "emphasis",
                cfg.emphasis_duration_s * cfg.emphasis_repeats,
                f"emphasis {segment.id}",
            )
        )

    for previous, current in zip(spec.visuals, spec.visuals[1:], strict=False):
        if current.start < 0.6:
            continue
        if not _is_topic_change(previous.type, previous.position, previous.priority, current):
            continue
        if not _inside_any(current.start, windows):
            continue
        events.append(
            RingEvent(
                max(0.0, current.start - 0.05),
                "transition",
                cfg.transition_duration_s,
                f"cut {previous.id}→{current.id}",
            )
        )

    overrides: list[tuple[float, float, str]] = []
    if spec.cta and cfg.cta_anchor != cfg.default_anchor:
        overrides.append((spec.cta.start, spec.cta.end, cfg.cta_anchor))

    events.sort(key=lambda event: event.at)
    return RingPlan(config=cfg, events=events, windows=windows, anchor_overrides=overrides)


def layout_box(
    config: RingConfig,
    *,
    anchor: str | None = None,
    canvas_w: int = VIDEO_WIDTH,
    canvas_h: int = VIDEO_HEIGHT,
) -> dict[str, int]:
    """Pixel box for the ring+avatar wrapper."""
    size = config.diameter_px
    margin = 96
    bottom = 340  # YouTube UI reserve
    chosen = anchor or config.default_anchor

    if chosen == "bottom_center":
        left = (canvas_w - size) // 2
        top = canvas_h - bottom - size + int(size * 0.08)
    elif chosen == "bottom_left":
        left = margin
        top = canvas_h - bottom - size + int(size * 0.08)
    elif chosen == "center":
        left = (canvas_w - size) // 2
        top = (canvas_h - size) // 2
    else:  # bottom_right — leave extra room on the right so neon bloom isn't clipped
        right_margin = max(margin, 140)
        left = canvas_w - right_margin - size
        top = canvas_h - bottom - size + int(size * 0.08)

    return {"left": left, "top": max(margin, top), "size": size}


def ring_svg(config: RingConfig, size: int | None = None) -> str:
    """Inline SVG for the crisp neon rim.

    Two concentric strokes: Deep Red shadow ring + Signal Red primary.
    Glow is intentionally *not* an SVG filter — HyperFrames/Puppeteer screenshot
    capture often drops ``feGaussianBlur``, leaving a flat matte stroke. Neon
    bloom is applied via CSS ``box-shadow`` on ``.avatar-clip`` instead.
    """
    px = size or config.diameter_px
    stroke = config.stroke
    deep = "#B4001E"
    cx = px / 2
    r = px * 0.46
    outer_w = max(8, px * 0.032)
    inner_w = max(6, px * 0.024)
    return (
        f'<svg class="pulse-ring-svg" viewBox="0 0 {px} {px}" width="{px}" height="{px}" '
        f'aria-hidden="true">'
        f'<circle class="pulse-ring-shadow" cx="{cx}" cy="{cx}" r="{r:.1f}" '
        f'fill="none" stroke="{deep}" stroke-width="{outer_w:.1f}" opacity="0.55"/>'
        f'<circle class="pulse-ring-stroke" cx="{cx}" cy="{cx}" r="{r:.1f}" '
        f'fill="none" stroke="{stroke}" stroke-width="{inner_w:.1f}"/>'
        f"</svg>"
    )


def ring_css(plan: RingPlan, *, duration: float) -> str:
    """Absolute-time keyframes for the ring wrapper across the whole composition.

    One animation spans the full duration. State changes are percentage stops,
    so a seeking renderer always shows the same frame.
    """
    if duration <= 0 or not plan.windows:
        return ""

    cfg = plan.config
    stops: list[tuple[float, str]] = [(0.0, _transform(0.0, 0.0))]

    # Hidden outside visible windows.
    for start, end in plan.windows:
        # Arrive.
        stops.append((_pct(max(0.0, start - cfg.exit_duration_s), duration), _transform(0.0, 0.0)))
        stops.append((_pct(start, duration), _transform(1.0, 1.0)))
        # Leave.
        stops.append((_pct(end, duration), _transform(1.0, 1.0)))
        stops.append((_pct(min(duration, end + cfg.exit_duration_s), duration), _transform(0.0, 0.0)))

    for event in plan.events:
        if event.state == "emphasis":
            pulse = 1.0 + cfg.emphasis_pulse
            mid = event.at + event.duration / 2
            stops.append((_pct(event.at, duration), _transform(1.0, 1.0)))
            stops.append((_pct(event.at + cfg.emphasis_duration_s * 0.5, duration), _transform(1.0, pulse)))
            stops.append((_pct(event.at + cfg.emphasis_duration_s, duration), _transform(1.0, 1.0)))
            stops.append((_pct(mid, duration), _transform(1.0, pulse)))
            stops.append((_pct(event.at + event.duration, duration), _transform(1.0, 1.0)))
        elif event.state == "transition":
            expand = cfg.transition_expand
            mid = event.at + event.duration / 2
            stops.append((_pct(event.at, duration), _transform(1.0, 1.0)))
            stops.append((_pct(mid, duration), _transform(1.0, expand)))
            stops.append((_pct(event.at + event.duration, duration), _transform(1.0, 1.0)))

    stops.append((100.0, _transform(0.0, 0.0)))
    stops = _dedupe_stops(stops)

    parts = [f"{percent:.4f}%{{{body}}}" for percent, body in stops]
    return (
        f"@keyframes pulse_ring_life{{{''.join(parts)}}}\n"
        f".pulse-ring-wrap{{"
        f"animation:pulse_ring_life {duration:.3f}s linear 0s 1 normal both;"
        f"transform-origin:center center;"
        f"}}\n"
        f".pulse-ring-breath{{"
        f"animation:pulse_ring_idle {cfg.idle_cycle_s:.2f}s ease-in-out infinite alternate;"
        f"transform-origin:center center;"
        f"}}\n"
        f"@keyframes pulse_ring_idle{{"
        f"0%{{transform:scale(1)}}"
        f"100%{{transform:scale({1.0 + cfg.idle_pulse:.3f})}}"
        f"}}"
    )


def lightning_overlay_svg(config: RingConfig, size: int) -> str:
    """Sparse neural bolts rising from the ring — CSS-animated, HF-safe.

    Uses class ``ring-lightning`` (never ``ring-network`` — that ghosted left).
    Masked from the ring rim upward so lines read as emitted energy.
    """
    if not config.lightning:
        return ""
    cx = size / 2
    top = size * 0.08
    nodes = [
        (cx, top),
        (cx - size * 0.14, top - size * 0.18),
        (cx + size * 0.12, top - size * 0.22),
        (cx - size * 0.26, top - size * 0.08),
        (cx + size * 0.24, top - size * 0.10),
        (cx - size * 0.06, top - size * 0.34),
        (cx + size * 0.04, top - size * 0.38),
        (cx - size * 0.18, top - size * 0.42),
        (cx + size * 0.16, top - size * 0.46),
    ]
    lines = []
    for index, (x, y) in enumerate(nodes[1:], start=1):
        parent = nodes[0] if index < 4 else nodes[min(index - 1, 3)]
        lines.append(
            f'<line class="bolt-line" x1="{parent[0]:.1f}" y1="{parent[1]:.1f}" '
            f'x2="{x:.1f}" y2="{y:.1f}" stroke="{config.stroke}" '
            f'stroke-width="{max(1.2, size * 0.0045):.1f}" opacity="0.75"/>'
        )
    dots = "".join(
        f'<circle class="bolt-node" cx="{x:.1f}" cy="{y:.1f}" '
        f'r="{max(1.8, size * 0.01):.1f}" fill="{config.glow}"/>'
        for x, y in nodes
    )
    orbs = "".join(
        f'<circle class="bolt-orb" cx="{x:.1f}" cy="{y:.1f}" '
        f'r="{max(3.0, size * 0.018):.1f}" fill="{config.stroke}" opacity="0.25"/>'
        for x, y in nodes[5:]
    )
    view = f"{-size * 0.15:.0f} {-size * 0.55:.0f} {size * 1.3:.0f} {size * 0.85:.0f}"
    return (
        f'<svg class="ring-lightning" viewBox="{view}" width="{size}" height="{int(size * 0.7)}" '
        f'aria-hidden="true" style="position:absolute;left:0;top:{-int(size * 0.42)}px;'
        f'pointer-events:none;overflow:visible;'
        f'mask-image:radial-gradient(ellipse 70% 90% at 50% 100%,#000 18%,#fff 55%);'
        f'-webkit-mask-image:radial-gradient(ellipse 70% 90% at 50% 100%,#000 18%,#fff 55%);">'
        f"{''.join(lines)}{dots}{orbs}</svg>"
    )


def network_overlay_svg(config: RingConfig, size: int) -> str:
    """Deprecated alias — use ``lightning_overlay_svg``. Kept for tests."""
    return lightning_overlay_svg(config, size)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _inside_any(time: float, windows: list[tuple[float, float]]) -> bool:
    return any(start - 0.05 <= time <= end + 0.05 for start, end in windows)


def _is_topic_change(prev_type: str, prev_position: str, prev_priority: str, current: Any) -> bool:
    if prev_position != current.position:
        return True
    if current.type in {"motion_graphics", "infographic", "meme"} and prev_type != current.type:
        return True
    return current.priority in {"high", "critical"} and prev_priority not in {"high", "critical"}


def _pct(time: float, duration: float) -> float:
    return max(0.0, min(100.0, (time / duration) * 100.0 if duration else 0.0))


def _transform(opacity: float, scale: float) -> str:
    return f"opacity:{opacity:.3f};transform:scale({scale:.3f});"


def _dedupe_stops(stops: list[tuple[float, str]]) -> list[tuple[float, str]]:
    seen: set[str] = set()
    out: list[tuple[float, str]] = []
    for percent, body in sorted(stops, key=lambda stop: stop[0]):
        key = f"{percent:.4f}"
        if key in seen:
            # Later write wins at the same timestamp (emphasis over idle).
            out = [(p, b) for p, b in out if f"{p:.4f}" != key]
        seen.add(key)
        out.append((percent, body))
    return out
