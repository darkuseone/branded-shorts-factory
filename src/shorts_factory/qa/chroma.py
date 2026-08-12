"""Catch the stock clips that were never meant to be shown as they are.

Free libraries are full of chroma-key assets: a battery icon, an arrow, a
subscribe button, each drawn on a flat #00b140 green so an editor can key it
out. Nothing in the metadata says so — the title is "battery low battery
warning", the tags are on topic, the resolution is fine — so the level-1 gate
passes it happily and a full-screen slab of green lands in the video. It did:
a cartoon battery on green filled the top half of a Short about a model
breaking out of a sandbox.

The frame says what the metadata will not. A keyed background is a large,
contiguous, unnaturally saturated field of a single hue, and that is cheap to
measure: shrink a frame to a thumbnail, count the pixels that sit in the
chroma-green (or chroma-blue) box, and reject when they dominate. Real footage
of grass, foliage or a green wall does not come close — natural greens carry
far less saturation and far more variation than a key colour, which is the
whole reason keying works.

No new dependencies: ffmpeg already ships with the pipeline, and a 32x32
rawvideo thumbnail is 3KB of bytes to count in Python.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..logging_utils import get_logger

log = get_logger("chroma")

#: Thumbnail edge. Small on purpose: a key background is a huge flat field, so
#: it survives any downscale, and a tiny frame keeps this to microseconds.
_THUMB = 32

#: Share of the frame that must be key-coloured before the clip is refused.
#: A keyed asset is typically 60-90% background; a real shot of foliage that
#: saturated does not happen.
MAX_KEY_SHARE = 0.45


def _thumbnail(path: Path, *, ffmpeg: str, at: float) -> bytes:
    """One frame as raw RGB, or empty when it cannot be read."""
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(at, 0.0):.3f}", "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={_THUMB}:{_THUMB}",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]  # fmt: skip
    try:
        done = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("thumbnail failed for %s: %s", path.name, exc)
        return b""
    return done.stdout if len(done.stdout) >= _THUMB * _THUMB * 3 else b""


def _is_key_pixel(red: int, green: int, blue: int) -> bool:
    """Whether one pixel sits in the chroma-green or chroma-blue box.

    The test is deliberately strict. A key colour is near-pure: one channel
    dominates the other two by a wide margin and is itself bright. Foliage,
    a painted wall or a green-lit room all fail at least one of those.
    """
    if green > 90 and green - red > 60 and green - blue > 60:
        return True
    return blue > 90 and blue - red > 70 and blue - green > 55


def key_share(path: Path, *, ffmpeg: str = "ffmpeg", duration: float = 0.0) -> float:
    """Largest share of any sampled frame that is chroma-key background.

    Sampling more than one frame matters: keyed assets often open on a fade
    from black, and a single early sample would read as ordinary footage.
    Returns 0.0 when nothing could be read — an unreadable frame is the
    download gate's problem, not this one's.
    """
    span = duration if duration > 0 else 0.0
    offsets = [span * share for share in (0.35, 0.6)] if span > 0.5 else [0.0]

    worst = 0.0
    for offset in offsets:
        raw = _thumbnail(path, ffmpeg=ffmpeg, at=offset)
        if not raw:
            continue
        total = len(raw) // 3
        keyed = sum(
            1
            for index in range(total)
            if _is_key_pixel(raw[index * 3], raw[index * 3 + 1], raw[index * 3 + 2])
        )
        worst = max(worst, keyed / total if total else 0.0)
    return worst


def looks_keyed(path: Path, *, ffmpeg: str = "ffmpeg", duration: float = 0.0) -> tuple[bool, float]:
    """`(should be refused, measured share)` for one downloaded asset."""
    share = key_share(path, ffmpeg=ffmpeg, duration=duration)
    return share >= MAX_KEY_SHARE, share
