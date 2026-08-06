"""Meme insertion policy from the REDSHIFT brand book §11.

Rules enforced here:

* Roughly one meme every ``frequency`` videos (default 10 → about 1 of 8–12).
* Never on science / medicine rubrics (configurable forbidden list).
* Only on allowed narrative beats: end of context, or the core1→core2 seam.
* Never in the hook and never on the climax / CTA.
* History lives in ``.state/meme-history.json`` so the cadence survives runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..spec import Spec, Visual

log = get_logger("meme_policy")

HISTORY_FILENAME = "meme-history.json"
DEFAULT_FREQUENCY = 10
DEFAULT_FORBIDDEN = ("наука", "медицина", "science", "medicine")
DEFAULT_BEATS = ("context_end", "core1_to_core2")


@dataclass
class MemePolicyConfig:
    enabled: bool = True
    frequency: int = DEFAULT_FREQUENCY
    max_duration: float = 1.5
    forbidden_rubrics: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN))
    allowed_beats: list[str] = field(default_factory=lambda: list(DEFAULT_BEATS))

    @classmethod
    def from_brandbook(cls, raw: dict[str, Any] | None) -> MemePolicyConfig:
        data = raw if isinstance(raw, dict) else {}
        forbidden = data.get("forbidden_rubrics")
        beats = data.get("allowed_beats")
        frequency = data.get("frequency", DEFAULT_FREQUENCY)
        max_duration = data.get("max_duration", 1.5)
        return cls(
            enabled=bool(data.get("enabled", True)),
            frequency=max(1, int(frequency) if isinstance(frequency, (int, float)) else DEFAULT_FREQUENCY),
            max_duration=float(max_duration) if isinstance(max_duration, (int, float)) else 1.5,
            forbidden_rubrics=[str(item).lower() for item in forbidden]
            if isinstance(forbidden, list)
            else list(DEFAULT_FORBIDDEN),
            allowed_beats=[str(item) for item in beats] if isinstance(beats, list) else list(DEFAULT_BEATS),
        )


@dataclass
class MemeDecision:
    allowed: bool
    reason: str
    beat: str = ""
    start: float = 0.0
    duration: float = 1.5


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
        videos = [str(item) for item in data.get("videos", []) if item]
        meme_at = [str(item) for item in data.get("meme_at", []) if item]
        return cls(videos=videos, meme_at=meme_at)

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


def decide_meme(
    spec: Spec,
    policy: MemePolicyConfig,
    history: MemeHistory,
) -> MemeDecision:
    """Should this video carry a meme, and where?"""
    if not policy.enabled:
        return MemeDecision(False, "brandbook memes disabled")
    if not spec.memes.enabled:
        return MemeDecision(False, "scenario memes.enabled is false")

    rubric = (spec.rubric or "").strip().lower()
    if rubric and rubric in {item.lower() for item in policy.forbidden_rubrics}:
        return MemeDecision(False, f"rubric {spec.rubric!r} forbids memes")

    # Explicit meme slots in the scenario still need frequency + beat checks,
    # but if the author placed one we try to honour a legal beat.
    since = history.videos_since_last_meme()
    # frequency=10 → insert when at least 8 videos passed without a meme
    # (covers the "1 of 8–12" band with a deterministic floor).
    min_gap = max(1, policy.frequency - 2)
    if since < min_gap and spec.id not in history.meme_at:
        return MemeDecision(
            False,
            f"frequency gate: {since} video(s) since last meme, need ≥ {min_gap}",
        )

    window = find_meme_window(spec, policy)
    if window is None:
        return MemeDecision(False, "no allowed beat window on this timeline")
    beat, start, duration = window
    duration = min(duration, spec.memes.max_duration or policy.max_duration, policy.max_duration)
    return MemeDecision(True, "ok", beat=beat, start=start, duration=duration)


def find_meme_window(spec: Spec, policy: MemePolicyConfig) -> tuple[str, float, float] | None:
    """Pick an allowed insertion window from the narration structure."""
    segments = spec.all_segments
    if len(segments) < 2:
        return None

    hook = spec.hook
    body = [segment for segment in segments if hook is None or segment.id != hook.id]
    if not body:
        return None

    duration = min(spec.memes.max_duration or policy.max_duration, policy.max_duration, 1.5)
    climax_start = spec.cta.start if spec.cta else max(0.0, spec.duration_target - 4.0)

    candidates: list[tuple[str, float, float]] = []

    if "context_end" in policy.allowed_beats and body:
        # End of the first body beat ("Контекст").
        context = body[0]
        start = max(context.end - duration, context.start + 0.2)
        if _safe_window(start, duration, hook_end=hook.end if hook else 0.0, climax_start=climax_start):
            candidates.append(("context_end", start, duration))

    if "core1_to_core2" in policy.allowed_beats and len(body) >= 3:
        # Seam between core beat 1 and core beat 2 (skip context = body[0]).
        core1, core2 = body[1], body[2]
        start = max(core1.end - duration * 0.3, core1.end - 0.1)
        if start + duration > core2.start + 0.05:
            start = core2.start
        if _safe_window(start, duration, hook_end=hook.end if hook else 0.0, climax_start=climax_start):
            candidates.append(("core1_to_core2", start, duration))

    return candidates[0] if candidates else None


def _safe_window(start: float, duration: float, *, hook_end: float, climax_start: float) -> bool:
    end = start + duration
    if start < hook_end + 0.05:
        return False
    return end <= climax_start - 0.05


def visual_in_allowed_beat(spec: Spec, visual: Visual, policy: MemePolicyConfig) -> bool:
    """True when an author-placed meme visual sits on an allowed beat."""
    window = find_meme_window(spec, policy)
    if window is None:
        return False
    _beat, start, duration = window
    # Accept if the visual overlaps the planned window closely.
    return visual.start < start + duration + 0.75 and visual.end > start - 0.75


def ensure_meme_visual(spec: Spec, decision: MemeDecision) -> Visual | None:
    """Inject a meme visual when the policy says yes and none exists yet."""
    if not decision.allowed:
        return None
    existing = [visual for visual in spec.visuals if visual.type == "meme"]
    if existing:
        return None
    if not spec.memes.tags:
        log.info("meme allowed but no tags to match; skipping insert")
        return None

    visual = Visual(
        id="meme-auto",
        type="meme",
        query=" ".join(spec.memes.tags[:3]),
        keywords=list(spec.memes.tags),
        start=decision.start,
        duration=decision.duration,
        position="fullscreen",
        motion="none",
        priority="low",
        notes=f"auto-inserted at {decision.beat}",
    )
    spec.visuals.append(visual)
    spec.visuals.sort(key=lambda item: (item.start, item.id))
    log.info(
        "inserted meme visual at %.2fs (%s)",
        decision.start,
        decision.beat,
        extra={"stage": "memes"},
    )
    return visual
