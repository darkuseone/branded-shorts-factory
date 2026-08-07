"""Authoring helpers: research packs and Shorts script craft."""

from .research import NewsClaim, ResearchPack, load_research, research_topic, save_research
from .scriptcraft import (
    HookIssue,
    ScriptDraft,
    draft_to_document,
    validate_hook,
    validate_script_craft,
)

__all__ = [
    "HookIssue",
    "NewsClaim",
    "ResearchPack",
    "ScriptDraft",
    "draft_to_document",
    "load_research",
    "research_topic",
    "save_research",
    "validate_hook",
    "validate_script_craft",
]
