"""Stage 03 — Shotlist Director. The rhythm engine (§8).

This is where a script becomes an edit, frame-accurate, before a single asset
has been found. Everything here comes from measuring the three reference
videos, not from taste:

* **one entity, one shot** — the main lesson of reference V2. When a name is
  spoken it is on screen; material is chosen per entity in a sentence, not per
  paragraph topic.
* **MODE_B never holds past 2.9s** — in V2 no shot lives longer, at all, and
  median equals mean (1.54s), so the rhythm never sags.
* **the presenter carries 40-70% of the runtime** — brandbook rule, checked as
  a hard gate before the artifact is written.

Deterministic on purpose: an LLM may propose the shot breakdown, but the
timings, the mode state machine and the checks are computed here so the result
is reproducible and testable without a model (.cursorrules: don't replace
deterministic checks with LLM calls).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..core.config import Config
from ..core.errors import RhythmViolation
from ..core.log import get_logger
from ..core.schemas import (
    Burst,
    Mode,
    Script,
    Shot,
    Shotlist,
    ShotQueries,
    TextOverlay,
    VisualIntent,
)

log = get_logger("s03")

STAGE = "s03_shotlist"

#: §8.2 step 3 — visual_intent decides the frame mode.
INTENT_MODE: dict[str, Mode] = {
    "COSMIC_HERO": "MODE_B",
    "SIMULATION": "MODE_B",
    "DATA_VIZ": "MODE_B",
    "ABSTRACT_NEURAL": "MODE_B",
    "ARCHIVE": "MODE_B",
    "MEME": "MODE_B",
    "HUMAN_SCALE": "MODE_B",
    "UI_WALKTHROUGH": "MODE_A",
    "ARTICLE": "MODE_A",
    "DIAGRAM": "MODE_A",
    "ICON_LOGO": "MODE_A",
    "LAB_TECH": "MODE_A",
    "PRESENTER_ONLY": "MODE_C",
}

#: Sections where the presenter addresses the viewer directly.
DIRECT_SECTIONS = frozenset({"CLOSER"})

#: Motion per mode — a static frame in a dynamic mode is a defect (§1.1).
MODE_MOTION = {
    "MODE_A": "push_in_2pct",
    "MODE_B": "push_in_3pct",
    "MODE_C": "none",
}

_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)
_ENUM_SPLIT = re.compile(r"[,;]| и | или ")


@dataclass(frozen=True)
class RhythmConfig:
    """Every number the engine uses, read from config/rhythm.yaml once."""

    fps: int
    target_s: float
    mode_min: dict[str, float]
    mode_max: dict[str, float]
    median_min: float
    median_max: float
    sections: dict[str, dict]
    burst_min_shots: int
    burst_max_shots: int
    burst_len: tuple[float, float]
    burst_per_video_max: int
    mode_min_interval_s: float
    mode_max_hold_s: float
    presenter_min: float
    presenter_max: float
    closer_mode: Mode
    closer_len_s: float
    first_shot_modes: tuple[str, ...]

    @classmethod
    def from_config(cls, config: Config) -> RhythmConfig:
        modes = ("MODE_A", "MODE_B", "MODE_C")
        return cls(
            fps=int(config.rhythm("video.fps")),
            target_s=float(config.rhythm("video.target_duration_s")),
            mode_min={m: float(config.rhythm(f"shot_duration_s.{m}.min")) for m in modes},
            mode_max={m: float(config.rhythm(f"shot_duration_s.{m}.max")) for m in modes},
            median_min=float(config.rhythm("shot_duration_s.global_median_target.min")),
            median_max=float(config.rhythm("shot_duration_s.global_median_target.max")),
            sections=dict(config.rhythm("sections_s")),
            burst_min_shots=int(config.rhythm("burst.shots.min")),
            burst_max_shots=int(config.rhythm("burst.shots.max")),
            burst_len=tuple(config.rhythm("burst.shot_len")),  # type: ignore[arg-type]
            burst_per_video_max=int(config.rhythm("burst.per_video.max")),
            mode_min_interval_s=float(config.rhythm("mode_switching.min_interval_s")),
            mode_max_hold_s=float(config.rhythm("mode_switching.max_hold_s")),
            presenter_min=float(config.rhythm("mode_switching.presenter_share.min")),
            presenter_max=float(config.rhythm("mode_switching.presenter_share.max")),
            closer_mode=config.rhythm("mode_switching.closer_mode"),
            closer_len_s=float(config.rhythm("mode_switching.closer_len_s")),
            first_shot_modes=tuple(config.rhythm("mode_switching.first_shot_modes")),
        )

    def clamp(self, mode: Mode, seconds: float) -> float:
        return max(self.mode_min[mode], min(seconds, self.mode_max[mode]))


# --------------------------------------------------------------------------- #
# Entity splitting — "one entity, one shot"
# --------------------------------------------------------------------------- #


def split_units(beat_text: str, entities: Sequence[str]) -> list[str]:
    """Break a beat into the things that each deserve their own frame.

    Named entities come first (they are what the viewer must see), then the
    beat is padded with its own clauses so a long sentence never sits on one
    static frame.
    """
    units = [entity for entity in entities if entity.strip()]
    clauses = [part.strip() for part in _ENUM_SPLIT.split(beat_text) if len(part.split()) >= 2]
    for clause in clauses:
        if len(units) >= 6:
            break
        if not any(entity.lower() in clause.lower() for entity in units):
            units.append(clause)
    return units or [beat_text]


def is_enumeration(beat_text: str, entities: Sequence[str]) -> bool:
    """Three or more homogeneous items in one beat — the BURST trigger (§8.2)."""
    if len(entities) >= 3:
        return True
    parts = [part for part in _ENUM_SPLIT.split(beat_text) if part.strip()]
    return len(parts) >= 3


# --------------------------------------------------------------------------- #
# Mode assignment
# --------------------------------------------------------------------------- #


class ModeSelector:
    """Applies R1-R9 (§2.2) while walking the beats in order."""

    def __init__(self, rhythm: RhythmConfig):
        self.rhythm = rhythm
        self.current: Mode | None = None
        self.held_since = 0.0
        self.last_switch = -999.0

    def choose(
        self,
        intent: VisualIntent,
        section: str,
        at: float,
        *,
        length: float = 0.0,
        meme: bool = False,
    ) -> Mode:
        wanted = self._preferred(intent, section, meme)

        # R1: never open on a split — it is a weak hook.
        if self.current is None:
            wanted = wanted if wanted in self.rhythm.first_shot_modes else "MODE_B"
            self._switch(wanted, at)
            return wanted

        if wanted == self.current:
            # R3: rotate before the run overshoots 12s, judging by where this
            # shot ends. Deciding on its start leaves the run a shot too long.
            if at + length - self.held_since > self.rhythm.mode_max_hold_s:
                forced = self._rotate(self.current)
                self._switch(forced, at)
                return forced
            return self.current

        # R4: switching more often than every 3.5s turns into mush.
        if at - self.last_switch < self.rhythm.mode_min_interval_s and not meme:
            return self.current

        self._switch(wanted, at)
        return wanted

    def _preferred(self, intent: VisualIntent, section: str, meme: bool) -> Mode:
        if meme:
            return "MODE_B"  # R6: a meme always plays full frame
        if section in DIRECT_SECTIONS:
            return "MODE_C"
        return INTENT_MODE.get(intent, "MODE_B")

    def _rotate(self, mode: Mode) -> Mode:
        return {"MODE_A": "MODE_C", "MODE_B": "MODE_C", "MODE_C": "MODE_B"}[mode]

    def _switch(self, mode: Mode, at: float) -> None:
        self.current = mode
        self.held_since = at
        self.last_switch = at


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def build_shotlist(
    script: Script,
    config: Config,
    *,
    word_timings: list[dict] | None = None,
    total_duration_s: float | None = None,
) -> Shotlist:
    """Turn a script into a frame-accurate shot plan."""
    rhythm = RhythmConfig.from_config(config)
    duration = total_duration_s or _estimated_duration(script, rhythm)
    selector = ModeSelector(rhythm)

    shots: list[Shot] = []
    bursts: list[Burst] = []
    cursor = 0.0
    counter = 0

    for beat in script.beats:
        beat_seconds = beat.duration_hint_s or _beat_share(beat, script, duration)
        units = split_units(beat.text, beat.entities)
        burst = (
            is_enumeration(beat.text, beat.entities)
            and beat.section in {"BODY", "CLIMAX"}
            and len(bursts) < rhythm.burst_per_video_max
        )

        # Lengths come from the section's rhythm profile (§2.3), not from the
        # mode ceiling: a ceiling-length shot is the slowest legal edit, and
        # aiming at it puts the median above the 1.2-2.2s target every time.
        target = section_target(beat.section, rhythm)
        plan = plan_beat(beat_seconds, units, target, rhythm, burst=burst)
        first_index = counter
        burst_ids: list[str] = []

        for unit, length, in_burst in plan:
            counter += 1
            # The mode is chosen per shot, so R3 (12s ceiling on one mode) can
            # fire inside a long beat instead of only at beat boundaries.
            mode = selector.choose(
                beat.visual_intent, beat.section, cursor, length=length, meme=beat.meme_slot
            )
            shot = Shot(
                id=f"s{counter:03d}",
                beat_id=beat.id,
                t_start=round(cursor, 3),
                t_end=round(cursor + length, 3),
                mode=mode,
                visual_intent="MEME" if beat.meme_slot else beat.visual_intent,
                entity=unit if unit in beat.entities else "",
                must_show=beat.must_show or unit,
                queries=default_queries(unit, beat.visual_intent, beat.must_show),
                motion=MODE_MOTION[mode],
                transition_in="cut",
                text_overlay=TextOverlay(words=_words(unit), style=_caption_style(mode)),
                priority=_priority(beat.section, beat.emotion),
                fallback_intent=_fallback(beat.visual_intent),
                is_burst=in_burst,
            )
            shots.append(shot)
            cursor = shot.t_end
            if in_burst:
                burst_ids.append(shot.id)

        if burst_ids:
            bursts.append(
                Burst(
                    at=round(shots[first_index].t_start, 3),
                    count=len(burst_ids),
                    shot_ids=burst_ids,
                )
            )

    shots = _apply_closer(shots, rhythm, cursor)
    shots = _rebalance_presenter(shots, rhythm)
    shots = _repair_mode_runs(shots, rhythm)
    total = shots[-1].t_end if shots else 0.0

    shotlist = Shotlist(
        fps=rhythm.fps,
        total_duration_s=round(total, 3),
        shots=shots,
        bursts=bursts,
        mode_budget=_mode_budget(shots, total),
    )
    log.info(
        "%d shots, median %.2fs, presenter %.0f%%, %d burst(s)",
        len(shots),
        shotlist.median_shot_s(),
        shotlist.presenter_share() * 100,
        len(bursts),
        extra={"stage": STAGE},
    )
    return shotlist


def section_target(section: str, rhythm: RhythmConfig) -> float:
    """Preferred shot length for a section, from the rhythm profile (§2.3)."""
    profile = rhythm.sections.get(section.lower(), {})
    low, high = profile.get("shot_len", [rhythm.median_min, rhythm.median_max])
    return (float(low) + float(high)) / 2


def plan_beat(
    beat_seconds: float,
    units: Sequence[str],
    target_s: float,
    rhythm: RhythmConfig,
    *,
    burst: bool,
) -> list[tuple[str, float, bool]]:
    """Cover a beat exactly, as `(unit, length, is_burst)` triples.

    Two rules drive the shot count. A beat longer than the mode's ceiling needs
    *more* shots, not longer ones — clamping a 11s beat to one 2.9s shot would
    silently drop 8 seconds of narration. And a BURST is punctuation *inside*
    the beat (§2.3), so the series takes its share and the rest of the beat is
    edited normally after it.
    """
    if beat_seconds <= 0 or not units:
        return []

    plan: list[tuple[str, float, bool]] = []
    remaining = beat_seconds
    cycle = list(units)
    index = 0

    if burst:
        low, high = rhythm.burst_len
        count = max(rhythm.burst_min_shots, min(len(units), rhythm.burst_max_shots))
        # The series may not eat more than a third of the beat, or the words
        # spoken over it become unreadable.
        length = max(low, min(high, beat_seconds / 3 / count))
        for _ in range(count):
            plan.append((cycle[index % len(cycle)], round(length, 3), True))
            index += 1
            remaining -= length

    normal = _even_lengths(remaining, target_s, rhythm)
    for length in normal:
        plan.append((cycle[index % len(cycle)], length, False))
        index += 1

    _fix_drift(plan, beat_seconds, rhythm)
    return plan


def _even_lengths(seconds: float, target_s: float, rhythm: RhythmConfig) -> list[float]:
    """Split `seconds` into shots as close to `target_s` as the total allows.

    The count is rounded, not floored: at 5.0s with a 1.8s target, four shots of
    1.25s read better than two of 2.5s, and the ceiling of the tightest mode
    (2.9s) is respected either way.
    """
    if seconds <= 0.01:
        return []
    tightest_ceiling = min(rhythm.mode_max.values())
    count = max(1, round(seconds / target_s))
    count = max(count, math.ceil(seconds / tightest_ceiling))
    return [round(seconds / count, 3)] * count


def _fix_drift(plan: list[tuple[str, float, bool]], target: float, rhythm: RhythmConfig) -> None:
    """Absorb rounding into the longest non-burst shot, keeping VO in sync."""
    drift = target - sum(length for _, length, _ in plan)
    if abs(drift) < 0.005 or not plan:
        return
    candidates = [i for i, (_, _, is_burst) in enumerate(plan) if not is_burst]
    if not candidates:
        return
    index = max(candidates, key=lambda i: plan[i][1])
    unit, length, is_burst = plan[index]
    ceiling = min(rhythm.mode_max.values())
    plan[index] = (unit, round(min(max(length + drift, 0.1), ceiling), 3), is_burst)


def _repair_mode_runs(shots: list[Shot], rhythm: RhythmConfig) -> list[Shot]:
    """Merge mode runs shorter than the switching interval back into neighbours.

    Rebalancing the presenter share flips individual shots, which can leave a
    one-shot island of another mode — legal on paper, mush on screen. R4 exists
    to stop exactly that, so the island is absorbed rather than kept.
    """
    if len(shots) < 2:
        return shots
    closer_boundary = shots[-1].t_end - rhythm.closer_len_s

    index = 0
    while index < len(shots):
        end = index
        while end + 1 < len(shots) and shots[end + 1].mode == shots[index].mode:
            end += 1
        run = shots[index : end + 1]
        span = run[-1].t_end - run[0].t_start
        protected = run[0].t_start >= closer_boundary or any(s.visual_intent == "MEME" for s in run)
        if span + 0.001 < rhythm.mode_min_interval_s and index > 0 and not protected:
            neighbour = shots[index - 1].mode
            for shot in run:
                if shot.duration <= rhythm.mode_max[neighbour] + 0.001:
                    shot.mode = neighbour
                    shot.motion = MODE_MOTION[neighbour]
            index = 0  # runs merged, re-scan from the start
            continue
        index = end + 1
    return shots


def _apply_closer(shots: list[Shot], rhythm: RhythmConfig, total: float) -> list[Shot]:
    """R7: the last 2 seconds are always the presenter's face plus SUBSCRIBE."""
    if not shots:
        return shots
    boundary = max(0.0, total - rhythm.closer_len_s)
    for shot in shots:
        if shot.t_end > boundary:
            shot.mode = rhythm.closer_mode
            shot.motion = MODE_MOTION[rhythm.closer_mode]
            if shot.visual_intent != "MEME":
                shot.visual_intent = "PRESENTER_ONLY"
    return shots


