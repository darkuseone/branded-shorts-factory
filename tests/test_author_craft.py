"""Author craft: hooks, research packs."""

from __future__ import annotations

from pathlib import Path

from shorts_factory.author.research import load_research, research_topic, save_research
from shorts_factory.author.scriptcraft import validate_hook, validate_script_craft
from shorts_factory.config import Settings


def test_hook_rejects_greeting():
    issues = validate_hook("Привет, сегодня поговорим про дыры", 3.0)
    assert any(issue.level == "error" for issue in issues)


def test_hook_ideal_window():
    issues = validate_hook("Модель OpenAI сама взломала Hugging Face.", 3.0)
    assert not any(issue.level == "error" for issue in issues)


def test_hook_too_long_is_error():
    issues = validate_hook("Факт.", 6.0)
    assert any("hard max" in issue.message for issue in issues)


def test_research_offline_enriches_openai_topic(tmp_path: Path):
    settings = Settings.from_env(root=Path("/workspace"), workdir=tmp_path)
    # Force offline via settings replacement
    from dataclasses import replace

    settings = replace(settings, offline=True)
    pack = research_topic("OpenAI Hugging Face ExploitGym", settings=settings)
    assert len(pack.claims) >= 3
    dest = tmp_path / "research.json"
    save_research(pack, dest)
    loaded = load_research(dest)
    assert loaded is not None
    assert loaded.claims


def test_script_craft_warns_on_avatar_everywhere():
    issues = validate_script_craft(
        hook_text="OpenAI модель сама взломала Hugging Face.",
        hook_duration=3.0,
        body_durations=[5, 5, 5],
        avatar_segment_count=4,
        total_segments=4,
    )
    assert any("avatar" in issue.message for issue in issues)
