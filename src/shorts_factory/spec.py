"""The scenario contract: parse, validate and normalise the incoming JSON.

The user authors the JSON elsewhere and drops it into the repository, so this
module is the project's front door. It never guesses silently: every applied
default and every suspicious value becomes a `SpecIssue`, and all problems are
collected in one pass so a single run reports the complete list.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .errors import SpecError

Level = Literal["error", "warning", "info"]

VISUAL_TYPES = frozenset(
    {"footage", "image", "motion_graphics", "infographic", "meme", "screen_record", "generated"}
)
POSITIONS = frozenset(
    {"fullscreen", "top", "bottom", "center", "pip", "left", "right", "background", "split"}
)
MOTIONS = frozenset({"none", "kenburns", "parallax", "zoom_in", "zoom_out", "pan_left", "pan_right"})
PRIORITIES = frozenset({"low", "normal", "high", "critical"})
CAPTION_STYLES = frozenset({"karaoke", "word_pop", "line", "none"})
AUDIO_FX_TYPES = frozenset(
    {
        "whoosh",
        "impact",
        "riser",
        "swoosh",
        "click",
        "pop",
        "glitch",
        "sub_drop",
        "transition",
        "ui",
        # Ring states from the brand book: a soft pulse under an emphasised
        # number, and the power-down/up tick as the ring leaves and returns.
        "thump",
        "power_down",
        "power_up",
    }
)

# Channel rubrics from the REDSHIFT brand book. Free-form strings are accepted
# too — the set is only for autocomplete and music/meme policy matching.
RUBRICS = frozenset(
    {
        "космос",
        "space",
        "it",
        "технологии",
        "tech",
        "ai",
        "наука",
        "science",
        "медицина",
        "medicine",
    }
)

# Shorts hard limits (YouTube caps Shorts at 180s; below 8s they underperform).
MIN_DURATION = 5.0
MAX_DURATION = 180.0
RECOMMENDED_MAX_DURATION = 60.0

# ElevenLabs v3 stability window recommended for punchy short-form delivery.
STABILITY_MIN = 40.0
STABILITY_MAX = 55.0

# Speech density band, in letters per second. Roughly 1.6–5 words/s in English
# and 1.3–4 words/s in Russian — the range that reads as energetic but clear.
SLOW_DENSITY = 7.5
FAST_DENSITY = 26.0

_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,63}$")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+\s*")


@dataclass(frozen=True)
class SpecIssue:
    """One validation finding, addressed by a JSON path such as ``visuals[2].start``."""

    path: str
    message: str
    level: Level = "error"

    def __str__(self) -> str:
        return f"{self.level.upper()} {self.path}: {self.message}"


class _Collector:
    """Accumulates issues while walking the document."""

    def __init__(self) -> None:
        self.issues: list[SpecIssue] = []

    def error(self, path: str, message: str) -> None:
        self.issues.append(SpecIssue(path, message, "error"))

    def warn(self, path: str, message: str) -> None:
        self.issues.append(SpecIssue(path, message, "warning"))

    def info(self, path: str, message: str) -> None:
        self.issues.append(SpecIssue(path, message, "info"))

    @property
    def has_errors(self) -> bool:
        return any(issue.level == "error" for issue in self.issues)


# --------------------------------------------------------------------------- #
# Field readers
# --------------------------------------------------------------------------- #


def _obj(value: Any, path: str, col: _Collector) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        col.error(path, f"expected an object, got {type(value).__name__}")
        return {}
    return value


def _string(
    data: dict[str, Any],
    key: str,
    path: str,
    col: _Collector,
    *,
    required: bool = False,
    default: str = "",
) -> str:
    value = data.get(key)
    if value is None:
        if required:
            col.error(f"{path}.{key}", "is required")
        return default
    if not isinstance(value, str):
        col.error(f"{path}.{key}", f"expected a string, got {type(value).__name__}")
        return default
    text = value.strip()
    if required and not text:
        col.error(f"{path}.{key}", "must not be empty")
    return text or default


def _number(
    data: dict[str, Any],
    key: str,
    path: str,
    col: _Collector,
    *,
    required: bool = False,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    value = data.get(key)
    if value is None:
        if required:
            col.error(f"{path}.{key}", "is required")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        col.error(f"{path}.{key}", f"expected a number, got {type(value).__name__}")
        return default
    number = float(value)
    if minimum is not None and number < minimum:
        col.error(f"{path}.{key}", f"must be >= {minimum} (got {number:g})")
        return default if default is not None else minimum
    if maximum is not None and number > maximum:
        col.error(f"{path}.{key}", f"must be <= {maximum} (got {number:g})")
        return default if default is not None else maximum
    return number


def _bool(data: dict[str, Any], key: str, path: str, col: _Collector, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        col.error(f"{path}.{key}", f"expected true/false, got {type(value).__name__}")
        return default
    return value


def _enum(
    data: dict[str, Any],
    key: str,
    path: str,
    col: _Collector,
    allowed: Iterable[str],
    default: str,
) -> str:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        col.error(f"{path}.{key}", f"expected a string, got {type(value).__name__}")
        return default
    normalised = value.strip().lower()
    allowed_set = set(allowed)
    if normalised not in allowed_set:
        col.error(f"{path}.{key}", f"unknown value {value!r}; allowed: {', '.join(sorted(allowed_set))}")
        return default
    return normalised


def _string_list(data: dict[str, Any], key: str, path: str, col: _Collector) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        col.error(f"{path}.{key}", f"expected a list of strings, got {type(value).__name__}")
        return []
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            col.error(f"{path}.{key}[{index}]", f"expected a string, got {type(item).__name__}")
            continue
        text = item.strip()
        if text:
            out.append(text)
    return out


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class ScriptSegment:
    """One spoken beat. ``start``/``duration`` are seconds on the master timeline."""

    id: str
    text: str
    start: float
    duration: float
    emphasis: str = "normal"
    on_camera: bool = False
    pause_after: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def word_count(self) -> int:
        return len([token for token in self.text.split() if any(ch.isalnum() for ch in token)])

    @property
    def words_per_second(self) -> float:
        return self.word_count / self.duration if self.duration > 0 else 0.0

    @property
    def chars_per_second(self) -> float:
        """Speech density, counting letters only.

        Words-per-second is language-dependent (Russian words run ~30% longer
        than English ones), so pacing is judged on characters instead.
        """
        letters = sum(1 for char in self.text if char.isalnum())
        return letters / self.duration if self.duration > 0 else 0.0


@dataclass
class VoiceSettings:
    provider: str = "elevenlabs"
    model: str = "eleven_v3"
    voice_id: str = ""
    stability: float = 47.0
    similarity_boost: float = 0.8
    style: float = 0.35
    speed: float = 1.0
    language: str = "ru"


@dataclass
class AvatarSettings:
    enabled: bool = True
    provider: str = "heygen"
    avatar_id: str = ""
    version: str = "avatar_5"
    position: str = "bottom_right"
    scale: float = 0.34
    background: str = "transparent"
    segments: list[str] = field(default_factory=list)
    #: When set (or when ``provider`` is ``external``), the pipeline loads
    #: ``jobs/<id>/avatar.mp4`` instead of calling the HeyGen API.
    external_path: str = ""


@dataclass
class Visual:
    """A slot on the visual track that the pipeline must fill with a real asset."""

    id: str
    type: str
    query: str
    keywords: list[str]
    start: float
    duration: float
    position: str = "fullscreen"
    motion: str = "none"
    priority: str = "normal"
    segment_ref: str | None = None
    allow_magnific: bool = True
    allow_generative: bool = True
    quality_floor: float = 0.0
    must_include: list[str] = field(default_factory=list)
    must_avoid: list[str] = field(default_factory=list)
    source_hint: str | None = None
    notes: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def all_queries(self) -> list[str]:
        """Primary query first, then the author's extra keywords, deduplicated."""
        seen: set[str] = set()
        out: list[str] = []
        for candidate in [self.query, *self.keywords]:
            key = candidate.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(candidate.strip())
        return out

    @property
    def is_hero(self) -> bool:
        return self.priority in {"high", "critical"}


