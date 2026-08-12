"""Shot lengths taken from the narration instead of from the scenario.

The first cut looked like a slide show, and the reason was in the JSON: every
b-roll slot had been given roughly the same hand-written duration, so the
picture changed on a metronome no matter what was being said. Length is one of
the few things an edit says on its own — four seconds on one frame reads as
"this matters", two shots of a second each read as "and then, and then" — and
spending it uniformly says nothing at all.

The numbers here are not invented. `config/rhythm.yaml` holds the measured
profile the rhythm engine was built around (§2.3, §8): the 1.2-2.2s median
band, per-mode ceilings, and how long a burst may run. This module reuses that
config and the engine's own `plan_beat`, and applies it to the pipeline that
actually ships video, where the shot count is fixed by the author and only the
lengths are ours to choose.

What drives a length:

* **enumeration** — three or more items in one sentence become a burst of
  short frames, the way a list sounds;
* **weight of the words** — a beat that concludes something holds; a beat that
  lists or contrasts moves;
* **what is in the slot** — an infographic has to be read, so it is never cut
  as fast as a piece of b-roll;
* **where the words fall** — cuts land on word boundaries from the real voice
  alignment, so the picture never changes in the middle of a word.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_utils import get_logger
from ..spec import ScriptSegment, Spec, Visual
from ..voice.elevenlabs import VoiceClip

log = get_logger("rhythm")

#: Slots that tile the frame in sequence. Overlays and chips are timed against
#: whatever is underneath them, so retiming them would break that pairing.
MAIN_POSITIONS = frozenset({"fullscreen", "background", "split_upper", "top", "center", "bottom", "auto", ""})

#: How far a cut may move to land between words rather than inside one. It
#: scales with the shot, because a fifth of a second is a lot to take off a
#: 1s frame and nothing at all on a 4s one.
SNAP_WINDOW_S = 0.22
SNAP_WINDOW_SHARE = 0.18

#: Content that has to be read rather than glanced at.
READABLE_TYPES = frozenset({"infographic", "card", "chart", "quote"})
READABLE_WEIGHT = 1.55

#: A meme is a punchline; it lands and gets out.
MEME_WEIGHT = 0.8

#: Fallbacks for when config/rhythm.yaml cannot be read (tests, odd checkouts).
FALLBACK_MEDIAN = (1.2, 2.2)
FALLBACK_SHOT = (0.8, 4.2)
FALLBACK_BURST = (0.35, 0.7)


@dataclass
class RetimeReport:
    """What the retimer did, in the terms the defect was reported in."""

    retimed: int = 0
    median_s: float = 0.0
    shortest_s: float = 0.0
    longest_s: float = 0.0
    bursts: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def spread_s(self) -> float:
        """Zero spread is the slide show, stated as a number."""
        return round(self.longest_s - self.shortest_s, 3)

    def to_dict(self) -> dict[str, object]:
        return {
            "retimed": self.retimed,
            "median_s": round(self.median_s, 3),
            "shortest_s": round(self.shortest_s, 3),
            "longest_s": round(self.longest_s, 3),
            "spread_s": self.spread_s,
            "bursts": self.bursts,
            "notes": self.notes,
        }


@dataclass
class _Band:
    """The rhythm profile, from config when it is there and sane when it is not."""

    median_min: float
    median_max: float
    shot_min: float
    shot_max: float
    burst_min: float
    burst_max: float
    burst_shots_min: int
    burst_shots_max: int
    bursts_per_video: int
    sections: dict[str, dict]

    @property
    def target(self) -> float:
        return (self.median_min + self.median_max) / 2

    def section_target(self, at: float) -> float:
        """Preferred length where this moment sits in the video (§2.3).

        The profile is written as time ranges — a hook is cut faster than a
        closer — so the section is read off the clock rather than guessed from
        the segment's name.
        """
        for profile in self.sections.values():
            start = float(profile.get("start", -1))
            end = float(profile.get("end", -1))
            if start <= at < end and "shot_len" in profile:
                low, high = profile["shot_len"]
                return (float(low) + float(high)) / 2
        return self.target


def load_band(root: Path | None = None) -> _Band:
    """Read the measured profile the rhythm engine uses."""
    try:
        from redshift.core.config import Config

        config = Config.load(root)
        median = config.get("rhythm", "shot_duration_s.global_median_target")
        modes = ("MODE_A", "MODE_B", "MODE_C")
        lows = [float(config.get("rhythm", f"shot_duration_s.{m}.min")) for m in modes]
        highs = [float(config.get("rhythm", f"shot_duration_s.{m}.max")) for m in modes]
        burst = config.get("rhythm", "burst.shot_len")
        return _Band(
            median_min=float(median["min"]),
            median_max=float(median["max"]),
            shot_min=min(lows),
            shot_max=max(highs),
            # The engine's burst lengths are frame-flashes (0.10-0.35s), which
            # is a different device from "a couple of quick shots". What was
            # asked for is a fast pair around a second, so the burst floor is
            # the tightest *shot*, not the flash.
            burst_min=max(min(lows), float(burst[0])),
            burst_max=max(min(lows) * 1.25, float(burst[1]) * 2),
            burst_shots_min=int(config.get("rhythm", "burst.shots.min")),
            burst_shots_max=int(config.get("rhythm", "burst.shots.max")),
            bursts_per_video=int(config.get("rhythm", "burst.per_video.max")),
            sections=dict(config.get("rhythm", "sections_s")),
        )
    except Exception as exc:  # config is a convenience here, never a dependency
        log.info("rhythm config unavailable (%s); using built-in band", exc, extra={"stage": "rhythm"})
        return _Band(
            median_min=FALLBACK_MEDIAN[0],
            median_max=FALLBACK_MEDIAN[1],
            shot_min=FALLBACK_SHOT[0],
            shot_max=FALLBACK_SHOT[1],
            burst_min=FALLBACK_BURST[0],
            burst_max=FALLBACK_BURST[1],
            burst_shots_min=2,
            burst_shots_max=4,
            bursts_per_video=2,
            sections={},
        )


def retime_visuals(
    spec: Spec,
    voice_clips: list[VoiceClip] | None = None,
    *,
    band: _Band | None = None,
) -> RetimeReport:
    """Rewrite b-roll timings so the cut follows the voice. Mutates `spec`."""
    band = band or load_band()
    report = RetimeReport()
    clips = {clip.segment_id: clip for clip in (voice_clips or [])}
    bursts_left = band.bursts_per_video
    lengths: list[float] = []

    for segment in spec.all_segments:
        slots = _slots_for(spec, segment)
        if not slots:
            continue

        clip = clips.get(segment.id)
        start = clip.start if clip else segment.start
        span = (clip.duration if clip else segment.duration) or 0.0
        if span <= 0:
            continue

        words = _timed_words(segment, clip, start)
        burst = bursts_left > 0 and _is_enumeration(segment.text) and len(slots) >= 2
        weights = [
            _weight(visual) * carried
            for visual, carried in zip(slots, _carried_weights(slots, words, start, span), strict=True)
        ]
        if _is_contrast(segment.text) and len(slots) >= 2:
            # "Не хакер. Модель." — a correction lands as a fast pair, because
            # that is how it is said. Two long frames turn it into a statement.
            weights[0] *= 0.62
            weights[1] *= 0.62
        if burst:
            count = min(len(slots), max(2, band.burst_shots_min))
            for index in range(count):
                weights[index] = _BURST_WEIGHT
            bursts_left -= 1
            report.bursts += 1
        if holds_a_conclusion(segment.text):
            # The frame the conclusion lands on is held: this is the moment the
            # viewer is being asked to take something in, not to keep up.
            weights[-1] *= 1.45

        target = band.section_target(start + span / 2)
        durations = _distribute(span, weights, band, target)
        boundaries = _word_boundaries(clip, start)

        cursor = start
        for visual, length in zip(slots, durations, strict=True):
            end = _snap(
                cursor + length,
                boundaries,
                floor=cursor + band.shot_min,
                ceiling=start + span,
                length=length,
            )
            visual.start = round(cursor, 3)
            visual.duration = round(max(end - cursor, band.shot_min), 3)
            lengths.append(visual.duration)
            cursor = round(visual.start + visual.duration, 3)
            report.retimed += 1

        # Rounding and snapping both nudge the tail; the last slot absorbs it
        # so the segment still ends where the voice does.
        drift = (start + span) - cursor
        if slots and abs(drift) > 0.01:
            last = slots[-1]
            last.duration = round(max(last.duration + drift, band.shot_min), 3)
            lengths[-1] = last.duration

    trimmed = _seal_overlaps(spec, band)
    if trimmed:
        report.notes.append(f"{trimmed} shot(s) trimmed where a segment ran into the next")
        lengths = [visual.duration for visual in spec.visuals if _is_main(visual)]

    if lengths:
        report.median_s = statistics.median(lengths)
        report.shortest_s = min(lengths)
        report.longest_s = max(lengths)
        if report.median_s < band.median_min or report.median_s > band.median_max:
            # Not something retiming can fix on its own: the shot count is the
            # author's, and N slots over M seconds have a median of M/N however
            # the seconds are shared out. Say which way it is off, so the
            # scenario gets more slots (or fewer) rather than a silent drift.
            direction = "too slow" if report.median_s > band.median_max else "too fast"
            report.notes.append(
                f"median shot {report.median_s:.2f}s is {direction} for the "
                f"{band.median_min:g}-{band.median_max:g}s band — the scenario needs "
                f"{'more' if direction == 'too slow' else 'fewer'} slots, not different lengths"
            )
        if report.spread_s < 0.3:
            report.notes.append("every shot came out the same length; the cut will read as a slide show")
        log.info(
            "retimed %d shots: median %.2fs, %.2f-%.2fs, %d burst(s)",
            report.retimed,
            report.median_s,
            report.shortest_s,
            report.longest_s,
            report.bursts,
            extra={"stage": "rhythm"},
        )
    return report


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

#: A burst frame is a beat of the list, not a shot in its own right.
_BURST_WEIGHT = 0.55

#: Words that end a thought. A frame that carries one is held, because the
#: viewer is being asked to take something in rather than follow along.
_CONCLUSION = (
    "итог",
    "вывод",
    "значит",
    "поэтому",
    "в итоге",
    "результат",
    "оказалось",
    "именно так",
    "вот почему",
)

#: Marks that make a sentence a list.
_ENUMERATION_MARKS = (",", ";", "—", "–", " и ", " или ")

#: A correction or a reversal: said quickly, so cut quickly.
_CONTRAST = (" не ", " но ", " а ", " зато ", " однако ", " вместо ")

#: Spelled-out numbers, which the narration uses instead of digits so the
#: voice reads them correctly.
_NUMBER_WORDS = frozenset(
    {
        "ноль",
        "один",
        "одна",
        "два",
        "две",
        "три",
        "четыре",
        "пять",
        "шесть",
        "семь",
        "восемь",
        "девять",
        "десять",
        "сто",
        "двести",
        "триста",
        "четыреста",
        "пятьсот",
        "девятьсот",
        "тысяча",
        "тысяч",
        "миллион",
        "миллионов",
        "процент",
        "процента",
        "процентов",
    }
)


def _is_main(visual: Visual) -> bool:
    return (visual.position or "auto") in MAIN_POSITIONS


def _slots_for(spec: Spec, segment: ScriptSegment) -> list[Visual]:
    """The main-plane visuals that tile this segment, in order."""
    slots = [visual for visual in spec.visuals if _segment_of(spec, visual) is segment and _is_main(visual)]
    return sorted(slots, key=lambda visual: visual.start)


def _seal_overlaps(spec: Spec, band: _Band) -> int:
    """No two main-plane shots may occupy the same instant.

    Each segment is retimed against its own narration, and a synthesised
    segment can overrun the window the scenario gave it — the voice takes as
    long as it takes. Two segments stretched that way put two clips on one
    track at the same moment, which HyperFrames rejects outright: the run dies
    at lint with "overlapping_clips_same_track" and no video comes out at all.
    Trimming the earlier shot is right rather than dropping either: the picture
    still changes on the word it was cut to, one frame sooner.
    """
    slots = sorted((v for v in spec.visuals if _is_main(v)), key=lambda v: v.start)
    trimmed = 0
    for earlier, later in zip(slots, slots[1:], strict=False):
        overlap = (earlier.start + earlier.duration) - later.start
        if overlap <= 0.001:
            continue
        gap = round(later.start - earlier.start, 3)
        if gap <= 0:
            # Two shots claiming the same instant is a scenario error, not a
            # retiming one; say so rather than paper over it with a zero-length
            # clip the renderer would have to interpret.
            log.warning(
                "%s and %s both start at %.2fs", earlier.id, later.id, later.start, extra={"stage": "rhythm"}
            )
            continue
        # Below the band's floor if it has to be: a short shot is a stylistic
        # problem, an overlap is a failed render.
        earlier.duration = gap
        trimmed += 1
        log.info(
            "trimmed %s by %.2fs so it does not overlap %s",
            earlier.id,
            overlap,
            later.id,
            extra={"stage": "rhythm"},
        )
    return trimmed


def _segment_of(spec: Spec, visual: Visual) -> ScriptSegment | None:
    if visual.segment_ref:
        return spec.segment_by_id(visual.segment_ref)
    return spec.segment_at(visual.start)


def _is_enumeration(text: str) -> bool:
    """Three or more items in one breath — the engine's BURST trigger (§8.2)."""
    marks = sum(text.count(mark) for mark in _ENUMERATION_MARKS)
    return marks >= 2


