"""Automatic audio design.

A Short with silent cuts feels amateur; a Short with a whoosh on every cut
feels cheap. Two jobs live here:

* **Placement** — if the author did not hand-place `audio_fx`, propose a
  restrained set keyed to the brand's montage language: one hero hit under
  the hook, whooshes only on real topic changes, a soft thump under an
  emphasised number, a short riser into the climax, a tick as the host
  leaves and returns. Author-provided effects always win (then get density-
  capped and peak-trimmed).
* **Scoring / indexing** — the SFX bank is ranked for *short accents*
  (oneshot / ≤ ~1.5s useful body). Long cinematic beds and drones are
  demoted so the Short gets a handful of sharp "burps", not a wall of FX.

Nothing here invents drama the narration does not already carry. The effects
are punctuation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assets.library import LibraryItem, SfxLibrary
from ..logging_utils import get_logger
from ..spec import AudioFx, Spec, Visual
from ..voice.sfx_analysis import gain_db_for
from .elevenlabs import SfxClip

log = get_logger("audio_design")

#: Hard ceiling — past five accents a Short starts to sound like a trailer.
MAX_AUTO_FX = 5
#: Preferred useful body for an accent ("short burp").
PREFERRED_ACCENT_S = 1.2
#: Absolute play length after trim — never drag a 10s cinematic under VO.
MAX_PLAY_S = 1.6
#: Cuts closer together than this share one effect.
MIN_GAP = 1.8
#: Same type of effect may not land again within this window.
SAME_TYPE_GAP = 5.0
#: How long a riser is allowed to run before the climax it is building to.
RISER_LEAD = 1.4
#: Soft headroom so a peak that starts slightly before t=0 still plays.
EARLY_START_SLACK = 0.05
#: Tails longer than this get trimmed with a fade.
MAX_TAIL = 1.0


@dataclass(frozen=True)
class Beat:
    """One moment on the timeline that deserves a sound."""

    at: float
    role: str
    intensity: float = 0.6
    duration: float = 0.55
    reason: str = ""


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #


def suggest_audio_fx(spec: Spec) -> list[AudioFx]:
    """Propose effects for a spec that has none.

    Author-provided `audio_fx` always wins, then passes through the same
    density + short-accent gate as autosuggest so a hand-authored list of
    twelve whooshes cannot flood the mix.
    """
    if spec.audio_fx:
        return finalize_audio_fx(list(spec.audio_fx), spec)

    beats = collect_beats(spec)
    policy = spec.sfx_policy
    trimmed = enforce_density(
        beats,
        duration=spec.duration_target,
        max_effects=policy.max_effects,
        min_gap=policy.min_gap,
        type_cooldown=policy.type_cooldown,
    )
    suggestions = [
        AudioFx(
            type=beat.role,
            at=round(beat.at, 3),
            intensity=beat.intensity,
            duration=min(beat.duration, PREFERRED_ACCENT_S),
        )
        for beat in trimmed
    ]
    for item in suggestions:
        log.info(
            "suggested %s @ %.2fs (%s)",
            item.type,
            item.at,
            next((beat.reason for beat in trimmed if beat.at == item.at and beat.role == item.type), ""),
        )
    return suggestions


def finalize_audio_fx(effects: list[AudioFx], spec: Spec) -> list[AudioFx]:
    """Cap + shorten an author or auto list before library resolve."""
    policy = spec.sfx_policy
    beats = [
        Beat(
            at=fx.at,
            role=fx.type,
            intensity=fx.intensity,
            duration=min(fx.duration or PREFERRED_ACCENT_S, PREFERRED_ACCENT_S),
            reason="author",
        )
        for fx in effects
    ]
    trimmed = enforce_density(
        beats,
        duration=spec.duration_target,
        max_effects=policy.max_effects if policy.max_effects is not None else MAX_AUTO_FX,
        min_gap=policy.min_gap,
        type_cooldown=policy.type_cooldown,
    )
    by_key = {(fx.type, round(fx.at, 3)): fx for fx in effects}
    out: list[AudioFx] = []
    for beat in trimmed:
        source = by_key.get((beat.role, round(beat.at, 3)))
        duration = min(
            (source.duration if source else beat.duration) or PREFERRED_ACCENT_S,
            PREFERRED_ACCENT_S if beat.role != "riser" else RISER_LEAD,
            MAX_PLAY_S,
        )
        out.append(
            AudioFx(
                type=beat.role,
                at=round(beat.at, 3),
                intensity=beat.intensity,
                duration=round(duration, 3),
                prompt=source.prompt if source else "",
            )
        )
    if len(effects) > len(out):
        log.info(
            "sfx density: kept %d of %d author effect(s)",
            len(out),
            len(effects),
            extra={"stage": "audio"},
        )
    return out


def collect_beats(spec: Spec) -> list[Beat]:
    """Map montage structure onto a sparse set of sound anchors."""
    beats: list[Beat] = []

    if spec.hook:
        beats.append(
            Beat(
                at=max(0.0, spec.hook.start + 0.05),
                role="impact",
                intensity=0.75,
                duration=0.55,
                reason="hook",
            )
        )

    # Topic changes, not every visual cut.
    for previous, current in zip(spec.visuals, spec.visuals[1:], strict=False):
        if not _is_topic_change(previous, current):
            continue
        if current.start < 0.6:
            continue
        beats.append(
            Beat(
                at=max(0.0, current.start - 0.05),
                role="whoosh",
                intensity=0.5,
                duration=0.45,
                reason=f"cut {previous.id}→{current.id}",
            )
        )

    guard = spec.sfx_policy.voice_guard
    for segment in spec.all_segments:
        if segment.emphasis != "high":
            continue
        at = segment.start + min(0.35, max(guard, segment.duration * 0.12))
        beats.append(Beat(at=at, role="thump", intensity=0.4, duration=0.35, reason=f"emphasis {segment.id}"))

    # Host exit / enter from avatar.segments gaps (FULL_FOOTAGE windows).
    if spec.avatar.enabled and spec.avatar.segments:
        on_ids = set(spec.avatar.segments)
        present = [segment for segment in spec.all_segments if segment.id in on_ids]
        for left, right in zip(present, present[1:], strict=False):
            gap = right.start - left.end
            if gap < 1.2:
                continue
            beats.append(
                Beat(at=left.end, role="host_exit", intensity=0.35, duration=0.2, reason="host exit")
            )
            beats.append(
                Beat(at=right.start, role="host_enter", intensity=0.35, duration=0.2, reason="host enter")
            )

    climax = _climax_at(spec)
    if climax is not None and climax > RISER_LEAD:
        beats.append(
            Beat(
                at=max(0.0, climax - RISER_LEAD),
                role="riser",
                intensity=0.55,
                duration=RISER_LEAD,
                reason="pre-climax",
            )
        )

    if spec.cta and spec.cta.text:
        beats.append(Beat(at=spec.cta.start, role="pop", intensity=0.5, duration=0.35, reason="cta"))

    return sorted(beats, key=lambda beat: beat.at)


def enforce_density(
    beats: list[Beat],
    *,
    duration: float,
    max_effects: int | None = None,
    min_gap: float = MIN_GAP,
    type_cooldown: float = SAME_TYPE_GAP,
) -> list[Beat]:
    """Cap how many sounds a Short gets and keep them from stacking.

    Ceiling scales with length so a 20-second short stays sparse and a
    55-second one still has room for a climax build — never above ``MAX_AUTO_FX``.
    """
    derived = max(3, min(MAX_AUTO_FX, int(duration / 10) + 1))
    ceiling = max_effects if max_effects is not None else derived
    ceiling = max(0, min(ceiling, MAX_AUTO_FX))
    kept: list[Beat] = []
    last_at = -min_gap
    last_by_type: dict[str, float] = {}

    # Priority: hook impact and climax riser first, then topic changes, then
    # the rest. Without this a dense script of emphasis marks would crowd out
    # the one sound the viewer is meant to notice.
    priority = {
        "impact": 0,
        "riser": 1,
        "whoosh": 2,
        "transition": 2,
        "thump": 3,
        "pop": 4,
        "host_exit": 5,
        "host_enter": 5,
    }
    ordered = sorted(beats, key=lambda beat: (priority.get(beat.role, 6), beat.at))

    accepted: list[Beat] = []
    for beat in ordered:
        if len(accepted) >= ceiling:
            break
        if beat.at - last_by_type.get(beat.role, -type_cooldown) < type_cooldown:
            continue
        if any(abs(beat.at - other.at) < min_gap and other.role != beat.role for other in accepted):
            continue
        accepted.append(beat)
        last_by_type[beat.role] = beat.at

    for beat in sorted(accepted, key=lambda item: item.at):
        if beat.at - last_at < min_gap and kept:
            continue
        kept.append(beat)
        last_at = beat.at
    return kept


def _is_topic_change(previous: Visual, current: Visual) -> bool:
    if previous.position != current.position:
        return True
    if current.type in {"motion_graphics", "infographic", "meme"} and previous.type != current.type:
        return True
    if current.priority in {"high", "critical"} and previous.priority not in {"high", "critical"}:
        return True
    if previous.query and current.query and previous.query.lower() != current.query.lower():
        prev_words = set(previous.query.lower().split())
        cur_words = set(current.query.lower().split())
        if not (prev_words & cur_words) and current.start - previous.end >= 0.4:
            return True
    # Same type and position — only count it if the gap between them is long
    # enough that the ear has settled; otherwise it is one continuous thought.
    return current.start - previous.end >= 0.8


def _climax_at(spec: Spec) -> float | None:
    emphasised = [segment for segment in spec.all_segments if segment.emphasis == "high"]
    if emphasised:
        return emphasised[-1].start
    heroes = [visual for visual in spec.visuals if visual.priority in {"high", "critical"}]
    if heroes:
        return heroes[-1].start
    if spec.visuals:
        return spec.visuals[len(spec.visuals) // 2].start
    return None


# --------------------------------------------------------------------------- #
# Bank indexing / scoring
# --------------------------------------------------------------------------- #


def accent_score(item: LibraryItem, role: str) -> float:
    """Rank a bank file for short-accent use. Higher = better burp.

    Prefers oneshots and short measured bodies; demotes drones, beds and
    multi-second cinematic hits that drown narration.
    """
    score = 0.0
    shape = (item.shape or "").lower()
    if shape in {"drone", "bed"}:
        return -100.0
    if shape in {"oneshot", "hit", "transient"}:
        score += 4.0
    elif shape == "whoosh":
        score += 2.0
    elif shape == "riser" and role == "riser":
        score += 3.0

    duration = item.duration or 0.0
    if duration <= 0:
        score += 0.5
    elif duration <= 0.6:
        score += 5.0
    elif duration <= PREFERRED_ACCENT_S:
        score += 3.5
    elif duration <= MAX_PLAY_S:
        score += 1.5
    elif duration <= 3.0:
        score -= 1.0
    else:
        score -= 4.0

    if item.tail and item.tail > MAX_TAIL:
        score -= min(3.0, item.tail - MAX_TAIL)

    # Sharp attack is good punctuation; long soft attacks feel like beds.
    if item.attack and item.attack < 0.15:
        score += 1.5
    elif item.attack and item.attack > 1.0 and role != "riser":
        score -= 1.5

    return score


def index_short_accents(library: SfxLibrary, *, limit: int = 5) -> list[LibraryItem]:
    """Return the top short accents in the bank (for diagnostics / kits)."""
    ranked = sorted(
        (item for item in library.items if item.usable_as_accent and accent_score(item, "impact") > 0),
        key=lambda item: accent_score(item, "impact"),
        reverse=True,
    )
    return ranked[: max(0, limit)]


# --------------------------------------------------------------------------- #
# Sourcing
# --------------------------------------------------------------------------- #


def fx_volume(intensity: float) -> float:
    """Legacy linear volume. Kept for tests and for generated (keyless) sounds.

    Library sounds go through `library_volume` instead, which normalises by
    measured loudness so a hand-collected bank spanning 20 dB lands evenly.
    """
    return round(0.15 + 0.30 * max(0.0, min(intensity, 1.0)), 3)


def library_volume(item: LibraryItem, role: str, intensity: float) -> float:
    """Peak linear gain for a measured bank sound.

    Combines the role's target LUFS with the author's intensity (a soft ±3 dB
    around the target) and converts the result to a linear multiplier the
    FFmpeg mix understands.
    """
    from .sfx_analysis import AudioProfile

    profile = AudioProfile(path=item.path, lufs=item.lufs, peak_dbtp=item.peak_dbtp)
    base_db = gain_db_for(profile, role)
    intensity_db = (max(0.0, min(intensity, 1.0)) - 0.6) * 5.0  # ±2.0 dB around the default
    total_db = base_db + intensity_db
    return round(10.0 ** (total_db / 20.0), 3)


def align_start(anchor: float, peak_at: float) -> float:
    """Start the file so its loudest moment lands on `anchor`."""
    return max(-EARLY_START_SLACK, anchor - max(0.0, peak_at))


def trim_plan(item: LibraryItem, *, role: str, requested: float) -> tuple[float, float]:
    """How much of the file to keep, and where to start reading it.

    Long cinematic hits are cut to a short burp around the peak; risers are
    cut so they finish on the climax rather than ringing past it. Returns
    ``(trim_start, play_duration)``.
    """
    duration = item.duration or requested
    if duration <= 0:
        return 0.0, min(requested, MAX_PLAY_S)

    if role == "riser":
        start = max(0.0, item.peak_at - RISER_LEAD)
        end = min(duration, item.peak_at + 0.2)
        return start, max(0.25, min(MAX_PLAY_S, end - start))

    # Prefer a tight window around the peak — "short burp".
    body = min(requested or PREFERRED_ACCENT_S, PREFERRED_ACCENT_S, MAX_PLAY_S)
    if item.tail > MAX_TAIL or duration > body + 0.4:
        start = max(0.0, item.peak_at - max(min(item.attack, 0.12), 0.04))
        end = min(duration, start + body)
        return start, max(0.15, end - start)

    return 0.0, min(duration, body)


def resolve_from_library(effects: list[AudioFx], library: SfxLibrary) -> tuple[list[SfxClip], list[AudioFx]]:
    """Split effects into "your bank has this" and "this needs generating".

    Picker uses accent scoring so long cinematic beds lose to short oneshots.
    Returns ``(clips, still_needed)``.
    """
    clips: list[SfxClip] = []
    missing: list[AudioFx] = []

    for fx in effects:
        extra = [word for word in fx.prompt.lower().split() if len(word) > 2][:4]
        item = library.pick(fx.type, extra, scorer=lambda it, role=fx.type: accent_score(it, role))
        if item is None:
            missing.append(fx)
            continue

        trim_start, play_duration = trim_plan(item, role=fx.type, requested=fx.duration)
        play_duration = min(play_duration, MAX_PLAY_S)
        start = align_start(fx.at, item.peak_at - trim_start)
        volume = library_volume(item, fx.type, fx.intensity) if item.analysis else fx_volume(fx.intensity)

        clips.append(
            SfxClip(
                fx_id=fx.id,
                path=item.path,
                start=start,
                duration=play_duration,
                volume=volume,
                source="library",
                trim_start=trim_start,
            )
        )

    if clips:
        log.info(
            "%d of %d effect(s) came from your sfx bank (%s)",
            len(clips),
            len(effects),
            ", ".join(sorted({clip.path.stem for clip in clips})),
            extra={"stage": "audio"},
        )
    return clips, missing