@dataclass
class MusicSettings:
    track: str = ""
    volume: float = 0.18
    ducking: bool = True
    duck_to: float = 0.06
    fade_in: float = 0.5
    fade_out: float = 1.0
    start_at: float = 0.0


@dataclass
class AudioFx:
    type: str
    at: float
    intensity: float = 0.6
    duration: float = 0.8
    prompt: str = ""

    @property
    def id(self) -> str:
        return f"fx_{self.type}_{int(round(self.at * 100)):06d}"


@dataclass
class CaptionSettings:
    enabled: bool = True
    style: str = "karaoke"
    font: str = "Inter"
    size: int = 72
    position: str = "center_bottom"
    max_chars_per_line: int = 22
    highlight_color: str = "#FF2A3C"
    text_color: str = "#FFFFFF"
    stroke: bool = True
    uppercase: bool = False


@dataclass
class BrandElements:
    logo: str = ""
    logo_position: str = "top_left"
    lower_third: bool = False
    color_primary: str = "#FF2A3C"
    color_accent: str = "#37E4FF"
    color_background: str = "#0A0C10"
    font_family: str = "Inter"
    intro_sting: bool = False
    outro_card: bool = True
    safe_area: bool = True
    watermark_opacity: float = 0.55


@dataclass
class Cta:
    text: str = ""
    start: float = 0.0
    duration: float = 3.0
    style: str = "badge"
    url: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class MemeSettings:
    enabled: bool = False
    tags: list[str] = field(default_factory=list)
    max_count: int = 1
    max_duration: float = 1.5


