"""Two-level quality gate: aggregation, verdicts and the run report."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..media.download import LocalAsset
from .native import NativeVerdict
from .vision import VisionVerdict

Outcome = Literal["accepted", "rejected", "manual_review"]


@dataclass
class VisualQA:
    """The full QA trail for one visual slot."""

    visual_id: str
    outcome: Outcome
    asset: LocalAsset | None = None
    native: NativeVerdict | None = None
    vision: VisionVerdict | None = None
    attempts: int = 1
    rejected: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.outcome == "manual_review"

    def to_dict(self) -> dict[str, object]:
        return {
            "visual_id": self.visual_id,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "asset": self.asset.to_dict() if self.asset else None,
            "native": self.native.to_dict() if self.native else None,
            "vision": self.vision.to_dict() if self.vision else None,
            "rejected": self.rejected,
            "notes": self.notes,
        }


@dataclass
class QAReport:
    """Everything the two gates decided, ready to be written to disk."""

    results: list[VisualQA] = field(default_factory=list)

    def add(self, result: VisualQA) -> None:
        self.results.append(result)

    @property
    def accepted(self) -> list[VisualQA]:
        return [result for result in self.results if result.outcome == "accepted"]

    @property
    def manual_review(self) -> list[VisualQA]:
        return [result for result in self.results if result.outcome == "manual_review"]

    @property
    def rejected(self) -> list[VisualQA]:
        return [result for result in self.results if result.outcome == "rejected"]

    @property
    def vision_checked(self) -> int:
        return sum(1 for r in self.results if r.vision and not r.vision.skipped)

    def is_renderable(self) -> bool:
        """A render needs at least one accepted visual and no empty slots."""
        return bool(self.accepted) and not self.rejected

    def summary(self) -> str:
        return (
            f"{len(self.accepted)} accepted, {len(self.manual_review)} need review, "
            f"{len(self.rejected)} unfilled ({self.vision_checked} vision-checked)"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary(),
            "counts": {
                "accepted": len(self.accepted),
                "manual_review": len(self.manual_review),
                "rejected": len(self.rejected),
                "vision_checked": self.vision_checked,
            },
            "visuals": [result.to_dict() for result in self.results],
        }


def combine(
    native: NativeVerdict | None,
    vision: VisionVerdict | None,
    *,
    require_vision: bool,
    manual_review_ok: bool,
) -> Outcome:
    """Fold both gates into a single outcome.

    Level 1 is a hard filter: if the metadata says the asset is about something
    else, it never reaches level 2. Level 2 can still veto, and when it is
    unavailable the asset is only accepted if vision was not required.

    Level 1 has a third answer besides pass and fail: *plausible, but the words
    do not settle it* — an asset with no metadata, or one sharing a single
    generic noun with the subject. Those may only ship if something actually
    looked at the frame. When the vision gate is missing or abstains they are
    rejected, and the slot falls back to the brand backdrop. An empty slot is
    a smaller failure than a hair salon in a story about a break-in.
    """
    if native is not None and not native.passed:
        return "rejected"

    unseen = native is not None and native.needs_vision

    if vision is None:
        if unseen:
            return "rejected"
        return "accepted" if not require_vision else ("manual_review" if manual_review_ok else "rejected")

    if vision.verdict == "pass":
        return "accepted"
    if vision.verdict == "replace":
        return "rejected"

    # "manual": the model abstained or could not be reached.
    if vision.skipped and unseen:
        return "rejected"
    if vision.skipped and not require_vision:
        return "accepted"
    return "manual_review" if manual_review_ok else "rejected"