def _rebalance_presenter(shots: list[Shot], rhythm: RhythmConfig) -> list[Shot]:
    """R2: pull the presenter share into the 40-70% band the brandbook demands."""
    total = sum(shot.duration for shot in shots)
    if total <= 0:
        return shots

    def share() -> float:
        return sum(s.duration for s in shots if s.mode in {"MODE_A", "MODE_C"}) / total

    # Whole runs are flipped, never single shots: a lone shot in another mode
    # is an R4 violation by construction, and it looks like a glitch.
    runs = _mode_runs(shots)

    promotable = sorted(
        (
            run
            for run in runs
            if run[0].mode == "MODE_B"
            and not any(s.is_burst or s.visual_intent == "MEME" for s in run)
            and all(s.duration >= rhythm.mode_min["MODE_A"] for s in run)
        ),
        key=lambda run: -_run_span(run),
    )
    for run in promotable:
        if share() >= rhythm.presenter_min:
            break
        _set_mode(run, "MODE_A")

    # Demoting only works on runs short enough for MODE_B: a 5s shot cannot
    # move there without breaking the 2.9s ceiling, and shortening it here
    # would desync the narration.
    demotable = sorted(
        (
            run
            for run in runs
            if run[0].mode == "MODE_A"
            and INTENT_MODE.get(run[0].visual_intent) == "MODE_B"
            and all(s.duration <= rhythm.mode_max["MODE_B"] for s in run)
        ),
        key=lambda run: -_run_span(run),
    )
    for run in demotable:
        if share() <= rhythm.presenter_max:
            break
        _set_mode(run, "MODE_B")
    return shots


