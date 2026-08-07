"""Gemini / composite vision gate wiring."""

from __future__ import annotations

from shorts_factory.config import Credentials, Settings
from shorts_factory.qa.gemini_vision import CompositeVisionGate, GeminiVisionGate


def test_gemini_unavailable_without_key(tmp_path):
    settings = Settings.from_env(root=tmp_path, workdir=tmp_path / "build")
    settings = Settings(
        credentials=Credentials(),
        paths=settings.paths,
        offline=True,
    )
    gate = GeminiVisionGate(settings)
    assert not gate.is_available
    assert "GEMINI" in gate.unavailable_reason()


def test_composite_falls_back_to_grok_message():
    settings = Settings.from_env()
    settings = Settings(
        credentials=Credentials(grok="x"),
        paths=settings.paths,
    )
    gate = CompositeVisionGate(settings)
    assert gate.is_available
    assert "gemini" in gate.unavailable_reason().lower() or gate.gemini.is_available is False
