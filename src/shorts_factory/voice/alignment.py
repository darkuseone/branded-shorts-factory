"""Find where each written segment is actually spoken, using the pauses.

When the narration comes from the avatar clip, there is no word alignment to
read: HeyGen returns a video with the voice already in it, not a list of word
timings. Falling back to "spread the words evenly over the scenario's own
timings" puts the subtitles wherever the author guessed, and with one word on
screen at a time that drift is the most visible thing in the video.

But the audio itself says where the segments are. A narrator pauses between
sentences, and those pauses are measurable: ffmpeg's `silencedetect` reports
every quiet stretch, and the longest of them are the seams between segments.
Choosing the N-1 largest gaps for N segments turns "the author thinks this
sentence starts at 8.2s" into "the voice stops here", which is the thing the
picture and the captions actually have to match.

This is not word-level alignment and does not pretend to be. It puts each
sentence in the right place; the estimator then spreads that sentence's words
across the span it really occupies, which is a far smaller error than
spreading them across a span the author imagined.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..logging_utils import get_logger

log = get_logger("alignment")

#: Quiet stretches shorter than this are breaths inside a sentence, not seams.
MIN_PAUSE_S = 0.22

#: Anything quieter than this counts as silence. Voice recordings carry room
#: tone, so the threshold is well above digital silence.
SILENCE_DB = -32.0

_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


@dataclass(frozen=True)
class Pause:
    start: float
    end: float

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def middle(self) -> float:
        return self.start + self.length / 2


def find_pauses(audio: Path, *, ffmpeg: str = "ffmpeg", threshold_db: float = SILENCE_DB) -> list[Pause]:
    """Every quiet stretch in the file, in order. Never raises."""
    command = [
        ffmpeg, "-hide_banner", "-nostats", "-i", str(audio),
        "-af", f"silencedetect=noise={threshold_db}dB:d={MIN_PAUSE_S}",
        "-f", "null", "-",
    ]  # fmt: skip
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("silence detection failed: %s", exc, extra={"stage": "voice"})
        return []

    report = f"{done.stderr}\n{done.stdout}"
    starts = [float(value) for value in _START_RE.findall(report)]
    ends = [float(value) for value in _END_RE.findall(report)]
    pauses = [Pause(start, end) for start, end in zip(starts, ends, strict=False) if end > start]
    return [pause for pause in pauses if pause.length >= MIN_PAUSE_S]


def split_at_pauses(
    audio: Path,
    weights: list[float],
    total: float,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[tuple[float, float]]:
    """Cut `total` seconds into len(weights) spans, preferring real pauses.

    `weights` is how long each segment is *expected* to be — the character
    count of its text works well. The expected boundaries are the starting
    guess; each one then snaps to the nearest genuine pause, provided one is
    close enough that it plausibly belongs to that seam. A segment with no
    pause near its guess keeps the guess.

    Returns `(start, duration)` per segment, always covering `total` exactly
    and never overlapping.
    """
    count = len(weights)
    if count == 0 or total <= 0:
        return []
    if count == 1:
        return [(0.0, total)]

    span = sum(weights) or float(count)
    expected: list[float] = []
    cursor = 0.0
    for weight in weights[:-1]:
        cursor += total * weight / span
        expected.append(cursor)

    pauses = find_pauses(audio, ffmpeg=ffmpeg)
    if not pauses:
        log.info("no pauses found; keeping the estimated split", extra={"stage": "voice"})
        boundaries = expected
    else:
        # A seam may move by this much to land in a real pause. Wide enough to
        # absorb an author's guess being a second out, tight enough that a
        # sentence cannot be handed to the wrong segment.
        window = max(1.2, total / count * 0.6)
        boundaries = []
        taken: set[int] = set()
        for guess in expected:
            best_index, best_distance = -1, window
            for index, pause in enumerate(pauses):
                if index in taken:
                    continue
                distance = abs(pause.middle - guess)
                if distance < best_distance:
                    best_index, best_distance = index, distance
            if best_index >= 0:
                taken.add(best_index)
                boundaries.append(pauses[best_index].middle)
            else:
                boundaries.append(guess)
        boundaries.sort()
        moved = sum(1 for guess, edge in zip(expected, boundaries, strict=True) if abs(edge - guess) > 0.05)
        log.info(
            "%d/%d segment seams snapped to a pause in the narration",
            moved,
            len(boundaries),
            extra={"stage": "voice"},
        )

    spans: list[tuple[float, float]] = []
    previous = 0.0
    for edge in [*boundaries, total]:
        edge = min(max(edge, previous + 0.05), total)
        spans.append((round(previous, 3), round(edge - previous, 3)))
        previous = edge
    return spans