@dataclass
class SfxPolicy:
    """Density caps for automatic sound design (brand book §7)."""

    max_effects: int | None = None  # None → derived from duration
    min_gap: float = 1.2
    type_cooldown: float = 4.0
    voice_guard: float = 0.4


@dataclass
class RingSettings:
    """Optional per-video overrides for the Pulse Ring."""

    enabled: bool = True
    anchor: str = ""  # empty → brandbook default
    diameter_ratio: float | None = None


@dataclass
class Constraints:
    magnific_token_budget: int | None = None
    allow_generative: bool = True
    require_vision_qa: bool = True
    min_visual_score: float = 0.62
    manual_review_ok: bool = True


@dataclass
class Spec:
    """A fully validated, normalised scenario."""

    id: str
    title: str
    duration_target: float
    language: str
    topic: str
    hook: ScriptSegment | None
    script: list[ScriptSegment]
    voice: VoiceSettings
    avatar: AvatarSettings
    visuals: list[Visual]
    music: MusicSettings
    audio_fx: list[AudioFx]
    captions: CaptionSettings
    brand: BrandElements
    cta: Cta | None
    memes: MemeSettings
    constraints: Constraints
    rubric: str = ""
    sfx_policy: SfxPolicy = field(default_factory=SfxPolicy)
    ring: RingSettings = field(default_factory=RingSettings)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- derived views ------------------------------------------------------

    @property
    def all_segments(self) -> list[ScriptSegment]:
        """Hook first, then the body, ordered by start time."""
        segments = ([self.hook] if self.hook else []) + list(self.script)
        return sorted(segments, key=lambda s: s.start)

    @property
    def narration_text(self) -> str:
        return " ".join(segment.text for segment in self.all_segments)

    @property
    def spoken_duration(self) -> float:
        segments = self.all_segments
        return max((s.end for s in segments), default=0.0)

    def segment_by_id(self, segment_id: str) -> ScriptSegment | None:
        for segment in self.all_segments:
            if segment.id == segment_id:
                return segment
        return None

    def segment_at(self, time: float) -> ScriptSegment | None:
        """The segment being spoken at ``time`` (used to give visuals context)."""
        for segment in self.all_segments:
            if segment.start <= time < segment.end:
                return segment
        return None

    def context_for(self, visual: Visual) -> str:
        """Narration text that overlaps a visual — the ground truth for QA."""
        if visual.segment_ref:
            explicit = self.segment_by_id(visual.segment_ref)
            if explicit:
                return explicit.text
        overlapping = [
            segment.text
            for segment in self.all_segments
            if segment.start < visual.end and visual.start < segment.end
        ]
        return " ".join(overlapping) or self.title

    def coverage(self) -> float:
        """Fraction of the target duration covered by at least one visual."""
        if self.duration_target <= 0:
            return 0.0
        spans = sorted((v.start, v.end) for v in self.visuals)
        covered = 0.0
        cursor = 0.0
        for start, end in spans:
            start = max(start, cursor)
            end = min(end, self.duration_target)
            if end > start:
                covered += end - start
                cursor = end
        return min(covered / self.duration_target, 1.0)

    def gaps(self) -> list[tuple[float, float]]:
        """Uncovered windows on the visual track, as ``(start, end)`` pairs."""
        spans = sorted((v.start, v.end) for v in self.visuals)
        gaps: list[tuple[float, float]] = []
        cursor = 0.0
        for start, end in spans:
            if start > cursor + 0.05:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < self.duration_target - 0.05:
            gaps.append((cursor, self.duration_target))
        return gaps


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _parse_segment(
    data: dict[str, Any],
    path: str,
    col: _Collector,
    *,
    fallback_id: str,
    cursor: float,
) -> ScriptSegment:
    text = _string(data, "text", path, col, required=True)
    start = _number(data, "start", path, col, minimum=0.0)
    duration = _number(data, "duration", path, col, minimum=0.05)
    if start is None:
        start = cursor
    if duration is None:
        # 2.6 words/second is a realistic upper-mid pace for energetic Shorts VO.
        duration = max(1.2, round(len(text.split()) / 2.6, 2))
        col.info(f"{path}.duration", f"missing, estimated {duration:g}s from the text")
    segment = ScriptSegment(
        id=_string(data, "id", path, col, default=fallback_id) or fallback_id,
        text=text,
        start=float(start),
        duration=float(duration),
        emphasis=_enum(data, "emphasis", path, col, {"low", "normal", "high"}, "normal"),
        on_camera=_bool(data, "on_camera", path, col, False),
        pause_after=float(_number(data, "pause_after", path, col, default=0.0, minimum=0.0) or 0.0),
    )
    return segment