def _mode_runs(shots: list[Shot]) -> list[list[Shot]]:
    """Contiguous stretches of the same mode."""
    runs: list[list[Shot]] = []
    for shot in shots:
        if runs and runs[-1][0].mode == shot.mode:
            runs[-1].append(shot)
        else:
            runs.append([shot])
    return runs


def _run_span(run: list[Shot]) -> float:
    return run[-1].t_end - run[0].t_start


def _set_mode(run: list[Shot], mode: Mode) -> None:
    for shot in run:
        shot.mode = mode
        shot.motion = MODE_MOTION[mode]


def default_queries(unit: str, intent: VisualIntent, must_show: str) -> ShotQueries:
    """Fallback three-level queries when no LLM produced them (§9.2)."""
    literal = [unit.strip()] if unit.strip() else []
    conceptual = [f"{must_show or unit} cinematic".strip()]
    stock = _STOCK_HINT.get(intent, ("4k footage",))
    return ShotQueries(
        literal=literal,
        conceptual=[c for c in conceptual if c],
        stock_en=[f"{hint}" for hint in stock],
    )


_STOCK_HINT: dict[str, tuple[str, ...]] = {
    "COSMIC_HERO": ("nebula 4k", "space telescope stars"),
    "SIMULATION": ("particle simulation dark", "scientific visualization"),
    "LAB_TECH": ("laboratory closeup dark", "server room dark"),
    "ABSTRACT_NEURAL": ("abstract particles loop", "neural network background"),
    "ARCHIVE": ("archive footage launch", "historical film grain"),
    "HUMAN_SCALE": ("person silhouette scale", "scientist looking at screen"),
    "UI_WALKTHROUGH": ("screen recording interface", "dark ui closeup"),
    "ICON_LOGO": ("logo icon dark background",),
}


