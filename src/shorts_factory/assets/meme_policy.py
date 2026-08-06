"""Meme insertion for REDSHIFT sci-pop meta-irony.

The channel is science popularization with a dry Russian sense of humor —
not random meme spam. One well-timed reaction beats three cheap cuts.

Placement language (beats):

* ``hook_punch`` — 0.4–1.2s after the opening question (never *during* it)
* ``misconception`` — right after a myth / wrong intuition is named
* ``absurd_scale`` — after an impossible number (90 atm, 10⁹ K, …)
* ``deadpan_accept`` — dry acceptance of horror («естественно»)
* ``reveal_twist`` — seam between core beats / plot twist
* ``praise_irony`` — sarcastic «гениально» after an «obvious» conclusion
* ``doubt`` — soft skepticism
* ``context_end`` / ``core1_to_core2`` — classic brand-book seams

Hard rules:

* Prefer ≤ 1 meme per Short; a second only if duration ≥ 55s and gap ≥ 12s
* Prefer clips with ``max_use`` ≤ 1.5s; long sources are trimmed to the punch
* Medicine stays banned; science/sci-pop is allowed (irony is the brand)
* Hard-intensity / unsafe-for-science memes never auto-fire on космос/наука
* History in ``.state/meme-history.json`` keeps the cadence honest
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..spec import Spec, Visual

log = get_logger("meme_policy")

HISTORY_FILENAME = "meme-history.json"
DEFAULT_FREQUENCY = 4  # sci-pop irony signature: ~1 of 3–5
DEFAULT_FORBIDDEN = ("медицина", "medicine")
DEFAULT_BEATS = (
    "hook_punch",
    "misconception",
    "absurd_scale",
    "deadpan_accept",
    "reveal_twist",
    "context_end",
    "core1_to_core2",
)

_NUMBER_RE = re.compile(
    r"(?:"
    r"\d+(?:[.,]\d+)?\s*(?:%|°|к|K|км|м|с|атм|г|кг|т|св\.?\s*лет|световых)?"
    r"|"
    r"девяност|миллиард|триллион|тысяч|миллион|бесконеч"
    r")",
    re.IGNORECASE,
)
_MYTH_RE = re.compile(
    r"(миф|кажется|принято считать|многие думают|на самом деле|перепутал|не так)",
    re.IGNORECASE,
)
_HORROR_RE = re.compile(
    r"(убива|смерт|раздав|испари|разорв|ад|кошмар|невозможн|выжить)",
    re.IGNORECASE,
)


@dataclass
class MemePolicyConfig:
    enabled: bool = True
    frequency: int = DEFAULT_FREQUENCY
    max_duration: float = 1.4
    max_per_video: int = 1
    forbidden_rubrics: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN))
    allowed_beats: list[str] = field(default_factory=lambda: list(DEFAULT_BEATS))
    prefer_safe_for_science: bool = True

    @classmethod
    def from_brandbook(cls, raw: dict[str, Any] | None) -> MemePolicyConfig:
        data = raw if isinstance(raw, dict) else {}
        forbidden = data.get("forbidden_rubrics")
        beats = data.get("allowed_beats")
        frequency = data.get("frequency", DEFAULT_FREQUENCY)
        max_duration = data.get("max_duration", 1.4)
        max_per = data.get("max_per_video", 1)
        return cls(
            enabled=bool(data.get("enabled", True)),
            frequency=max(1, int(frequency) if isinstance(frequency, (int, float)) else DEFAULT_FREQUENCY),
            max_duration=float(max_duration) if isinstance(max_duration, (int, float)) else 1.4,
            max_per_video=max(1, int(max_per) if isinstance(max_per, (int, float)) else 1),
            forbidden_rubrics=[str(item).lower() for item in forbidden]
            if isinstance(forbidden, list)
            else list(DEFAULT_FORBIDDEN),
            allowed_beats=[str(item) for item in beats] if isinstance(beats, list) else list(DEFAULT_BEATS),
            prefer_safe_for_science=bool(data.get("prefer_safe_for_science", True)),
        )


@dataclass
class MemeDecision:
    allowed: bool
    reason: str
    beat: str = ""
    start: float = 0.0
    duration: float = 1.4
    tags: list[str] = field(default_factory=list)
    humor: str = ""


@dataclass
class MemeHistory:
    videos: list[str] = field(default_factory=list)
    meme_at: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> MemeHistory:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("meme history unreadable (%s); starting fresh", exc)
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            videos=[str(item) for item in data.get("videos", []) if item],
            meme_at=[str(item) for item in data.get("meme_at", []) if item],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"videos": self.videos, "meme_at": self.meme_at}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def videos_since_last_meme(self) -> int:
        count = 0
        for video_id in reversed(self.videos):
            if video_id in self.meme_at:
                break
            count += 1
        return count

    def record(self, video_id: str, *, used_meme: bool) -> None:
        if video_id not in self.videos:
            self.videos.append(video_id)
        if used_meme and video_id not in self.meme_at:
            self.meme_at.append(video_id)


def history_path(root: Path) -> Path:
    return root / ".state" / HISTORY_FILENAME


def decide_meme(spec: Spec, policy: MemePolicyConfig, history: MemeHistory) -> MemeDecision:
    """Should this video carry a meme, which beat, and which tags to match?"""
    if not policy.enabled:
        return MemeDecision(False, "brandbook memes disabled")
    if not spec.memes.enabled:
        return MemeDecision(False, "scenario memes.enabled is false")

    rubric = (spec.rubric or "").strip().lower()
    if rubric and rubric in {item.lower() for item in policy.forbidden_rubrics}:
        return MemeDecision(False, f"rubric {spec.rubric!r} forbids memes")

    # Cold start: empty history may carry a meme (signature hook punch).
    # Afterwards enforce ~1 meme every ``frequency`` videos.
    if history.videos and spec.id not in history.meme_at:
        since = history.videos_since_last_meme()
        min_gap = max(1, policy.frequency - 1)
        if since < min_gap:
            return MemeDecision(
                False,
                f"frequency gate: {since} video(s) since last meme, need ≥ {min_gap}",
            )

    candidates = rank_beats(spec, policy)
    if not candidates:
        return MemeDecision(False, "no ironic beat available on this timeline")

    beat, start, duration, tags, humor = candidates[0]
    duration = min(duration, spec.memes.max_duration or policy.max_duration, policy.max_duration)
    return MemeDecision(
        True,
        f"irony beat {beat}",
        beat=beat,
        start=start,
        duration=duration,
        tags=tags,
        humor=humor,
    )


def rank_beats(spec: Spec, policy: MemePolicyConfig) -> list[tuple[str, float, float, list[str], str]]:
    """Score possible insert moments; highest priority first."""
    segments = spec.all_segments
    if len(segments) < 2:
        return []

    hook = spec.hook
    body = [segment for segment in segments if hook is None or segment.id != hook.id]
    climax_start = spec.cta.start if spec.cta else max(0.0, spec.duration_target - 4.0)
    hook_end = hook.end if hook else 0.0
    duration_cap = min(spec.memes.max_duration or policy.max_duration, policy.max_duration, 1.5)

    scored: list[tuple[float, str, float, float, list[str], str]] = []

    # 1) Hook punch — the sci-pop signature: question → tiny ironic cut → answer.
    if "hook_punch" in policy.allowed_beats and hook is not None:
        start = min(hook.end + 0.15, max(hook.end - 0.05, hook.start + hook.duration * 0.85))
        if _safe_window(start, duration_cap, hook_end=0.0, climax_start=climax_start):
            # Prefer landing just after the hook finishes speaking.
            start = hook.end + 0.08
            if start + duration_cap < climax_start:
                tags = ["hook", "вопрос", "шок", "wtf", "заявление"]
                scored.append((90.0, "hook_punch", start, min(1.2, duration_cap), tags, "hook_punch"))

    # 2) Numbers / absurd scale on emphasised body beats.
    if "absurd_scale" in policy.allowed_beats:
        for segment in body:
            if not _NUMBER_RE.search(segment.text) and segment.emphasis != "high":
                continue
            if not _NUMBER_RE.search(segment.text) and not _HORROR_RE.search(segment.text):
                continue
            start = segment.start + min(segment.duration * 0.55, max(0.8, segment.duration - 1.2))
            if not _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
                continue
            priority = 80.0 if _NUMBER_RE.search(segment.text) else 70.0
            scored.append(
                (
                    priority,
                    "absurd_scale",
                    start,
                    duration_cap,
                    ["цифра", "шок", "челюсть", "страшно", "вау"],
                    "shock",
                )
            )
            break

    # 3) Myth / misconception beat.
    if "misconception" in policy.allowed_beats:
        for segment in body:
            if not _MYTH_RE.search(segment.text):
                continue
            start = segment.start + min(0.6, segment.duration * 0.35)
            if not _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
                continue
            scored.append(
                (
                    75.0,
                    "misconception",
                    start,
                    duration_cap,
                    ["миф", "перепутал", "нет", "сомнение", "ошибка"],
                    "misconception",
                )
            )
            break

    # 4) Deadpan accept after horror language.
    if "deadpan_accept" in policy.allowed_beats:
        for segment in body:
            if not _HORROR_RE.search(segment.text):
                continue
            start = segment.end - min(duration_cap, 1.0)
            start = max(segment.start + 0.4, start)
            if not _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
                continue
            scored.append(
                (
                    72.0,
                    "deadpan_accept",
                    start,
                    duration_cap,
                    ["естественно", "deadpan", "сойдёт", "принято"],
                    "deadpan_accept",
                )
            )
            break

    # 5) Reveal twist on core1→core2 seam.
    if "reveal_twist" in policy.allowed_beats and len(body) >= 3:
        core1, core2 = body[1], body[2]
        start = max(core1.end - 0.05, core1.end)
        if start + duration_cap > core2.start + 0.2:
            start = core2.start
        if _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
            scored.append(
                (
                    65.0,
                    "reveal_twist",
                    start,
                    duration_cap,
                    ["поворот", "твист", "неожиданно"],
                    "reveal_twist",
                )
            )

    if "core1_to_core2" in policy.allowed_beats and len(body) >= 3:
        core1, core2 = body[1], body[2]
        start = core2.start
        if _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
            scored.append(
                (
                    55.0,
                    "core1_to_core2",
                    start,
                    duration_cap,
                    ["переход", "later", "поворот"],
                    "time_skip",
                )
            )

    if "context_end" in policy.allowed_beats and body:
        context = body[0]
        start = max(context.end - duration_cap, context.start + 0.3)
        if _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
            scored.append(
                (
                    50.0,
                    "context_end",
                    start,
                    duration_cap,
                    ["контекст", "later", "переход"],
                    "time_skip",
                )
            )

    scored.sort(key=lambda item: (-item[0], item[2]))
    return [(beat, start, dur, tags, humor) for _, beat, start, dur, tags, humor in scored]


def find_meme_window(spec: Spec, policy: MemePolicyConfig) -> tuple[str, float, float] | None:
    ranked = rank_beats(spec, policy)
    if not ranked:
        return None
    beat, start, duration, _tags, _humor = ranked[0]
    return beat, start, duration


def _safe_window(start: float, duration: float, *, hook_end: float, climax_start: float) -> bool:
    end = start + duration
    if start < 0.0:
        return False
    if hook_end > 0 and start < hook_end + 0.05:
        return False
    return end <= climax_start - 0.05


def visual_in_allowed_beat(spec: Spec, visual: Visual, policy: MemePolicyConfig) -> bool:
    window = find_meme_window(spec, policy)
    if window is None:
        return False
    _beat, start, duration = window
    return visual.start < start + duration + 0.75 and visual.end > start - 0.75


def ensure_meme_visual(spec: Spec, decision: MemeDecision) -> Visual | None:
    """Inject a meme visual when the policy says yes and none exists yet."""
    if not decision.allowed:
        return None
    if any(visual.type == "meme" for visual in spec.visuals):
        return None

    tags = decision.tags or list(spec.memes.tags) or [decision.humor or decision.beat]
    visual = Visual(
        id="meme-auto",
        type="meme",
        query=" ".join(tags[:4]),
        keywords=list(tags),
        start=decision.start,
        duration=decision.duration,
        position="fullscreen",
        motion="none",
        priority="low",
        notes=f"auto-irony:{decision.beat}:{decision.humor}",
    )
    spec.visuals.append(visual)
    spec.visuals.sort(key=lambda item: (item.start, item.id))
    log.info(
        "inserted meme at %.2fs (%s / %s)",
        decision.start,
        decision.beat,
        decision.humor,
        extra={"stage": "memes"},
    )
    return visual
