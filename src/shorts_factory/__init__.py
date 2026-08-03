"""Branded Shorts Factory — one JSON in, one finished 9:16 MP4 out.

The package is intentionally dependency-light: the whole orchestration layer
runs on the Python standard library so it behaves identically on a laptop and
inside a GitHub Actions runner. External systems (HeyGen, ElevenLabs, stock
providers, Magnific, Grok, HyperFrames) are reached through thin adapters that
degrade gracefully when their credentials are absent.
"""

from .errors import (
    BudgetExceededError,
    ProviderError,
    QualityGateError,
    RenderError,
    ShortsFactoryError,
    SpecError,
)
from .spec import Spec, SpecIssue, load_spec, parse_spec

__version__ = "0.1.0"

__all__ = [
    "BudgetExceededError",
    "ProviderError",
    "QualityGateError",
    "RenderError",
    "ShortsFactoryError",
    "Spec",
    "SpecError",
    "SpecIssue",
    "load_spec",
    "parse_spec",
    "__version__",
]