def _parse_script(raw: Any, col: _Collector) -> list[ScriptSegment]:
    if raw is None:
        col.error("script", "is required")
        return []
    if isinstance(raw, str):
        # A plain string is accepted but split so downstream timing still works.
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(raw) if s.strip()]
        col.warn("script", "given as plain text; split into segments and auto-timed")
        segments: list[ScriptSegment] = []
        cursor = 0.0
        for index, sentence in enumerate(sentences):
            duration = max(1.2, round(len(sentence.split()) / 2.6, 2))
            segments.append(ScriptSegment(id=f"s{index + 1}", text=sentence, start=cursor, duration=duration))
            cursor += duration
        return segments
    if not isinstance(raw, list):
        col.error("script", f"expected a list of segments, got {type(raw).__name__}")
        return []

    segments = []
    cursor = 0.0
    for index, item in enumerate(raw):
        path = f"script[{index}]"
        if not isinstance(item, dict):
            col.error(path, f"expected an object, got {type(item).__name__}")
            continue
        segment = _parse_segment(item, path, col, fallback_id=f"s{index + 1}", cursor=cursor)
        segments.append(segment)
        cursor = segment.end + segment.pause_after
    return segments


def _parse_visual(data: dict[str, Any], path: str, col: _Collector, index: int) -> Visual | None:
    visual_type = _enum(data, "type", path, col, VISUAL_TYPES, "footage")
    query = _string(data, "query", path, col)
    keywords = _string_list(data, "keywords", path, col)
    if not query and keywords:
        query = keywords[0]
        col.info(f"{path}.query", "missing, using the first keyword")
    if not query:
        col.error(f"{path}.query", "a query or at least one keyword is required")
        return None

    start = _number(data, "start", path, col, required=True, minimum=0.0)
    duration = _number(data, "duration", path, col, required=True, minimum=0.2)
    if start is None or duration is None:
        return None

    return Visual(
        id=_string(data, "id", path, col, default=f"v{index + 1}") or f"v{index + 1}",
        type=visual_type,
        query=query,
        keywords=keywords,
        start=float(start),
        duration=float(duration),
        position=_enum(data, "position", path, col, POSITIONS, "fullscreen"),
        motion=_enum(data, "motion", path, col, MOTIONS, "none"),
        priority=_enum(data, "priority", path, col, PRIORITIES, "normal"),
        segment_ref=_string(data, "segment_ref", path, col) or None,
        allow_magnific=_bool(data, "allow_magnific", path, col, True),
        allow_generative=_bool(data, "allow_generative", path, col, True),
        quality_floor=float(
            _number(data, "quality_floor", path, col, default=0.0, minimum=0.0, maximum=1.0) or 0.0
        ),
        must_include=_string_list(data, "must_include", path, col),
        must_avoid=_string_list(data, "must_avoid", path, col),
        source_hint=_string(data, "source_hint", path, col) or None,
        notes=_string(data, "notes", path, col),
    )


def _parse_voice(data: dict[str, Any], col: _Collector, language: str) -> VoiceSettings:
    path = "voice_settings"
    stability = _number(data, "stability", path, col, default=47.0, minimum=0.0, maximum=100.0)
    stability = 47.0 if stability is None else stability
    if stability <= 1.0:  # accept the 0..1 convention and normalise it
        stability *= 100.0
    if not (STABILITY_MIN <= stability <= STABILITY_MAX):
        col.warn(
            f"{path}.stability",
            f"{stability:g} is outside the {STABILITY_MIN:g}–{STABILITY_MAX:g} window "
            "recommended for Shorts delivery",
        )
    return VoiceSettings(
        provider=_string(data, "provider", path, col, default="elevenlabs") or "elevenlabs",
        model=_string(data, "model", path, col, default="eleven_v3") or "eleven_v3",
        voice_id=_string(data, "voice_id", path, col),
        stability=stability,
        similarity_boost=float(
            _number(data, "similarity_boost", path, col, default=0.8, minimum=0.0, maximum=1.0) or 0.8
        ),
        style=float(_number(data, "style", path, col, default=0.35, minimum=0.0, maximum=1.0) or 0.35),
        speed=float(_number(data, "speed", path, col, default=1.0, minimum=0.5, maximum=2.0) or 1.0),
        language=_string(data, "language", path, col, default=language) or language,
    )


