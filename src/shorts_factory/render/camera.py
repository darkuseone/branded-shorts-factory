"""One camera for the whole film, instead of one per shot.

The HyperFrames motion doctrine states the rule this module implements: *how a
shot exits determines how the next one enters — same axis, same direction,
matched speed, and the cut lands mid-motion on both sides.* A film that obeys
it reads as one continuous camera move; a film that does not reads as a stack
of separately animated slides, which is what the first cut looked like even
after the shot lengths were fixed.

Three decisions follow from that, and they are the whole module:

**One velocity.** Every ordinary shot travels at the same speed, so the
distance it covers is its duration times that speed. Matching speed across a
cut then costs nothing — it is true by construction, for every pair of shots,
including the ones the rhythm engine made short.

**One direction.** The film picks a dominant vector and spends it on ordinary
cuts. Other vectors mean something: a push into the frame for the moment the
story turns, a rise for the conclusion. A shot that mirrors its neighbour —
drifting right after drifting left — kills the eye's momentum, so nothing is
mirrored by accident.

**No idle motion.** A shot either performs a move or holds still on purpose.
The standing ``scale(1.02)`` that used to sit under "no motion" was neither:
too small to read as a move, too large to read as a locked frame. An
infographic holds still because it has to be read; everything else moves.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Percent of frame width per second. Slow enough to be felt rather than seen —
#: at a 1.7s median that is about 4% of travel per shot.
PAN_VELOCITY = 2.6

#: A shot that travels less than this may as well be locked; one that travels
#: more than this is a whip, not a drift.
MIN_TRAVEL = 1.4
MAX_TRAVEL = 7.0

#: Headroom so the pan never exposes the edge of the frame. The move is
#: centred, so half the travel is needed on each side, plus a little for the
#: rounding a renderer does at 1080 wide.
EDGE_MARGIN = 0.6

#: A push covers ground on the Z axis instead of X; this is how much of it.
PUSH_DEPTH = 0.09

#: The rise that ends the film. Upward is reserved for a conclusion.
RISE_TRAVEL = 3.2


@dataclass(frozen=True)
class CameraMove:
    """The two ends of one shot's move, as CSS transforms."""

    start: str
    end: str
    #: True when the shot is deliberately locked — the graphic has to be read.
    still: bool = False

    def as_pair(self) -> tuple[str, str]:
        return self.start, self.end


def travel_for(duration: float) -> float:
    """How far a shot moves: far enough to be felt, at the film's speed."""
    return max(MIN_TRAVEL, min(MAX_TRAVEL, PAN_VELOCITY * max(duration, 0.0)))


def _scale_for(travel: float) -> float:
    """Just enough zoom that the pan never shows the edge of the frame."""
    return round(1.0 + (travel + EDGE_MARGIN) / 100.0, 4)


def hold(scale: float = 1.0) -> CameraMove:
    """A locked frame. Used where movement would fight the content."""
    transform = f"scale({scale:.4f})"
    return CameraMove(transform, transform, still=True)


def drift(duration: float, *, direction: int = -1) -> CameraMove:
    """The ordinary shot: a lateral move along the film's current.

    `direction` is -1 for the current (leftward travel, the house default) and
    +1 only where a reversal is deliberate. The move is centred on the frame,
    so the subject sits in the middle at the midpoint of the shot rather than
    sliding in from an edge.
    """
    travel = travel_for(duration)
    scale = _scale_for(travel)
    half = travel / 2.0
    start = f"scale({scale:.4f}) translateX({-direction * half:.3f}%)"
    end = f"scale({scale:.4f}) translateX({direction * half:.3f}%)"
    return CameraMove(start, end)


def push(duration: float, *, out: bool = False) -> CameraMove:
    """A move on Z. Forward is "deeper into this thought"; back is arrival."""
    depth = PUSH_DEPTH * min(max(duration, 0.6), 3.0) / 1.8
    near, far = 1.0 + depth, 1.0 + depth * 0.08
    first, second = (near, far) if out else (far, near)
    return CameraMove(f"scale({first:.4f})", f"scale({second:.4f})")


def rise(duration: float) -> CameraMove:
    """Upward — reserved for the conclusion, spent once."""
    travel = min(RISE_TRAVEL, travel_for(duration))
    scale = _scale_for(travel)
    return CameraMove(
        f"scale({scale:.4f}) translateY({travel / 2:.3f}%)",
        f"scale({scale:.4f}) translateY({-travel / 2:.3f}%)",
    )


def move_for(
    *,
    duration: float,
    hint: str = "",
    readable: bool = False,
    closing: bool = False,
) -> CameraMove:
    """Pick this shot's move from the film's grammar.

    `hint` is the scenario's own `motion` value. It is honoured where it names
    a reserved vector the author clearly meant — a push, an arrival — and
    otherwise ignored, because "kenburns" on every slot is how the edit ended
    up with twenty-five unrelated little drifts.
    """
    if readable:
        return hold()
    if closing:
        return rise(duration)
    if hint in {"zoom_in", "push"}:
        return push(duration)
    if hint in {"zoom_out", "pull"}:
        return push(duration, out=True)
    if hint == "pan_right":
        # The author asked for the reverse of the current; honour it, since a
        # deliberate reversal is a device and a silent one is a mistake.
        return drift(duration, direction=1)
    return drift(duration)
