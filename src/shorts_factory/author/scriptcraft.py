"""Shorts script craft — hooks, structure checks, draft → scenario JSON."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FORBIDDEN_HOOK_RE = re.compile(
    r"^\s*(привет|здравствуй|всем\s+привет|сегодня\s+поговор|hey\s+guys|welcome\s+back)",
    re.IGNORECASE,
)
HOOK_MIN = 1.2
HOOK_IDEAL_MAX = 3.0
HOOK_HARD_MAX = 5.0


@dataclass
class HookIssue:
    level: str  # error | warning
    message: str


@dataclass
class ScriptDraft:
    """Lightweight author draft before full Spec parsing."""

    id: str
    title: str
    topic: str
    rubric: str = "AI"
    language: str = "ru"
    hook_text: str = ""
    hook_duration: float = 3.0
    body: list[dict[str, Any]] = field(default_factory=list)
    cta_text: str = "Подпишись, если хочешь такие разборы раньше ленты."
    research_path: str = ""
    avatar_on_ratio: float = 0.45


def validate_hook(text: str, duration: float) -> list[HookIssue]:
    issues: list[HookIssue] = []
    cleaned = (text or "").strip()
    if not cleaned:
        issues.append(HookIssue("error", "hook text is empty"))
        return issues
    if FORBIDDEN_HOOK_RE.search(cleaned):
        issues.append(HookIssue("error", "hook must not start with a greeting / soft intro"))
    if duration < HOOK_MIN:
        issues.append(HookIssue("error", f"hook duration {duration:.1f}s is too short (< {HOOK_MIN}s)"))
    if duration > HOOK_HARD_MAX:
        issues.append(HookIssue("error", f"hook duration {duration:.1f}s exceeds hard max {HOOK_HARD_MAX}s"))
    elif duration > HOOK_IDEAL_MAX:
        issues.append(HookIssue("warning", f"hook {duration:.1f}s is past the ideal 3s swipe window"))
    words = cleaned.split()
    if len(words) > 18:
        issues.append(HookIssue("warning", "hook is wordy; aim for one punchy sentence"))
    return issues


def validate_script_craft(
    *,
    hook_text: str,
    hook_duration: float,
    body_durations: list[float],
    avatar_segment_count: int,
    total_segments: int,
    visual_queries: list[str] | None = None,
) -> list[HookIssue]:
    issues = validate_hook(hook_text, hook_duration)
    if total_segments and avatar_segment_count / max(total_segments, 1) > 0.85:
        issues.append(
            HookIssue(
                "warning",
                "avatar is on almost the whole Short; aim ~40–50% on-camera with fullscreen payoffs",
            )
        )
    if visual_queries:
        lowered = [query.lower().strip() for query in visual_queries if query]
        if len(lowered) != len(set(lowered)):
            issues.append(HookIssue("warning", "duplicate visual queries — risk of repeating pictures"))
    body = sum(body_durations)
    if body + hook_duration > 70:
        issues.append(HookIssue("warning", "draft is long for Shorts retention; cut ~30%"))
    return issues


def draft_to_document(draft: ScriptDraft) -> dict[str, Any]:
    """Expand a draft into a scenario-shaped dict (not yet Spec-validated)."""
    cursor = float(draft.hook_duration)
    script: list[dict[str, Any]] = []
    for index, row in enumerate(draft.body, start=1):
        duration = float(row.get("duration") or 5.0)
        segment = {
            "id": str(row.get("id") or f"s{index}"),
            "text": str(row.get("text") or ""),
            "start": round(cursor, 3),
            "duration": duration,
        }
        if row.get("emphasis"):
            segment["emphasis"] = row["emphasis"]
        if row.get("on_camera"):
            segment["on_camera"] = True
        script.append(segment)
        cursor += duration

    on_camera_ids = ["hook"] + [segment["id"] for segment in script if segment.get("on_camera")]
    if len(on_camera_ids) <= 1 and script:
        # Default: first and last body beats on camera (~40%).
        on_camera_ids = ["hook", script[0]["id"], script[-1]["id"]]

    duration_target = round(cursor + 3.5, 1)
    return {
        "id": draft.id,
        "title": draft.title,
        "topic": draft.topic,
        "rubric": draft.rubric,
        "language": draft.language,
        "duration_target": duration_target,
        "hook": {
            "id": "hook",
            "text": draft.hook_text,
            "start": 0,
            "duration": draft.hook_duration,
            "emphasis": "high",
            "on_camera": True,
        },
        "script": [{k: v for k, v in segment.items() if k != "on_camera"} for segment in script],
        "voice_settings": {"provider": "elevenlabs", "model": "eleven_v3", "language": "ru"},
        "avatar": {
            "enabled": True,
            "provider": "external",
            "segments": on_camera_ids,
        },
        "cta": {"text": draft.cta_text, "start": round(cursor, 3), "duration": 3.2},
        "research": draft.research_path or f"jobs/{draft.id}/research.json",
        "constraints": {"require_vision_qa": True, "manual_review_ok": True},
    }
