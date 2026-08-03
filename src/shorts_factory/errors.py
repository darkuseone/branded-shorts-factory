"""Exception hierarchy shared by every stage of the pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .spec import SpecIssue


class ShortsFactoryError(Exception):
    """Base class for every error raised by this project."""


class SpecError(ShortsFactoryError):
    """The incoming JSON scenario is unusable.

    Carries every problem found in a single pass so the user can fix the whole
    file at once instead of playing whack-a-mole with one error per run.
    """

    def __init__(self, issues: Sequence[SpecIssue], source: str | None = None):
        self.issues = list(issues)
        self.source = source
        header = f"Invalid scenario JSON ({source})" if source else "Invalid scenario JSON"
        body = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(f"{header}:\n{body}" if body else header)


class ProviderError(ShortsFactoryError):
    """A remote provider (stock, generative, avatar, TTS) failed."""

    def __init__(self, provider: str, message: str, *, status: int | None = None):
        self.provider = provider
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"[{provider}] {message}{suffix}")


class BudgetExceededError(ShortsFactoryError):
    """A paid-token request would exceed the configured budget."""


class QualityGateError(ShortsFactoryError):
    """Quality assurance refused to hand the timeline over to the renderer."""


class RenderError(ShortsFactoryError):
    """HyperFrames could not lint, check or render the composition."""