def _is_contrast(text: str) -> bool:
    """A correction or a reversal, which is spoken fast and cut fast."""
    lowered = f" {text.casefold()} "
    return any(marker in lowered for marker in _CONTRAST)


def _timed_words(segment: ScriptSegment, clip: VoiceClip | None, start: float) -> list[tuple[str, float]]:
    """`(word, when it is said)` on the timeline, measured if we have it."""
    if clip is not None and clip.words:
        return [(word.word, word.start + clip.start) for word in clip.words]
    from ..voice.captions import estimate_words

    return [(word.word, word.start + start) for word in estimate_words(segment)]


def _carried_weights(
    slots: list[Visual], words: list[tuple[str, float]], start: float, span: float
) -> list[float]:
    """How much each shot is carrying, judged by the words spoken over it.

    A first pass hands every slot an equal share of the segment, which is only
    used to work out *which words land on which frame*. A frame that carries a
    number or a name is held — the viewer is being asked to read something. A
    frame that carries nothing but connective tissue gets out of the way. This
    is the whole point of the exercise: the picture changes when the narration
    gives it a reason to, not on a timer.
    """
    if not slots:
        return []
    if not words:
        return [1.0] * len(slots)

    share = span / len(slots)
    weights: list[float] = []
    for index in range(len(slots)):
        window_start = start + index * share
        window_end = window_start + share
        spoken = [word for word, when in words if window_start <= when < window_end]
        weight = 1.0
        if any(_is_substantial(word) for word in spoken):
            weight += 0.30
        if any(_is_number(word) for word in spoken):
            weight += 0.25
        if spoken and all(len(word.strip(_PUNCT)) <= 3 for word in spoken):
            weight -= 0.25
        weights.append(max(weight, 0.5))
    return weights


