"""Stage 05 — Critic. Two layers (§10).

**L0** is deterministic and free. It reads the measurements `core.media` already
computed and throws away 50-70% of candidates before a single vision token is
spent (§19.E2). Hard rejects are technical facts — too small, watermarked, blurry,
already used in this video. Soft penalties are taste, expressed as a score.

**L1** is the vision model, and it never sees one candidate at a time: nine tiles
go up as one contact sheet, so nine judgements cost one call (§19.E1). This
module builds the sheet and parses the verdict; the call itself belongs to a
client, because nothing in `pipeline/` may touch a network.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.log import get_logger
from ..core.media import contact_sheet, hamming
from ..core.retry import parse_json_strict
from ..core.schemas import (
    Candidate,
    CriticReport,
    CriticStats,
    RejectedCandidate,
    Shot,
    Shotlist,
    ShotVerdict,
)

log = get_logger("s05")

STAGE = "s05_critic"

#: Short keys in the model's reply (§19.E6) → the names we use internally.
CRITERIA = {"r": "relevance", "f": "factual_fit", "b": "brand_fit", "w": "wow", "s": "safety"}


@dataclass(frozen=True)
class L0Config:
    min_short_side_video: int
    min_short_side_photo: int
    min_sharpness: float
    static_motion_threshold: float
    acid_share_max: float
    phash_dupe_distance: int
    penalties: dict[str, float]

    @classmethod
    def from_config(cls, config: Config) -> L0Config:
        return cls(
            min_short_side_video=int(config.critic("l0.hard_reject.min_short_side_video_px")),
            min_short_side_photo=int(config.critic("l0.hard_reject.min_short_side_photo_px")),
            min_sharpness=float(config.critic("l0.hard_reject.min_sharpness")),
            static_motion_threshold=float(config.critic("l0.static_motion_threshold")),
            acid_share_max=float(config.critic("l0.acid_share_max")),
            phash_dupe_distance=int(config.critic("l0.phash_dupe_distance")),
            penalties=dict(config.critic("l0.penalties")),
        )


@dataclass
class L0Result:
    candidate: Candidate
    passed: bool
    score: float
    flags: list[str] = field(default_factory=list)

    @property
    def rejected_because(self) -> str:
        return "; ".join(self.flags) or "below the L0 bar"


#: Licences that clear commercial use without further checking. Internet Archive
#: items without an explicit right are the classic strike risk (§27 B1).
COMMERCIAL_LICENSES = (
    "public-domain",
    "public domain",
    "pd",
    "cc0",
    "cc-by",
    "cc by",
    "pexels",
    "pixabay",
    "nasa",
    "magnific",
    "generated",
)


def check_l0(
    candidate: Candidate,
    shot: Shot,
    config: Config,
    *,
    chosen_phashes: Sequence[str] = (),
    history_phashes: Sequence[str] = (),
) -> L0Result:
    """Judge one candidate on metadata alone. Costs nothing, runs in microseconds."""
    rules = L0Config.from_config(config)
    flags: list[str] = []
    score = 10.0

    is_video = candidate.duration > 0.05
    floor = rules.min_short_side_video if is_video else rules.min_short_side_photo
    short_side = min(candidate.w, candidate.h) if candidate.w and candidate.h else 0

    # -- hard rejects: technical facts, not opinions ------------------------
    if short_side and short_side < floor:
        flags.append(f"short side {short_side}px below {floor}px")
    if candidate.watermark_suspect:
        flags.append("watermark suspected")
    if candidate.sharpness and candidate.sharpness < rules.min_sharpness:
        flags.append(f"sharpness {candidate.sharpness:.0f} below {rules.min_sharpness:.0f}")
    if not _license_allows_commercial(candidate.license):
        flags.append(f"licence {candidate.license!r} does not clear commercial use")
    if is_video and candidate.duration + 0.05 < shot.duration and not _loopable(candidate):
        flags.append(f"{candidate.duration:.1f}s clip cannot fill a {shot.duration:.1f}s shot")
    if _too_similar(candidate.phash, chosen_phashes, rules.phash_dupe_distance):
        flags.append("near-duplicate of a shot already chosen in this video")

    if flags:
        return L0Result(candidate, passed=False, score=0.0, flags=flags)

    # -- soft penalties: taste, expressed as a number -----------------------
    notes: list[str] = []
    if shot.mode == "MODE_B" and candidate.motion < rules.static_motion_threshold:
        score += rules.penalties["static_in_mode_b"]
        notes.append("static frame in a dynamic mode")
    if _acid(candidate) > rules.acid_share_max:
        score += rules.penalties["acid_colors"]
        notes.append("acid palette, outside the brandbook")
    if _off_aspect(candidate):
        score += rules.penalties["off_aspect"]
        notes.append("aspect far from 9:16")
    if candidate.faces and shot.visual_intent not in {"HUMAN_SCALE", "ARCHIVE", "LAB_TECH"}:
        score += rules.penalties["unwanted_faces"]
        notes.append("faces where the shot does not call for them")
    if _too_similar(candidate.phash, history_phashes, rules.phash_dupe_distance):
        score += rules.penalties["used_in_history"]
        notes.append("used in a recent video")

    return L0Result(candidate, passed=True, score=max(0.0, min(score, 10.0)), flags=notes)


def filter_l0(
    candidates: Iterable[Candidate],
    shot: Shot,
    config: Config,
    *,
    chosen_phashes: Sequence[str] = (),
    history_phashes: Sequence[str] = (),
) -> tuple[list[L0Result], list[L0Result]]:
    """Split a shot's candidates into survivors (best first) and rejects."""
    survivors: list[L0Result] = []
    rejects: list[L0Result] = []
    for candidate in candidates:
        result = check_l0(
            candidate,
            shot,
            config,
            chosen_phashes=chosen_phashes,
            history_phashes=history_phashes,
        )
        (survivors if result.passed else rejects).append(result)
    survivors.sort(key=lambda item: (-item.score, item.candidate.sha256))
    log.debug(
        "%s: L0 kept %d of %d",
        shot.id,
        len(survivors),
        len(survivors) + len(rejects),
        extra={"stage": STAGE},
    )
    return survivors, rejects


