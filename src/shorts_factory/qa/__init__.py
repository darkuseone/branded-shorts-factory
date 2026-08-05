"""Two-level quality assurance: native logic, then Grok Vision."""

from .gate import QAReport, VisualQA, combine
from .native import NativeVerdict, check_native, domains_of
from .vision import GrokVisionGate, VisionVerdict

__all__ = [
    "GrokVisionGate",
    "NativeVerdict",
    "QAReport",
    "VisionVerdict",
    "VisualQA",
    "check_native",
    "combine",
    "domains_of",
]