def _parse_avatar(data: dict[str, Any], col: _Collector) -> AvatarSettings:
    path = "avatar"
    enabled = _bool(data, "enabled", path, col, bool(data))
    provider = (_string(data, "provider", path, col, default="heygen") or "heygen").lower()
    avatar_id = _string(data, "avatar_id", path, col)
    external_path = _string(data, "external_path", path, col)
    if enabled and not avatar_id and provider != "external" and not external_path:
        # Brandbook may still fill avatar_id later; keep enabled so prepare/render
        # can pick up jobs/<id>/avatar.mp4 without a HeyGen call.
        col.warn(
            f"{path}.avatar_id",
            "missing; will use brandbook look_id or jobs/<id>/avatar.mp4 if present",
        )
    return AvatarSettings(
        enabled=enabled,
        provider=provider,
        avatar_id=avatar_id,
        version=_string(data, "version", path, col, default="avatar_5") or "avatar_5",
        position=_enum(
            data,
            "position",
            path,
            col,
            {"bottom_right", "bottom_left", "bottom_center", "center", "fullscreen", "top_right"},
            "bottom_right",
        ),
        scale=float(_number(data, "scale", path, col, default=0.34, minimum=0.1, maximum=1.0) or 0.34),
        background=_string(data, "background", path, col, default="transparent") or "transparent",
        segments=_string_list(data, "segments", path, col),
        external_path=external_path,
    )


def _parse_audio_fx(raw: Any, col: _Collector) -> list[AudioFx]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        col.error("audio_fx", f"expected a list, got {type(raw).__name__}")
        return []
    effects: list[AudioFx] = []
    for index, item in enumerate(raw):
        path = f"audio_fx[{index}]"
        if not isinstance(item, dict):
            col.error(path, f"expected an object, got {type(item).__name__}")
            continue
        at = _number(item, "at", path, col, required=True, minimum=0.0)
        if at is None:
            continue
        effects.append(
            AudioFx(
                type=_enum(item, "type", path, col, AUDIO_FX_TYPES, "transition"),
                at=float(at),
                intensity=float(
                    _number(item, "intensity", path, col, default=0.6, minimum=0.0, maximum=1.0) or 0.6
                ),
                duration=float(
                    _number(item, "duration", path, col, default=0.8, minimum=0.05, maximum=5.0) or 0.8
                ),
                prompt=_string(item, "prompt", path, col),
            )
        )
    return sorted(effects, key=lambda fx: fx.at)


