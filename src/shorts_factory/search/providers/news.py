"""Thin news/search adapter used by research and photo-hint downloads."""

from __future__ import annotations

from ...author.research import research_topic
from ...config import Settings
from ...logging_utils import get_logger

log = get_logger("news_provider")


class NewsProvider:
    name = "news"

    def __init__(self, settings: Settings):
        self.settings = settings

    def research(self, topic: str, *, max_claims: int = 12):
        return research_topic(topic, settings=self.settings, max_claims=max_claims)
