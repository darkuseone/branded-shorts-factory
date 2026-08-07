"""Two-level quality assurance: native logic, then Gemini Vision (Grok fallback)."""

from .gate import QAReport, VisualQA, combine
from .gemini_vision import CompositeVisionGate, GeminiVisionGate
from .native import NativeVerdict, check_native, domains_of
from .vision import GrokVisionGate, VisionVerdict

__all__ = [
    "CompositeVisionGate",
    "GeminiVisionGate",
    "GrokVisionGate",
    "NativeVerdict",
    "QAReport",
    "VisionVerdict",
    "VisualQA",
    "check_native",
    "combine",
    "domains_of",
]