def _fallback(intent: VisualIntent) -> VisualIntent:
    """Every shot needs an insurance policy for the degradation ladder (§8.4)."""
    if intent in {"DATA_VIZ", "DIAGRAM"}:
        return "DATA_VIZ"  # we render these ourselves, always available
    if intent in {"PRESENTER_ONLY", "ARTICLE", "UI_WALKTHROUGH"}:
        return "PRESENTER_ONLY"
    return "ABSTRACT_NEURAL"


def _priority(section: str, emotion: str) -> int:
    if section in {"HOOK", "CLIMAX"}:
        return 1
    if emotion in {"awe", "shock", "tension"}:
        return 2
    return 2 if section == "BODY" else 3


def _caption_style(mode: Mode) -> str:
    return {"MODE_A": "two_line", "MODE_B": "karaoke", "MODE_C": "short"}[mode]


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)[:6]


def _beat_share(beat, script: Script, duration: float) -> float:
    total_words = sum(b.word_count for b in script.beats) or 1
    return max(0.6, duration * beat.word_count / total_words)


def _estimated_duration(script: Script, rhythm: RhythmConfig) -> float:
    """155 words per minute is the reference pace (§7.1)."""
    words = sum(beat.word_count for beat in script.beats)
    return round(words / 155.0 * 60.0, 3) if words else rhythm.target_s


