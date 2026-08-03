"""Ranking: turn a pile of provider results into one ordered shortlist.

The score is deliberately explainable — every component is kept on the ranked
object so reports can say *why* an asset won, and so the escalation policy can
reason about which axis is weak (a low `relevance` means search harder, a low
`quality` means this is a Magnific job).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import FREE_STOCK_PRIORITY, VIDEO_HEIGHT
from ..spec import Visual
from .candidates import Candidate
from .keywords import QueryPlan, tokenize

WEIGHTS = {
    "relevance": 0.46,
    "quality": 0.24,
    "fit": 0.20,
    "source": 0.10,
}


@dataclass
class RankedCandidate:
    candidate: Candidate
    score: float
    relevance: float
    quality: float
    fit: float
    source_bonus: float
    reasons: list[str] = field(default_factory=list)

    @property
    def weakest_axis(self) -> str:
        axes = {"relevance": self.relevance, "quality": self.quality, "fit": self.fit}
        return min(axes, key=lambda key: axes[key])

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "score": round(self.score, 4),
            "relevance": round(self.relevance, 4),
            "quality": round(self.quality, 4),
            "fit": round(self.fit, 4),
            "source_bonus": round(self.source_bonus, 4),
            "reasons": self.reasons,
        }


def _source_bonus(source: str) -> float:
    """Earlier entries in the free-source priority list win ties."""
    try:
        index = FREE_STOCK_PRIORITY.index(source)
    except ValueError:
        return 0.5
    return 1.0 - index / max(len(FREE_STOCK_PRIORITY) - 1, 1) * 0.6


def relevance_score(candidate: Candidate, plan: QueryPlan) -> tuple[float, list[str]]:
    """How well the asset's own words match the queries we asked for."""
    reasons: list[str] = []
    haystack = set(tokenize(candidate.searchable_text))
    if not haystack:
        # Some sources (NASA video assets, archive.org) ship almost no metadata.
        return 0.45, ["no metadata to match against"]

    primary_tokens = set(tokenize(plan.primary))
    overlap = haystack & primary_tokens
    primary_hit = len(overlap) / len(primary_tokens) if primary_tokens else 0.0

    plan_tokens = set(plan.all_terms)
    breadth = len(haystack & plan_tokens) / len(plan_tokens) if plan_tokens else 0.0

    # Being found by several independent queries is a strong relevance signal.
    multi_query = min(len(candidate.matched_queries) / 3.0, 1.0)

    score = 0.55 * primary_hit + 0.25 * breadth + 0.20 * multi_query
    if primary_hit >= 0.99:
        reasons.append("matches the full primary query")
    if len(candidate.matched_queries) > 1:
        reasons.append(f"found by {len(candidate.matched_queries)} queries")

    for term in plan.must_include:
        if term and term not in candidate.searchable_text.lower():
            score *= 0.6
            reasons.append(f"missing required term '{term}'")
    for term in plan.must_avoid:
        if term and term in candidate.searchable_text.lower():
            score *= 0.25
            reasons.append(f"contains banned term '{term}'")

    return max(0.0, min(score, 1.0)), reasons


def quality_score(candidate: Candidate) -> tuple[float, list[str]]:
    """Raw technical quality, independent of the slot it will fill."""
    reasons: list[str] = []
    if not candidate.height:
        return 0.5, ["unknown resolution"]

    long_edge = max(candidate.width, candidate.height)
    short_edge = min(candidate.width, candidate.height)
    resolution = min(long_edge / 1920.0, 1.0) * 0.6 + min(short_edge / 1080.0, 1.0) * 0.4
    if long_edge >= 3840:
        reasons.append("4K source")
    elif short_edge < 720:
        reasons.append("low resolution")

    score = resolution
    if candidate.media_type == "video" and 0 < candidate.duration < 3:
        score *= 0.85
        reasons.append("very short clip")
    return max(0.0, min(score, 1.0)), reasons


def fit_score(candidate: Candidate, visual: Visual) -> tuple[float, list[str]]:
    """How well the asset drops into a 9:16 slot of this exact length."""
    reasons: list[str] = []
    score = 0.5

    if candidate.is_vertical:
        score = 1.0
        reasons.append("native vertical")
    elif candidate.is_square_ish:
        score = 0.72
    elif candidate.height >= VIDEO_HEIGHT:
        # A tall-enough landscape asset survives a centre crop to 9:16.
        score = 0.62
        reasons.append("landscape, croppable")
    else:
        score = 0.4
        reasons.append("landscape, needs upscale to crop")

    if candidate.media_type == "video":
        if candidate.duration and candidate.duration + 0.25 < visual.duration:
            deficit = visual.duration - candidate.duration
            score *= max(0.35, 1.0 - deficit / max(visual.duration, 0.1))
            reasons.append(f"{candidate.duration:g}s clip for a {visual.duration:g}s slot")
        elif candidate.duration > visual.duration * 6 and candidate.duration > 60:
            score *= 0.9
            reasons.append("long clip, needs trimming")
    elif visual.motion == "none" and visual.duration > 4:
        score *= 0.85
        reasons.append("still image on a long slot without motion")

    return max(0.0, min(score, 1.0)), reasons


def score_candidate(candidate: Candidate, visual: Visual, plan: QueryPlan) -> RankedCandidate:
    relevance, relevance_reasons = relevance_score(candidate, plan)
    quality, quality_reasons = quality_score(candidate)
    fit, fit_reasons = fit_score(candidate, visual)
    source = _source_bonus(candidate.source)

    total = (
        WEIGHTS["relevance"] * relevance
        + WEIGHTS["quality"] * quality
        + WEIGHTS["fit"] * fit
        + WEIGHTS["source"] * source
    )
    if candidate.generated:
        # Generated assets are made to order; do not penalise them on source.
        total = min(1.0, total + 0.04)

    return RankedCandidate(
        candidate=candidate,
        score=round(total, 4),
        relevance=relevance,
        quality=quality,
        fit=fit,
        source_bonus=source,
        reasons=relevance_reasons + quality_reasons + fit_reasons,
    )


def rank_candidates(
    candidates: list[Candidate],
    visual: Visual,
    plan: QueryPlan,
    *,
    limit: int = 12,
) -> list[RankedCandidate]:
    """Score, sort and truncate. Highest score first, stable on ties."""
    ranked = [score_candidate(candidate, visual, plan) for candidate in candidates]
    ranked.sort(key=lambda item: (-item.score, item.candidate.key))
    return ranked[:limit]