# --------------------------------------------------------------------------- #
# L1 — contact sheet in, verdicts out
# --------------------------------------------------------------------------- #


def build_contact_sheet(
    paths: Sequence[Path], destination: Path, config: Config, ffmpeg: str = "ffmpeg"
) -> Path | None:
    """Nine candidates on one numbered sheet — the project's biggest saving."""
    grid = tuple(config.critic("contact_sheet.grid"))
    tile = tuple(config.critic("contact_sheet.tile"))
    return contact_sheet(
        list(paths),
        destination,
        grid=(int(grid[0]), int(grid[1])),
        tile=(int(tile[0]), int(tile[1])),
        jpeg_q=int(config.critic("contact_sheet.jpeg_q")),
        frame_at=float(config.critic("contact_sheet.video_frame_at")),
        ffmpeg=ffmpeg,
    )


@dataclass
class TileVerdict:
    index: int
    scores: dict[str, float]
    keep: bool
    why: str = ""
    better_query: str | None = None

    def weighted(self, weights: dict[str, float]) -> float:
        return round(sum(weights[name] * self.scores.get(name, 0.0) for name in weights), 3)


def parse_l1(reply: str, config: Config) -> list[TileVerdict]:
    """Read the model's JSON array. Anything malformed is dropped, not guessed."""
    try:
        payload = parse_json_strict(reply)
    except ValueError:
        log.warning("L1 reply was not JSON; treating the sheet as unjudged", extra={"stage": STAGE})
        return []
    if not isinstance(payload, list):
        return []

    verdicts: list[TileVerdict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("i", 0))
        except (TypeError, ValueError):
            continue
        scores = {name: _clamp10(entry.get(short, entry.get(name, 0))) for short, name in CRITERIA.items()}
        query = entry.get("q")
        verdicts.append(
            TileVerdict(
                index=index,
                scores=scores,
                keep=str(entry.get("v", entry.get("verdict", ""))).lower() == "keep",
                why=str(entry.get("why", entry.get("reason", "")))[:200],
                better_query=str(query) if isinstance(query, str) and query.strip() else None,
            )
        )
    return verdicts


