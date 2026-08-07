"""Level 2 vision gate — Gemini first, Grok as paid fallback.

Asks whether the frames illustrate the *exact* narration line, not merely a
related vibe. Strict context match is the whole point of this gate.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import ProviderError
from ..http import HttpClient
from ..logging_utils import get_logger
from ..media.download import LocalAsset
from ..media.ffmpeg import extract_keyframes
from ..spec import Visual
from .vision import SYSTEM_PROMPT, USER_TEMPLATE, VisionVerdict

log = get_logger("gemini_vision")

DEFAULT_MODEL = "gemini-2.0-flash"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class VisionGateResult:
    provider: str
    verdict: VisionVerdict


class GeminiVisionGate:
    """Gemini multimodal JSON gate. Missing key → unavailable, never silent pass."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = os.environ.get("GEMINI_VISION_MODEL", DEFAULT_MODEL)
        self.http = HttpClient(provider="gemini", timeout=60.0, min_interval=0.35)
        self._key = getattr(settings.credentials, "gemini", None)

    @property
    def is_available(self) -> bool:
        return bool(self._key)

    def unavailable_reason(self) -> str:
        return "" if self._key else "GEMINI_API_KEY / GOOGLE_API_KEY not set"

    def check(
        self,
        asset: LocalAsset,
        visual: Visual,
        context: str,
        *,
        budget_ok: bool = True,
    ) -> VisionVerdict:
        if not self.is_available:
            return VisionVerdict(
                verdict="manual",
                reason=self.unavailable_reason(),
                skipped=True,
            )
        if not budget_ok:
            return VisionVerdict(verdict="manual", reason="vision budget exhausted", skipped=True)

        frames = extract_keyframes(
            asset.path,
            self.settings.paths.workdir / "vision_frames" / visual.id,
            count=3,
            duration=getattr(asset, "duration", None),
            ffmpeg=self.settings.ffmpeg_cmd,
            prefix=getattr(getattr(asset, "candidate", None), "fingerprint", None) or "frame",
        )
        if not frames:
            return VisionVerdict(verdict="manual", reason="could not extract keyframes", skipped=True)

        prompt = USER_TEMPLATE.format(
            context=(context or visual.query or "")[:500],
            query=visual.query or visual.keywords,
            visual_type=visual.type,
            duration=visual.duration,
            constraints=_constraints(visual),
        )
        try:
            payload = self._ask(prompt, frames)
            return _parse(payload, frames_checked=len(frames))
        except Exception as exc:  # noqa: BLE001
            log.warning("gemini vision failed: %s", exc)
            return VisionVerdict(verdict="manual", reason=f"gemini error: {exc}", skipped=True)

    def _ask(self, prompt: str, frames: Sequence[Path]) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"text": f"{SYSTEM_PROMPT}\n\n{prompt}"}]
        for frame in frames:
            mime = "image/jpeg" if frame.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(frame.read_bytes()).decode("ascii"),
                    }
                }
            )
        url = f"{GEMINI_BASE}/models/{self.model}:generateContent"
        response = self.http.request(
            "POST",
            url,
            params={"key": self._key},
            json_body={
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
            },
        )
        data = response.json() or {}
        text = _extract_text(data)
        if not text:
            raise ProviderError("gemini", "empty vision response")
        return _loads_json(text)


class CompositeVisionGate:
    """Prefer Gemini; fall back to Grok when Gemini is missing or skips."""

    def __init__(self, settings: Settings, *, grok: Any | None = None):
        from .vision import GrokVisionGate

        self.settings = settings
        self.gemini = GeminiVisionGate(settings)
        self.grok = grok or GrokVisionGate(settings)
        self.last_provider = ""

    @property
    def is_available(self) -> bool:
        return self.gemini.is_available or self.grok.is_available

    def unavailable_reason(self) -> str:
        if self.gemini.is_available:
            return ""
        if self.grok.is_available:
            return "gemini missing; using grok fallback"
        return "GEMINI_API_KEY and GROK_API_KEY both unset"

    def check(
        self,
        asset: LocalAsset,
        visual: Visual,
        context: str,
        *,
        budget_ok: bool = True,
    ) -> VisionVerdict:
        if self.gemini.is_available:
            verdict = self.gemini.check(asset, visual, context, budget_ok=budget_ok)
            self.last_provider = "gemini"
            if not verdict.skipped or verdict.verdict != "manual":
                return verdict
            # Hard API failure / skip → try Grok once.
            if self.grok.is_available and "not set" not in (verdict.reason or ""):
                self.last_provider = "grok"
                return self.grok.check(asset, visual, context)
            return verdict

        if self.grok.is_available:
            self.last_provider = "grok"
            return self.grok.check(asset, visual, context)

        self.last_provider = "none"
        return VisionVerdict(verdict="manual", reason=self.unavailable_reason(), skipped=True)


def _constraints(visual: Visual) -> str:
    bits = []
    if visual.must_include:
        bits.append("Must include: " + ", ".join(visual.must_include))
    if visual.must_avoid:
        bits.append("Must avoid: " + ", ".join(visual.must_avoid))
    return "\n".join(bits)


def _extract_text(data: dict[str, Any]) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(str(part.get("text") or "") for part in parts)
    except (KeyError, IndexError, TypeError):
        return ""


def _loads_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def _parse(payload: dict[str, Any], *, frames_checked: int) -> VisionVerdict:
    verdict_raw = str(payload.get("verdict") or "manual").lower()
    if verdict_raw not in {"pass", "replace", "manual"}:
        verdict_raw = "manual"
    return VisionVerdict(
        verdict=verdict_raw,  # type: ignore[arg-type]
        on_topic=float(payload.get("on_topic") or 0.0),
        quality=float(payload.get("quality") or 0.0),
        coherent=bool(payload.get("coherent", True)),
        distractors=[str(item) for item in (payload.get("distractors") or [])],
        reason=str(payload.get("reason") or ""),
        frames_checked=frames_checked,
        skipped=False,
    )