def _cross_checks(spec: Spec, col: _Collector) -> None:
    """Timeline-level sanity checks that need the whole document."""
    if not spec.visuals:
        col.error("visuals", "at least one visual is required")

    seen_ids: set[str] = set()
    for index, visual in enumerate(spec.visuals):
        path = f"visuals[{index}]"
        if visual.id in seen_ids:
            col.error(f"{path}.id", f"duplicate visual id {visual.id!r}")
        seen_ids.add(visual.id)
        if visual.end > spec.duration_target + 0.5:
            col.error(
                f"{path}",
                f"ends at {visual.end:g}s, past duration_target {spec.duration_target:g}s",
            )
        if visual.segment_ref and spec.segment_by_id(visual.segment_ref) is None:
            col.error(f"{path}.segment_ref", f"unknown segment id {visual.segment_ref!r}")
        if visual.duration < 0.8:
            col.warn(f"{path}.duration", f"{visual.duration:g}s is too short to read on screen")

    segment_ids: set[str] = set()
    for segment in spec.all_segments:
        if segment.id in segment_ids:
            col.error("script", f"duplicate segment id {segment.id!r}")
        segment_ids.add(segment.id)

    for missing in [s for s in spec.avatar.segments if s not in segment_ids]:
        col.error("avatar.segments", f"unknown segment id {missing!r}")

    spoken = spec.spoken_duration
    if spoken > spec.duration_target + 1.0:
        col.error(
            "script",
            f"narration runs to {spoken:g}s but duration_target is {spec.duration_target:g}s",
        )
    elif spoken < spec.duration_target * 0.6:
        col.warn(
            "script",
            f"narration covers only {spoken:g}s of {spec.duration_target:g}s — expect dead air",
        )

    coverage = spec.coverage()
    if coverage < 0.85:
        col.warn(
            "visuals",
            f"cover {coverage * 100:.0f}% of the timeline; gaps: "
            + ", ".join(f"{a:g}–{b:g}s" for a, b in spec.gaps()),
        )

    if spec.cta and spec.cta.end > spec.duration_target + 0.5:
        col.error("cta", f"ends at {spec.cta.end:g}s, past duration_target")

    for index, fx in enumerate(spec.audio_fx):
        if fx.at > spec.duration_target:
            col.error(f"audio_fx[{index}].at", f"{fx.at:g}s is past duration_target")

    if spec.duration_target > RECOMMENDED_MAX_DURATION:
        col.warn(
            "duration_target",
            f"{spec.duration_target:g}s exceeds the {RECOMMENDED_MAX_DURATION:g}s sweet spot for Shorts",
        )

    # Pacing: Shorts live or die on density, so flag slow or breathless beats.
    for segment in spec.all_segments:
        density = segment.chars_per_second
        if density and density < SLOW_DENSITY:
            col.warn(
                f"script[{segment.id}]",
                f"slow pacing ({density:.1f} chars/s, {segment.words_per_second:.1f} words/s) "
                "— tighten the line or shorten the slot",
            )
        elif density > FAST_DENSITY:
            col.warn(
                f"script[{segment.id}]",
                f"breathless pacing ({density:.1f} chars/s) — give it room",
            )
        sentences = [s for s in _SENTENCE_SPLIT_RE.split(segment.text) if s.strip()]
        longest = max((len(s.split()) for s in sentences), default=0)
        if longest > 14:
            col.warn(
                f"script[{segment.id}]",
                f"longest sentence is {longest} words; Shorts read better under 14",
            )


