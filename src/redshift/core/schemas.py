"""Pydantic contracts for every artifact (§21).

Rules that apply to all of them:

* durations are seconds, float, three decimals;
* colours are hex strings, validated by regex;
* media references are either a cache sha256 or a path inside `runs/` — an
  external URL in `timeline.json` is a bug, because the renderer must not go to
  the network mid-render;
* every model carries `schema_version`; on a mismatch the stage fails with a
  clear message instead of guessing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import SchemaError

SCHEMA_VERSION = "1.0"

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")

Seconds = Annotated[float, Field(ge=0.0)]
Score10 = Annotated[float, Field(ge=0.0, le=10.0)]
Unit = Annotated[float, Field(ge=0.0, le=1.0)]

Pillar = Literal["space", "ai", "it_news", "science", "medicine_x_science"]
Section = Literal["HOOK", "SETUP", "BODY", "CLIMAX", "CLOSER"]
Emotion = Literal["awe", "tension", "irony", "shock", "calm", "urgency", "hope", "skepticism"]
Mode = Literal["MODE_A", "MODE_B", "MODE_C"]
CloserType = Literal["CLOSER_QUESTION", "CLOSER_PUNCH", "CLOSER_LOOP"]
VisualIntent = Literal[
    "COSMIC_HERO",
    "SIMULATION",
    "DATA_VIZ",
    "DIAGRAM",
    "ARTICLE",
    "UI_WALKTHROUGH",
    "ICON_LOGO",
    "LAB_TECH",
    "HUMAN_SCALE",
    "ABSTRACT_NEURAL",
    "ARCHIVE",
    "MEME",
    "PRESENTER_ONLY",
]
InfographicTemplate = Literal[
    "BIG_NUMBER",
    "BAR_RANK",
    "COMPARISON_AB",
    "TIMELINE",
    "ICON_FACT_LIST",
    "PROCESS_ARROW",
    "SCREEN_CARD",
]

T = TypeVar("T", bound="Artifact")


class Artifact(BaseModel):
    """Base for anything written to `runs/<run_id>/`."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: str = SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"artifact schema_version {value!r} != {SCHEMA_VERSION!r}; "
                "regenerate the artifact instead of guessing"
            )
        return value

    def save(self, path: Path) -> Path:
        """Atomic write, so a crash never leaves half an artifact behind."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    @classmethod
    def load(cls: type[T], path: Path) -> T:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SchemaError(cls.__name__, f"artifact not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise SchemaError(cls.__name__, f"{path} is not valid JSON: {exc}") from exc
        try:
            return cls.model_validate(raw)
        except Exception as exc:  # pydantic raises ValidationError
            raise SchemaError(cls.__name__, f"{path} does not match the schema: {exc}") from exc


def _hex_color(value: str) -> str:
    if not HEX_COLOR.match(value):
        raise ValueError(f"{value!r} is not a hex colour")
    return value


# --------------------------------------------------------------------------- #
# s00 topic.json (§5.3)
# --------------------------------------------------------------------------- #


class NumberClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    unit: str = ""
    claim: str = ""


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    title: str = ""
    authority: Unit = 0.5
    published_at: str = ""


class TopicScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: float = 0.0
    novelty: float = 0.0
    visual: float = 0.0
    wow: float = 0.0
    fit: float = 0.0
    buzz: float = 0.0
    auth: float = 0.0


class Topic(Artifact):
    topic_id: str
    pillar: Pillar
    headline_ru: str
    one_liner: str = ""
    entities: list[str] = Field(default_factory=list)
    numbers: list[NumberClaim] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    score: TopicScore = Field(default_factory=TopicScore)
    visual_precheck: dict[str, int] = Field(default_factory=dict)
    risk_flags: list[str] = Field(default_factory=list)
    suggested_closer: CloserType = "CLOSER_LOOP"


# --------------------------------------------------------------------------- #
# s01 research_pack.json (§6.2)
# --------------------------------------------------------------------------- #


class VerifiedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    claim: str
    value: float | None = None
    unit: str = ""
    sources: list[int] = Field(default_factory=list)
    confidence: Unit = 0.0
    quote: str = ""
    visualizable: bool = False

    @field_validator("quote")
    @classmethod
    def _quote_is_short(cls, value: str) -> str:
        # §5.4: a quote on screen is 12 words maximum, for fair-use reasons.
        if len(value.split()) > 12:
            raise ValueError("quote must be 12 words or fewer")
        return value


class VisualHook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    what: str
    where: str = ""
    query_en: str = ""
    priority: int = 2


class ArticleScreenshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    domain: str
    date: str = ""
    headline: str = ""


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str
    simple: str


class ResearchPack(Artifact):
    topic_id: str
    verified_claims: list[VerifiedClaim] = Field(default_factory=list)
    unverified: list[VerifiedClaim] = Field(default_factory=list)
    visual_hooks: list[VisualHook] = Field(default_factory=list)
    article_screenshots: list[ArticleScreenshot] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)

    def claim_ids(self) -> set[str]:
        return {claim.id for claim in self.verified_claims}


# --------------------------------------------------------------------------- #
# s02 script.json (§7.4)
# --------------------------------------------------------------------------- #


class Beat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    section: Section
    text: str
    claim_ids: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    emotion: Emotion = "calm"
    visual_intent: VisualIntent = "ABSTRACT_NEURAL"
    emphasis_words: list[str] = Field(default_factory=list)
    meme_slot: bool = False
    must_show: str = ""
    duration_hint_s: Seconds = 0.0

    @property
    def word_count(self) -> int:
        return len([w for w in self.text.split() if any(c.isalnum() for c in w)])


class Script(Artifact):
    topic_id: str
    closer_type: CloserType
    total_words: int = 0
    beats: list[Beat]
    cta_text: str = ""
    title_ru: str = ""
    description_ru: str = ""
    hashtags: list[str] = Field(default_factory=list)
    pinned_comment: str = ""

    @model_validator(mode="after")
    def _has_a_closer(self) -> Script:
        if not self.beats:
            raise ValueError("script has no beats")
        if not any(beat.section == "CLOSER" for beat in self.beats):
            raise ValueError("script has no CLOSER beat")
        return self

    @property
    def full_text(self) -> str:
        return " ".join(beat.text for beat in self.beats)


# --------------------------------------------------------------------------- #
# s03 shotlist.json (§8.4)
# --------------------------------------------------------------------------- #


class ShotQueries(BaseModel):
    """Three levels of search intent (§9.2)."""

    model_config = ConfigDict(extra="forbid")
    literal: list[str] = Field(default_factory=list)
    conceptual: list[str] = Field(default_factory=list)
    stock_en: list[str] = Field(default_factory=list)

    def flat(self) -> list[str]:
        seen: dict[str, None] = {}
        for query in [*self.literal, *self.conceptual, *self.stock_en]:
            cleaned = " ".join(query.split())
            if cleaned:
                seen.setdefault(cleaned, None)
        return list(seen)


class TextOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    words: list[str] = Field(default_factory=list)
    style: str = "karaoke"


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    beat_id: str
    t_start: Seconds
    t_end: Seconds
    mode: Mode
    visual_intent: VisualIntent
    entity: str = ""
    must_show: str = ""
    queries: ShotQueries = Field(default_factory=ShotQueries)
    motion: str = "none"
    transition_in: str = "cut"
    sfx: list[str] = Field(default_factory=list)
    text_overlay: TextOverlay = Field(default_factory=TextOverlay)
    priority: int = Field(default=2, ge=1, le=3)
    fallback_intent: VisualIntent = "ABSTRACT_NEURAL"
    is_burst: bool = False

    @property
    def duration(self) -> float:
        return round(self.t_end - self.t_start, 3)

    @model_validator(mode="after")
    def _positive_duration(self) -> Shot:
        if self.t_end <= self.t_start:
            raise ValueError(f"shot {self.id}: t_end must be after t_start")
        return self


class Burst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: Seconds
    count: int
    shot_ids: list[str] = Field(default_factory=list)


class Shotlist(Artifact):
    fps: int = 30
    width: int = 1080
    height: int = 1920
    total_duration_s: Seconds = 0.0
    shots: list[Shot] = Field(default_factory=list)
    bursts: list[Burst] = Field(default_factory=list)
    mode_budget: dict[str, float] = Field(default_factory=dict)

    def shot_by_id(self, shot_id: str) -> Shot | None:
        return next((shot for shot in self.shots if shot.id == shot_id), None)

    def presenter_share(self) -> float:
        if self.total_duration_s <= 0:
            return 0.0
        presenter = sum(s.duration for s in self.shots if s.mode in {"MODE_A", "MODE_C"})
        return presenter / self.total_duration_s

    def median_shot_s(self) -> float:
        durations = sorted(shot.duration for shot in self.shots)
        if not durations:
            return 0.0
        middle = len(durations) // 2
        if len(durations) % 2:
            return durations[middle]
        return (durations[middle - 1] + durations[middle]) / 2


# --------------------------------------------------------------------------- #
# s04 candidates.json (§9.7)
# --------------------------------------------------------------------------- #


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str
    source: str
    url: str = ""
    license: str = ""
    author: str = ""
    w: int = 0
    h: int = 0
    duration: Seconds = 0.0
    motion: float = 0.0
    sharpness: float = 0.0
    phash: str = ""
    faces: int = 0
    text: bool = False
    watermark_suspect: bool = False
    query_used: str = ""
    l0_score: float = 0.0
    l0_flags: list[str] = Field(default_factory=list)
    ladder_step: int = 0


class ShotCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str
    candidates: list[Candidate] = Field(default_factory=list)


class Candidates(Artifact):
    round: int = 1
    shots: list[ShotCandidates] = Field(default_factory=list)

    def for_shot(self, shot_id: str) -> list[Candidate]:
        entry = next((s for s in self.shots if s.shot_id == shot_id), None)
        return entry.candidates if entry else []


# --------------------------------------------------------------------------- #
# s05 critic_report.json (§10.5)
# --------------------------------------------------------------------------- #


class RejectedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str
    reason: str = ""
    score: float = 0.0


class ShotVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str
    winner: str | None = None
    score: float = 0.0
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    needs_refill: bool = False
    better_queries: list[str] = Field(default_factory=list)
    reason: str = ""


class SequenceNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str
    issue: str
    suggestion: str = ""


class CriticStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    l0_rejected: int = 0
    l1_evaluated: int = 0
    l1_rejected: int = 0
    grok_vision_calls: int = 0
    gemini_vision_calls: int = 0


class CriticReport(Artifact):
    run_id: str = ""
    pass_number: int = Field(default=1, alias="pass")
    shots: list[ShotVerdict] = Field(default_factory=list)
    sequence_notes: list[SequenceNote] = Field(default_factory=list)
    stats: CriticStats = Field(default_factory=CriticStats)
    palette_ok: bool = True
    hook_score: Score10 = 0.0

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def refill_needed(self) -> list[str]:
        return [verdict.shot_id for verdict in self.shots if verdict.needs_refill]


# --------------------------------------------------------------------------- #
# s07 memes_plan.json (§12.6)
# --------------------------------------------------------------------------- #


class MemeAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meme_db: float = -8.0
    vo_duck: float = -6.0
    music_duck: float = -4.0


class MemeInsertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beat_id: str
    meme_id: str
    t_start: Seconds
    dur: Seconds
    placement: Literal["before_phrase", "on_phrase", "after_phrase"] = "after_phrase"
    layout: Literal["FULL", "PIP"] = "FULL"
    fit_score: Score10 = 0.0
    reason: str = ""
    audio: MemeAudio = Field(default_factory=MemeAudio)


class SkippedMeme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beat_id: str
    reason: str


class MemesPlan(Artifact):
    insertions: list[MemeInsertion] = Field(default_factory=list)
    skipped: list[SkippedMeme] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# s08 infographics.json (§13.4)
# --------------------------------------------------------------------------- #


class InfographicItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = ""
    value: str = ""
    icon: str = ""
    note: str = ""


class InfographicSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str
    template: InfographicTemplate
    title: str = ""
    items: list[InfographicItem] = Field(default_factory=list)
    source: str = ""
    accent_word: str = ""
    icons: list[str] = Field(default_factory=list)
    duration_s: Seconds = 2.4
    file: str = ""

    @model_validator(mode="after")
    def _shorts_readability(self) -> InfographicSpec:
        # §13.3: more than five units on one card cannot be read in time.
        if len(self.items) > 5:
            raise ValueError(f"{self.template} card has {len(self.items)} items; maximum is 5")
        return self


class Infographics(Artifact):
    cards: list[InfographicSpec] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# s10 timeline.json (§15.3)
# --------------------------------------------------------------------------- #


class TimelineMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    w: int = 1080
    h: int = 1920
    fps: int = 30
    duration: Seconds = 0.0
    run_id: str = ""


class Clip(BaseModel):
    """One item on one layer. `src` is a local path or a cache reference."""

    model_config = ConfigDict(extra="forbid")
    t: Seconds
    d: Seconds = 0.0
    src: str = ""
    text: str = ""
    opacity: Unit = 1.0
    fit: str = "cover"
    motion: dict[str, Any] = Field(default_factory=dict)
    crop: str = ""
    mask: str = ""
    fx: list[str] = Field(default_factory=list)
    style: str = ""
    emph: bool = False
    gain: float = 0.0
    duck: dict[str, Any] = Field(default_factory=dict)
    blend: str = ""
    anchor: str = ""
    lut: str = ""
    grain: float = 0.0

    @field_validator("src")
    @classmethod
    def _no_external_urls(cls, value: str) -> str:
        # §21: the renderer must never fetch anything mid-render.
        if value.startswith(("http://", "https://")):
            raise ValueError("timeline may not reference external URLs; copy to runs/ first")
        return value


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    t: Seconds
    type: Literal["cut", "particle_dissolve", "soft_warp", "holographic_flicker", "morph"]
    d: Seconds = 0.28


class Flash(BaseModel):
    model_config = ConfigDict(extra="forbid")
    t: Seconds
    color: str = "#E11D48"
    opacity: Unit = 0.45
    frames: int = Field(default=2, ge=1, le=2)

    @field_validator("color")
    @classmethod
    def _valid(cls, value: str) -> str:
        return _hex_color(value)


class Timeline(Artifact):
    meta: TimelineMeta = Field(default_factory=TimelineMeta)
    tracks: dict[str, list[Clip]] = Field(default_factory=dict)
    transitions: list[Transition] = Field(default_factory=list)
    flashes: list[Flash] = Field(default_factory=list)

    LAYERS: ClassVar[tuple[str, ...]] = (
        "L0_backplate",
        "L1_footage",
        "L2_footage_fx",
        "L3_infographic",
        "L4_avatar",
        "L5_hud",
        "L6_subs",
        "L7_brand",
        "A0_music",
        "A1_vo",
        "A2_sfx",
    )

    @model_validator(mode="after")
    def _known_layers(self) -> Timeline:
        unknown = set(self.tracks) - set(self.LAYERS)
        if unknown:
            raise ValueError(f"unknown timeline layers: {sorted(unknown)}")
        return self

    def layer(self, name: str) -> list[Clip]:
        return self.tracks.setdefault(name, [])


# --------------------------------------------------------------------------- #
# s12 qc_report.json (§17)
# --------------------------------------------------------------------------- #


class QCCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    status: Literal["pass", "warn", "fail", "skip"]
    message: str = ""
    blocking: bool = False


class QCReport(Artifact):
    run_id: str = ""
    checks: list[QCCheck] = Field(default_factory=list)

    @property
    def failed(self) -> list[QCCheck]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def warnings(self) -> list[QCCheck]:
        return [check for check in self.checks if check.status == "warn"]

    @property
    def blocked(self) -> bool:
        return any(check.status == "fail" and check.blocking for check in self.checks)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        parts = [f"{count} {status}" for status, count in sorted(counts.items())]
        return ", ".join(parts) or "no checks run"
