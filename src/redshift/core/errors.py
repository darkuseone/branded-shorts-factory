"""Own exception classes. No bare excepts anywhere in the codebase (.cursorrules)."""

from __future__ import annotations


class RedshiftError(Exception):
    """Base class for everything this project raises."""


class ConfigError(RedshiftError):
    """A config/*.yaml file is missing, malformed or lacks a required key."""


class SchemaError(RedshiftError):
    """An artifact failed pydantic validation, or its schema_version is wrong."""

    def __init__(self, artifact: str, detail: str):
        self.artifact = artifact
        super().__init__(f"{artifact}: {detail}")


class BudgetExceeded(RedshiftError):
    """A call would exceed the per-video budget.

    Caught by the orchestrator, which switches on the degradation ladder (§4.6)
    instead of failing the run.
    """

    def __init__(self, kind: str, used: int, limit: int):
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(f"budget for {kind} exhausted: {used}/{limit}")


class ProviderError(RedshiftError):
    """An external provider failed after retries."""

    def __init__(self, provider: str, message: str, *, status: int | None = None):
        self.provider = provider
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"[{provider}] {message}{suffix}")


class CircuitOpen(ProviderError):
    """The provider tripped its circuit breaker and is parked (§4.4)."""

    def __init__(self, provider: str, until: float):
        self.until = until
        super().__init__(provider, "circuit breaker open, routing to next source")


class StageFailed(RedshiftError):
    """A pipeline stage could not produce its artifact."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"{stage}: {message}")


class RhythmViolation(RedshiftError):
    """A shotlist breaks the rhythm rules of §8.4 — a hard editorial defect."""


class QCFailed(RedshiftError):
    """The finished video failed a blocking QC check (§17)."""

    def __init__(self, checks: list[str]):
        self.checks = checks
        super().__init__("blocking QC checks failed: " + ", ".join(checks))