def parse_spec(document: Any, *, source: str | None = None) -> tuple[Spec, list[SpecIssue]]:
    """Validate and normalise a decoded JSON document.

    Returns the spec together with every non-fatal issue. Raises `SpecError`
    when the document cannot be used at all.
    """
    col = _Collector()
    data = _obj(document, "$", col)
    if col.has_errors:
        raise SpecError(col.issues, source)

    spec_id = _string(data, "id", "$", col, required=True)
    if spec_id and not _SLUG_RE.match(spec_id):
        col.error("id", "must be 2–64 chars of letters, digits, '.', '_' or '-'")

    title = _string(data, "title", "$", col, required=True)
    language = _string(data, "language", "$", col, default="ru") or "ru"
    topic = _string(data, "topic", "$", col) or title
    rubric_raw = _string(data, "rubric", "$", col)
    rubric = rubric_raw.strip()
    if rubric and rubric.lower() not in RUBRICS and rubric not in RUBRICS:
        col.info("rubric", f"{rubric!r} is not a known REDSHIFT rubric; music/meme policies may miss it")
    duration_target = _number(
        data,
        "duration_target",
        "$",
        col,
        required=True,
        minimum=MIN_DURATION,
        maximum=MAX_DURATION,
    )

    hook_raw = data.get("hook")
    hook: ScriptSegment | None = None
    if isinstance(hook_raw, str):
        text = hook_raw.strip()
        if text:
            hook = ScriptSegment(id="hook", text=text, start=0.0, duration=max(1.2, len(text.split()) / 2.8))
    elif isinstance(hook_raw, dict):
        hook = _parse_segment(hook_raw, "hook", col, fallback_id="hook", cursor=0.0)
    elif hook_raw is not None:
        col.error("hook", f"expected a string or object, got {type(hook_raw).__name__}")

    script = _parse_script(data.get("script"), col)
    if hook and script and script[0].start < hook.end - 0.01:
        # The hook occupies the opening; shift the body behind it once.
        shift = hook.end - script[0].start
        for segment in script:
            segment.start += shift
        col.info("script", f"shifted by {shift:g}s so it starts after the hook")

    if not script and not hook:
        col.error("script", "no narration found")

    visuals_raw = data.get("visuals")
    visuals: list[Visual] = []
    if visuals_raw is None:
        col.error("visuals", "is required")
    elif not isinstance(visuals_raw, list):
        col.error("visuals", f"expected a list, got {type(visuals_raw).__name__}")
    else:
        for index, item in enumerate(visuals_raw):
            path = f"visuals[{index}]"
            if not isinstance(item, dict):
                col.error(path, f"expected an object, got {type(item).__name__}")
                continue
            visual = _parse_visual(item, path, col, index)
            if visual:
                visuals.append(visual)
    visuals.sort(key=lambda v: (v.start, v.id))

    music_data = _obj(data.get("music"), "music", col)
    music = MusicSettings(
        track=_string(music_data, "track", "music", col),
        volume=float(
            _number(music_data, "volume", "music", col, default=0.18, minimum=0.0, maximum=1.0) or 0.18
        ),
        ducking=_bool(music_data, "ducking", "music", col, True),
        duck_to=float(
            _number(music_data, "duck_to", "music", col, default=0.06, minimum=0.0, maximum=1.0) or 0.06
        ),
        fade_in=float(_number(music_data, "fade_in", "music", col, default=0.5, minimum=0.0) or 0.5),
        fade_out=float(_number(music_data, "fade_out", "music", col, default=1.0, minimum=0.0) or 1.0),
        start_at=float(_number(music_data, "start_at", "music", col, default=0.0, minimum=0.0) or 0.0),
    )

    captions_data = _obj(data.get("captions"), "captions", col)
    captions = CaptionSettings(
        enabled=_bool(captions_data, "enabled", "captions", col, True),
        style=_enum(captions_data, "style", "captions", col, CAPTION_STYLES, "karaoke"),
        font=_string(captions_data, "font", "captions", col, default="Inter") or "Inter",
        size=int(_number(captions_data, "size", "captions", col, default=72, minimum=24, maximum=160) or 72),
        position=_enum(
            captions_data,
            "position",
            "captions",
            col,
            {"center", "center_bottom", "bottom", "top", "upper_third", "lower_third"},
            "center_bottom",
        ),
        max_chars_per_line=int(
            _number(captions_data, "max_chars_per_line", "captions", col, default=22, minimum=10, maximum=48)
            or 22
        ),
        highlight_color=_string(captions_data, "highlight_color", "captions", col, default="#FF2A3C")
        or "#FF2A3C",
        text_color=_string(captions_data, "text_color", "captions", col, default="#FFFFFF") or "#FFFFFF",
        stroke=_bool(captions_data, "stroke", "captions", col, True),
        uppercase=_bool(captions_data, "uppercase", "captions", col, False),
    )

    brand_data = _obj(data.get("brand_elements"), "brand_elements", col)
    brand = BrandElements(
        logo=_string(brand_data, "logo", "brand_elements", col),
        logo_position=_enum(
            brand_data,
            "logo_position",
            "brand_elements",
            col,
            {"top_left", "top_right", "bottom_left", "bottom_right", "none"},
            "top_left",
        ),
        lower_third=_bool(brand_data, "lower_third", "brand_elements", col, False),
        color_primary=_string(brand_data, "color_primary", "brand_elements", col, default="#FF2A3C")
        or "#FF2A3C",
        color_accent=_string(brand_data, "color_accent", "brand_elements", col, default="#37E4FF")
        or "#37E4FF",
        color_background=_string(brand_data, "color_background", "brand_elements", col, default="#0A0C10")
        or "#0A0C10",
        font_family=_string(brand_data, "font_family", "brand_elements", col, default="Inter") or "Inter",
        intro_sting=_bool(brand_data, "intro_sting", "brand_elements", col, False),
        outro_card=_bool(brand_data, "outro_card", "brand_elements", col, True),
        safe_area=_bool(brand_data, "safe_area", "brand_elements", col, True),
        watermark_opacity=float(
            _number(
                brand_data, "watermark_opacity", "brand_elements", col, default=0.55, minimum=0.0, maximum=1.0
            )
            or 0.55
        ),
    )

    cta_data = data.get("cta")
    cta: Cta | None = None
    if isinstance(cta_data, dict):
        target = duration_target or MIN_DURATION
        cta = Cta(
            text=_string(cta_data, "text", "cta", col, required=True),
            start=float(
                _number(cta_data, "start", "cta", col, default=max(0.0, target - 4.0), minimum=0.0)
                or max(0.0, target - 4.0)
            ),
            duration=float(_number(cta_data, "duration", "cta", col, default=3.0, minimum=0.5) or 3.0),
            style=_enum(cta_data, "style", "cta", col, {"badge", "banner", "card", "text", "arrow"}, "badge"),
            url=_string(cta_data, "url", "cta", col),
        )
    elif isinstance(cta_data, str) and cta_data.strip():
        target = duration_target or MIN_DURATION
        cta = Cta(text=cta_data.strip(), start=max(0.0, target - 4.0), duration=3.0)
    elif cta_data is not None:
        col.error("cta", f"expected an object or string, got {type(cta_data).__name__}")

    memes_data = _obj(data.get("memes"), "memes", col)
    memes = MemeSettings(
        enabled=_bool(memes_data, "enabled", "memes", col, False),
        tags=_string_list(memes_data, "tags", "memes", col),
        max_count=int(_number(memes_data, "max_count", "memes", col, default=1, minimum=0, maximum=5) or 1),
        max_duration=float(
            _number(memes_data, "max_duration", "memes", col, default=1.5, minimum=0.2, maximum=4.0) or 1.5
        ),
    )
    if memes.enabled and not memes.tags:
        col.warn("memes.tags", "memes enabled without tags; nothing will be inserted")

    sfx_raw = data.get("sfx")
    sfx_data = _obj(sfx_raw if isinstance(sfx_raw, dict) else {}, "sfx", col)
    policy_data = _obj(sfx_data.get("policy") if isinstance(sfx_data, dict) else {}, "sfx.policy", col)
    max_fx = _number(policy_data, "max_effects", "sfx.policy", col, minimum=0.0, maximum=40.0)
    sfx_policy = SfxPolicy(
        max_effects=int(max_fx) if max_fx is not None else None,
        min_gap=float(
            _number(policy_data, "min_gap", "sfx.policy", col, default=1.2, minimum=0.2, maximum=10.0) or 1.2
        ),
        type_cooldown=float(
            _number(policy_data, "type_cooldown", "sfx.policy", col, default=4.0, minimum=0.0, maximum=30.0)
            or 4.0
        ),
        voice_guard=float(
            _number(policy_data, "voice_guard", "sfx.policy", col, default=0.4, minimum=0.0, maximum=2.0)
            or 0.4
        ),
    )

    ring_data = _obj(data.get("ring"), "ring", col)
    diameter = _number(ring_data, "diameter_ratio", "ring", col, minimum=0.1, maximum=0.8)
    anchor_raw = ring_data.get("anchor")
    anchor = ""
    if isinstance(anchor_raw, str) and anchor_raw.strip():
        anchor = _enum(
            ring_data,
            "anchor",
            "ring",
            col,
            {"bottom_right", "bottom_left", "bottom_center", "center"},
            "bottom_right",
        )
    elif anchor_raw is not None and not isinstance(anchor_raw, str):
        col.error("ring.anchor", f"expected a string, got {type(anchor_raw).__name__}")
    ring = RingSettings(
        enabled=_bool(ring_data, "enabled", "ring", col, True),
        anchor=anchor,
        diameter_ratio=float(diameter) if diameter is not None else None,
    )

    constraints_data = _obj(data.get("constraints"), "constraints", col)
    budget_value = _number(constraints_data, "magnific_token_budget", "constraints", col, minimum=0.0)
    constraints = Constraints(
        magnific_token_budget=int(budget_value) if budget_value is not None else None,
        allow_generative=_bool(constraints_data, "allow_generative", "constraints", col, True),
        require_vision_qa=_bool(constraints_data, "require_vision_qa", "constraints", col, True),
        min_visual_score=float(
            _number(
                constraints_data,
                "min_visual_score",
                "constraints",
                col,
                default=0.62,
                minimum=0.0,
                maximum=1.0,
            )
            or 0.62
        ),
        manual_review_ok=_bool(constraints_data, "manual_review_ok", "constraints", col, True),
    )

    if col.has_errors:
        raise SpecError(col.issues, source)

    spec = Spec(
        id=spec_id,
        title=title,
        duration_target=float(duration_target or MIN_DURATION),
        language=language,
        topic=topic,
        rubric=rubric,
        hook=hook,
        script=script,
        voice=_parse_voice(_obj(data.get("voice_settings"), "voice_settings", col), col, language),
        avatar=_parse_avatar(_obj(data.get("avatar"), "avatar", col), col),
        visuals=visuals,
        music=music,
        audio_fx=_parse_audio_fx(data.get("audio_fx"), col),
        captions=captions,
        brand=brand,
        cta=cta,
        memes=memes,
        sfx_policy=sfx_policy,
        ring=ring,
        constraints=constraints,
        raw=data,
    )

    _cross_checks(spec, col)
    if col.has_errors:
        raise SpecError(col.issues, source)

    return spec, col.issues


def load_spec(path: str | Path) -> tuple[Spec, list[SpecIssue]]:
    """Read a scenario JSON file from disk and validate it."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SpecError([SpecIssue("$", f"file not found: {file_path}")], str(file_path)) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(
            [SpecIssue("$", f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")],
            str(file_path),
        ) from exc
    return parse_spec(document, source=str(file_path))


def format_issues(issues: Sequence[SpecIssue]) -> str:
    return "\n".join(f"  {issue}" for issue in issues)
