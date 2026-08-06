"""Meme insertion for REDSHIFT (brand book §11).

Hard rules from the plan:

* Frequency ~1 of 8–12 videos (history in ``.state/meme-history.json``)
* Insert only at end of «Контекст» or on «Ядро 1 → Ядро 2» seam
* Never in the hook and never in the climax
* Science and medicine rubrics banned by default
* Enter/exit with a branded whoosh (added by audio_design on meme visuals)

One well-timed reaction beats three cheap cuts.
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
#: Plan: roughly 1 meme per 8–12 videos.
DEFAULT_FREQUENCY = 10
DEFAULT_FORBIDDEN = ("медицина", "medicine", "наука", "science")
DEFAULT_BEATS = (
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

    # Cold start: empty history may carry a meme on an allowed seam.
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
    """Score allowed insert moments only — context end or core1→core2."""
    segments = spec.all_segments
    if len(segments) < 2:
        return []

    hook = spec.hook
    body = [segment for segment in segments if hook is None or segment.id != hook.id]
    climax_start = spec.cta.start if spec.cta else max(0.0, spec.duration_target - 4.0)
    hook_end = hook.end if hook else 0.0
    duration_cap = min(spec.memes.max_duration or policy.max_duration, policy.max_duration, 1.5)

    scored: list[tuple[float, str, float, float, list[str], str]] = []

    if "core1_to_core2" in policy.allowed_beats and len(body) >= 3:
        core1, core2 = body[1], body[2]
        start = core2.start
        if _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
            scored.append(
                (
                    70.0,
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
                    60.0,
                    "context_end",
                    start,
                    duration_cap,
                    ["контекст", "later", "переход"],
                    "time_skip",
                )
            )

    # Optional extended beats only if brandbook explicitly lists them.
    if "reveal_twist" in policy.allowed_beats and len(body) >= 3:
        core1, core2 = body[1], body[2]
        start = max(core1.end - 0.05, core1.end)
        if start + duration_cap > core2.start + 0.2:
            start = core2.start
        if _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
            scored.append(
                (
                    55.0,
                    "reveal_twist",
                    start,
                    duration_cap,
                    ["поворот", "твист", "неожиданно"],
                    "reveal_twist",
                )
            )

    if "absurd_scale" in policy.allowed_beats:
        for segment in body:
            if not _NUMBER_RE.search(segment.text):
                continue
            start = segment.start + min(segment.duration * 0.55, max(0.8, segment.duration - 1.2))
            if not _safe_window(start, duration_cap, hook_end=hook_end, climax_start=climax_start):
                continue
            scored.append(
                (
                    50.0,
                    "absurd_scale",
                    start,
                    duration_cap,
                    ["цифра", "шок", "челюсть"],
                    "shock",
                )
            )
            break

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
    # Never during the hook.
    if hook_end > 0 and start < hook_end + 0.05:
        return False
    # Never into the climax / CTA.
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
