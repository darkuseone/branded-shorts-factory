"""Caption cues, built from real word timings when the voice API gives them.

Shorts captions live or die on sync. When ElevenLabs returns an alignment we
use it verbatim; otherwise we distribute words across the segment weighted by
length, which is noticeably better than an even split.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..spec import CaptionSettings, ScriptSegment
from .elevenlabs import VoiceClip, WordTiming


@dataclass
class CaptionWord:
    text: str
    start: float
    end: float


@dataclass
class CaptionCue:
    """One on-screen line."""

    text: str
    start: float
    end: float
    words: list[CaptionWord] = field(default_factory=list)
    segment_id: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "segment_id": self.segment_id,
            "words": [
                {"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)} for w in self.words
            ],
        }


def _one_word_at_a_time(
    timings: list[WordTiming], settings: CaptionSettings, segment_id: str
) -> list[CaptionCue]:
    """One word on screen at a time, changing with the voice.

    No line is ever assembled, so there is nothing to wrap and nothing to run
    off the frame — the failure mode the line styles kept producing. A word
    holds until the next one starts, which closes the gaps that made subtitles
    look like they were flickering in and out.

    Cues never overlap: a word too short to read borrows time from the one
    after it, and the borrowing is given up as soon as the drift from the
    voice would become audible — being a frame late is invisible, being a
    third of a second late is a subtitle for the wrong word.
    """
    cues: list[CaptionCue] = []
    cursor = 0.0
    for index, timing in enumerate(timings):
        word = timing.word.strip()
        if not word:
            continue
        start = max(timing.start, cursor)
        if start - timing.start > MAX_DRIFT_S:
            start = timing.start
        # Hold until the next word takes over; the last one keeps its own end.
        following = timings[index + 1].start if index + 1 < len(timings) else timing.end
        end = max(following, start + MIN_WORD_S)
        cursor = end
        cues.append(
            CaptionCue(
                text=word.upper() if settings.uppercase else word,
                start=start,
                end=end,
                words=[CaptionWord(word, start, end)],
                segment_id=segment_id,
            )
        )
    return cues


def _estimate_words(segment: ScriptSegment) -> list[WordTiming]:
    """Spread a segment's words over its duration, weighted by word length."""
    words = segment.text.split()
    if not words:
        return []
    weights = [len(word) + 1 for word in words]
    total = sum(weights)
    timings: list[WordTiming] = []
    cursor = 0.0
    for word, weight in zip(words, weights, strict=True):
        span = segment.duration * weight / total
        timings.append(WordTiming(word, cursor, cursor + span))
        cursor += span
    return timings


def build_cues(
    segments: list[ScriptSegment],
    clips: list[VoiceClip],
    settings: CaptionSettings,
) -> list[CaptionCue]:
    """Group narration into on-screen lines with per-word highlight timings."""
    if not settings.enabled or settings.style == "none":
        return []

    by_segment = {clip.segment_id: clip for clip in clips}
    cues: list[CaptionCue] = []

    for segment in segments:
        clip = by_segment.get(segment.id)
        if clip and clip.words:
            # Alignment times are relative to the clip; shift onto the timeline.
            timings = [
                WordTiming(word.word, word.start + clip.start, word.end + clip.start) for word in clip.words
            ]
        else:
            timings = [
                WordTiming(word.word, word.start + segment.start, word.end + segment.start)
                for word in _estimate_words(segment)
            ]
        if not timings:
            continue
        cues.extend(_group(timings, settings, segment.id))

    cues.sort(key=lambda cue: cue.start)
    return cues


#: A word shown for less than this reads as a flicker; neighbours are held
#: fractionally longer instead so the eye can land on each one.
MIN_WORD_S = 0.22

#: How far a held word may push the next one behind the voice before the hold
#: is abandoned. Past this the subtitle stops being late and starts being wrong.
MAX_DRIFT_S = 0.18


def _group(timings: list[WordTiming], settings: CaptionSettings, segment_id: str) -> list[CaptionCue]:
    """Break a word stream into what goes on screen at once."""
    if settings.style == "word":
        return _one_word_at_a_time(timings, settings, segment_id)

    limit = max(8, settings.max_chars_per_line)
    lines: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    length = 0

    for timing in timings:
        word_length = len(timing.word)
        # A long pause is a natural line break even when the line is short.
        gap = timing.start - current[-1].end if current else 0.0
        if current and (length + word_length + 1 > limit or gap > 0.45):
            lines.append(current)
            current, length = [], 0
        current.append(timing)
        length += word_length + 1
    if current:
        lines.append(current)

    cues: list[CaptionCue] = []
    for line in lines:
        text = " ".join(word.word for word in line)
        cues.append(
            CaptionCue(
                text=text.upper() if settings.uppercase else text,
                start=line[0].start,
                end=line[-1].end,
                words=[CaptionWord(w.word, w.start, w.end) for w in line],
                segment_id=segment_id,
            )
        )
    return cues