def accept_threshold(shot: Shot, config: Config, *, fallback: bool = False) -> float:
    """Priority-1 shots are held to a higher bar; fallbacks to a lower one."""
    if fallback:
        return float(config.critic("accept_threshold_fallback"))
    if shot.priority == 1:
        return float(config.critic("accept_threshold_priority1"))
    return float(config.critic("accept_threshold"))


def needs_arbitration(verdict: TileVerdict, shot: Shot, config: Config) -> bool:
    """Whether Gemini is worth spending on this one (§10.3).

    Only three cases justify a second opinion: a grey-zone score on a hero
    shot, a shaky factual fit where a named entity is on screen, and the hook.
    Showing Hubble while saying JWST is the most expensive mistake this channel
    can make, so that case buys a second look.
    """
    weights = dict(config.critic("weights"))
    score = verdict.weighted(weights)
    low, high = (float(value) for value in config.critic("gray_zone"))

    if shot.priority == 1 and low <= score <= high:
        return True
    if shot.entity and verdict.scores.get("factual_fit", 10.0) <= 7.0:
        return True
    return shot.t_start < 2.0


def combine_opinions(grok: float, gemini: float, config: Config) -> float | None:
    """Consensus rule (§10.3). A contested candidate is not a good candidate."""
    if abs(grok - gemini) > float(config.critic("arbitration_max_delta")):
        return None
    return round((grok + gemini) / 2, 3)


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #


def build_report(
    shotlist: Shotlist,
    decisions: dict[str, dict[str, Any]],
    stats: CriticStats,
    *,
    run_id: str = "",
    pass_number: int = 1,
) -> CriticReport:
    """Fold per-shot decisions into the artifact the gapfill stage reads."""
    verdicts: list[ShotVerdict] = []
    for shot in shotlist.shots:
        decision = decisions.get(shot.id, {})
        winner = decision.get("winner")
        verdicts.append(
            ShotVerdict(
                shot_id=shot.id,
                winner=winner,
                score=float(decision.get("score", 0.0)),
                rejected=[
                    RejectedCandidate(
                        sha256=item.get("sha256", ""),
                        reason=item.get("reason", ""),
                        score=float(item.get("score", 0.0)),
                    )
                    for item in decision.get("rejected", [])
                ],
                needs_refill=winner is None,
                better_queries=list(decision.get("better_queries", [])),
                reason=str(decision.get("reason", "")),
            )
        )

    report = CriticReport(run_id=run_id, shots=verdicts, stats=stats)
    report.pass_number = pass_number
    log.info(
        "pass %d: %d/%d shots filled, %d need refill (L0 rejected %d, %d vision calls)",
        pass_number,
        sum(1 for v in verdicts if v.winner),
        len(verdicts),
        len(report.refill_needed()),
        stats.l0_rejected,
        stats.grok_vision_calls + stats.gemini_vision_calls,
        extra={"stage": STAGE},
    )
    return report


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _license_allows_commercial(license_text: str) -> bool:
    text = (license_text or "").strip().lower()
    if not text:
        # An item with no stated right is the classic Internet Archive trap.
        return False
    if "nc" in text.split("-") or "noncommercial" in text or "non-commercial" in text:
        return False
    return any(marker in text for marker in COMMERCIAL_LICENSES)


def _loopable(candidate: Candidate) -> bool:
    """A short, near-static clip can be looped; a busy one cannot, visibly."""
    return candidate.duration >= 1.0 and candidate.motion < 0.25


def _too_similar(phash: str, others: Sequence[str], distance: int) -> bool:
    return bool(phash) and any(hamming(phash, other) <= distance for other in others if other)


def _acid(candidate: Candidate) -> float:
    for flag in candidate.l0_flags:
        if flag.startswith("acid:"):
            try:
                return float(flag.split(":", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


def _off_aspect(candidate: Candidate) -> bool:
    if not candidate.w or not candidate.h:
        return False
    aspect = candidate.w / candidate.h
    return aspect > 2.2 or aspect < 0.4


def _clamp10(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 10.0))
    except (TypeError, ValueError):
        return 0.0