def _mode_budget(shots: Iterable[Shot], total: float) -> dict[str, float]:
    if total <= 0:
        return {}
    budget: dict[str, float] = {}
    for shot in shots:
        budget[shot.mode] = budget.get(shot.mode, 0.0) + shot.duration
    return {mode: round(value / total, 3) for mode, value in budget.items()}


# --------------------------------------------------------------------------- #
# Checks — §8.4 "проверка перед выходом"
# --------------------------------------------------------------------------- #


def check_rhythm(shotlist: Shotlist, config: Config) -> list[str]:
    """Return every rhythm rule the shotlist breaks. Empty list = clean."""
    rhythm = RhythmConfig.from_config(config)
    problems: list[str] = []

    if not shotlist.shots:
        return ["shotlist has no shots"]

    for shot in shotlist.shots:
        ceiling = rhythm.mode_max[shot.mode]
        if shot.is_burst:
            continue  # bursts are deliberately below the floor
        if shot.duration > ceiling + 0.001:
            problems.append(f"{shot.id}: {shot.duration:g}s exceeds the {shot.mode} ceiling {ceiling:g}s")

    share = shotlist.presenter_share()
    if not (rhythm.presenter_min - 0.001 <= share <= rhythm.presenter_max + 0.001):
        problems.append(
            f"presenter share {share:.0%} outside the {rhythm.presenter_min:.0%}-"
            f"{rhythm.presenter_max:.0%} band"
        )

    median = shotlist.median_shot_s()
    if not (rhythm.median_min <= median <= rhythm.median_max):
        problems.append(
            f"median shot {median:.2f}s outside the target {rhythm.median_min:g}-{rhythm.median_max:g}s"
        )

    if shotlist.shots[0].mode not in rhythm.first_shot_modes:
        problems.append(f"opens on {shotlist.shots[0].mode}; R1 requires one of {rhythm.first_shot_modes}")

    if len(shotlist.bursts) > rhythm.burst_per_video_max:
        problems.append(f"{len(shotlist.bursts)} bursts; the ceiling is {rhythm.burst_per_video_max}")

    problems.extend(_check_mode_switching(shotlist, rhythm))

    closer = shotlist.shots[-1]
    if closer.mode != rhythm.closer_mode:
        problems.append(f"final shot is {closer.mode}; R7 requires {rhythm.closer_mode}")

    if any(shot.fallback_intent is None for shot in shotlist.shots):
        problems.append("some shots have no fallback_intent")

    return problems


def _check_mode_switching(shotlist: Shotlist, rhythm: RhythmConfig) -> list[str]:
    problems: list[str] = []
    last_switch = 0.0
    held_mode = shotlist.shots[0].mode
    reported_hold = False
    for shot in shotlist.shots[1:]:
        if shot.mode == held_mode:
            if shot.t_end - last_switch > rhythm.mode_max_hold_s + 0.001 and not reported_hold:
                problems.append(
                    f"{shot.mode} held for {shot.t_end - last_switch:.1f}s; "
                    f"R3 caps it at {rhythm.mode_max_hold_s:g}s"
                )
                reported_hold = True
            continue
        gap = shot.t_start - last_switch
        if gap + 0.001 < rhythm.mode_min_interval_s:
            problems.append(
                f"{shot.id}: mode switched after {gap:.1f}s; R4 requires {rhythm.mode_min_interval_s:g}s"
            )
        held_mode = shot.mode
        last_switch = shot.t_start
        reported_hold = False
    return problems


def assert_rhythm(shotlist: Shotlist, config: Config) -> None:
    """Hard gate. A shotlist that breaks the rules is an editorial defect."""
    problems = check_rhythm(shotlist, config)
    if problems:
        raise RhythmViolation("; ".join(problems))