_PUNCT = " .,!?;:—–\"'«»()"


def _is_number(word: str) -> bool:
    stripped = word.strip(_PUNCT).casefold()
    return any(char.isdigit() for char in stripped) or stripped in _NUMBER_WORDS


def _is_substantial(word: str) -> bool:
    """A name or a term — something the viewer has to look at to take in."""
    stripped = word.strip(_PUNCT)
    if len(stripped) < 3:
        return False
    # Latin script inside Russian narration is a product or company name;
    # an internal capital ("OpenAI", "GPT") says the same thing outright.
    latin = sum(1 for char in stripped if "a" <= char.casefold() <= "z")
    return latin >= 3 or any(char.isupper() for char in stripped[1:])


def _weight(visual: Visual) -> float:
    if visual.type in READABLE_TYPES:
        return READABLE_WEIGHT
    if visual.type == "meme":
        return MEME_WEIGHT
    return 1.0


def _distribute(span: float, weights: list[float], band: _Band, target: float) -> list[float]:
    """Share a segment out by weight, then pull every shot into the legal band.

    Clamping alone would not preserve the total — the seconds a clamp takes off
    one shot have to land somewhere, or the picture stops before the voice
    does. They are pushed back onto whatever is still free to move.
    """
    if not weights:
        return []
    total_weight = sum(weights) or float(len(weights))
    lengths = [span * weight / total_weight for weight in weights]

    # Bias towards the section's preferred length so a segment with two slots
    # does not become two four-second shots just because it is long.
    pull = 0.35
    lengths = [length * (1 - pull) + target * pull for length in lengths]

    for _ in range(4):
        clamped = [min(max(length, band.shot_min), band.shot_max) for length in lengths]
        residue = span - sum(clamped)
        if abs(residue) < 0.01:
            return [round(length, 3) for length in clamped]
        movable = [
            index
            for index, length in enumerate(clamped)
            if (residue > 0 and length < band.shot_max) or (residue < 0 and length > band.shot_min)
        ]
        if not movable:
            return [round(length, 3) for length in clamped]
        share = residue / len(movable)
        lengths = list(clamped)
        for index in movable:
            lengths[index] += share
    return [round(min(max(length, band.shot_min), band.shot_max), 3) for length in lengths]


def _word_boundaries(clip: VoiceClip | None, start: float) -> list[float]:
    """Absolute times at which a word ends, from the real alignment."""
    if clip is None or not clip.words:
        return []
    return sorted({round(word.end + clip.start, 3) for word in clip.words})


def _snap(
    when: float, boundaries: list[float], *, floor: float, ceiling: float, length: float = 0.0
) -> float:
    """Move a cut onto the nearest word boundary, if one is close enough."""
    if not boundaries:
        return min(max(when, floor), ceiling)
    window = max(SNAP_WINDOW_S, SNAP_WINDOW_SHARE * length)
    nearest = min(boundaries, key=lambda edge: abs(edge - when))
    if abs(nearest - when) <= window and floor <= nearest <= ceiling:
        return nearest
    return min(max(when, floor), ceiling)


def holds_a_conclusion(text: str) -> bool:
    """Whether a beat is summing up — kept public; the planner reads it too."""
    lowered = text.casefold()
    return any(marker in lowered for marker in _CONCLUSION)
