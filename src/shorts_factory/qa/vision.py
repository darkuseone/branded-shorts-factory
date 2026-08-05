"""Level 2 — the strict gate: Grok Vision looks at the actual frames.

The native check only reads metadata, which lies. This stage extracts keyframes
from whatever is about to go into the render and asks a vision model the four
questions that matter: is it on topic, is anything distracting in frame, is the
quality good enough, and does it belong in *this* video.

Verdicts are `pass`, `replace` (find something else) or `manual` (a human
decides). Anything the model cannot answer becomes `manual`, never a silent
pass.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..config import Settings
from ..errors import ProviderError
from ..http import HttpClient
from ..logging_utils import get_logger
from ..media.download import LocalAsset
from ..media.ffmpeg import extract_keyframes
from ..spec import Visual

log = get_logger("vision")

DEFAULT_BASE = "https://api.x.ai/v1"
FRAMES_PER_ASSET = 3

Verdict = Literal["pass", "replace", "manual"]

SYSTEM_PROMPT = (
    "You are the final visual quality gate for a vertical short-form video. "
    "You are strict: a clip that is merely 'related' is not good enough, it must "
    "illustrate the narration. Reply with JSON only."
)

USER_TEMPLATE = """\
Narration spoken over this shot:
"{context}"

The shot is supposed to show: {query}
Requested visual type: {visual_type}. Slot length: {duration:.1f}s.
{constraints}

Judge the attached frame(s) from the SAME clip and answer as JSON:
{{
  "on_topic": 0.0-1.0,          // does it illustrate the narration?
  "distractors": ["..."],        // objects/text/watermarks that do not belong
  "quality": 0.0-1.0,            // sharpness, lighting, production value
  "coherent": true|false,        // does it fit a premium branded video?
  "verdict": "pass"|"replace"|"manual",
  "reason": "one short sentence"
}}
Use "replace" when something else would clearly serve better. Use "manual" only
when you genuinely cannot tell.
"""


@dataclass
class VisionVerdict:
    verdict: Verdict
    on_topic: float = 0.0
    quality: float = 0.0
    coherent: bool = True
    distractors: list[str] = field(default_factory=list)
    reason: str = ""
    frames_checked: int = 0
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def score(self) -> float:
        return round(0.65 * self.on_topic + 0.35 * self.quality, 3)

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "on_topic": round(self.on_topic, 3),
            "quality": round(self.quality, 3),
            "coherent": self.coherent,
            "distractors": self.distractors,
            "reason": self.reason,
            "frames_checked": self.frames_checked,
            "skipped": self.skipped,
        }


class GrokVisionGate:
    """Vision QA with a per-run call budget."""

    name = "grok_vision"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = os.environ.get("GROK_API_BASE", DEFAULT_BASE).rstrip("/")
        self.model = settings.grok_vision_model
        self.calls_used = 0
        self.call_budget = settings.budgets.grok_vision_calls
        key = settings.credentials.grok
        self.client = HttpClient(
            provider=self.name,
            timeout=max(settings.http_timeout, 90.0),
            min_interval=0.3,
            default_headers={"Authorization": f"Bearer {key}"} if key else {},
        )

    @property
    def is_available(self) -> bool:
        return bool(self.settings.credentials.grok) and not self.settings.offline

    def unavailable_reason(self) -> str:
        if self.settings.offline:
            return "offline mode"
        if not self.settings.credentials.grok:
            return "GROK_API_KEY not set"
        if self.calls_used >= self.call_budget:
            return f"vision call budget exhausted ({self.call_budget})"
        return ""

    # -- main entry point ---------------------------------------------------

    def check(self, asset: LocalAsset, visual: Visual, context: str) -> VisionVerdict:
        """Inspect an asset's frames. Never raises."""
        reason = self.unavailable_reason()
        if reason:
            log.info("%s: vision skipped (%s)", visual.id, reason, extra={"stage": "qa"})
            return VisionVerdict(verdict="manual", reason=f"not checked: {reason}", skipped=True)

        frames = extract_keyframes(
            asset.path,
            self.settings.paths.keyframes / visual.id,
            count=FRAMES_PER_ASSET,
            duration=asset.duration,
            ffmpeg=self.settings.ffmpeg_cmd,
            prefix=asset.candidate.fingerprint,
        )
        if not frames:
            return VisionVerdict(
                verdict="manual", reason="could not extract frames (is FFmpeg installed?)", skipped=True
            )

        try:
            payload = self._ask(frames, visual, context)
        except ProviderError as exc:
            log.warning("%s: vision request failed: %s", visual.id, exc, extra={"stage": "qa"})
            return VisionVerdict(verdict="manual", reason=f"vision request failed: {exc}", skipped=True)

        verdict = _parse(payload)
        verdict.frames_checked = len(frames)
        log.info(
            "%s: vision %s (on_topic %.2f, quality %.2f) — %s",
            visual.id,
            verdict.verdict,
            verdict.on_topic,
            verdict.quality,
            verdict.reason or "no reason given",
            extra={"stage": "qa"},
        )
        return verdict

    def _ask(self, frames: Sequence[Path], visual: Visual, context: str) -> str:
        constraints = []
        if visual.must_include:
            constraints.append("Must show: " + ", ".join(visual.must_include))
        if visual.must_avoid:
            constraints.append("Must not show: " + ", ".join(visual.must_avoid))

        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": USER_TEMPLATE.format(
                    context=context.strip()[:500] or "(no narration over this shot)",
                    query=visual.query,
                    visual_type=visual.type,
                    duration=visual.duration,
                    constraints="\n".join(constraints),
                ),
            }
        ]
        for frame in frames:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url(frame), "detail": "low"},
                }
            )

        self.calls_used += 1
        response = self.client.post_json(
            f"{self.base}/chat/completions",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.0,
                "max_tokens": 400,
            },
        )
        choices = (response or {}).get("choices") or []
        if not choices:
            raise ProviderError(self.name, "empty response")
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")


def _data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{encoded}"


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(content: str) -> VisionVerdict:
    """Extract the verdict JSON from a model reply, tolerating code fences."""
    match = _JSON_RE.search(content or "")
    if not match:
        return VisionVerdict(verdict="manual", reason="model reply was not JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return VisionVerdict(verdict="manual", reason="model reply was malformed JSON")
    if not isinstance(data, dict):
        return VisionVerdict(verdict="manual", reason="model reply was not an object")

    raw_verdict = str(data.get("verdict", "")).strip().lower()
    verdict: Verdict = raw_verdict if raw_verdict in {"pass", "replace", "manual"} else "manual"  # type: ignore[assignment]

    distractors = data.get("distractors")
    if isinstance(distractors, str):
        distractors = [distractors]
    elif not isinstance(distractors, list):
        distractors = []

    return VisionVerdict(
        verdict=verdict,
        on_topic=_clamp(data.get("on_topic")),
        quality=_clamp(data.get("quality")),
        coherent=bool(data.get("coherent", True)),
        distractors=[str(item) for item in distractors][:6],
        reason=str(data.get("reason", ""))[:240],
    )


def _clamp(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(number, 1.0))
