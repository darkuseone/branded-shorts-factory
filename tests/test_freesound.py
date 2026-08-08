"""Freesound / Pixabay catalog fill helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from shorts_factory.search.providers.freesound import (
    accept_for_role,
    cache_name,
    fill_from_pixabay_catalog,
    load_pixabay_catalog,
)


def test_cache_name_is_filesystem_safe():
    name = cache_name("whoosh", {"id": 123, "name": "Fast Whoosh!! / cool"})
    assert name.endswith(".mp3")
    assert "/" not in name
    assert " " not in name


def test_load_pixabay_catalog_skips_missing(tmp_path: Path):
    assert load_pixabay_catalog(tmp_path / "missing.json") == []


def test_load_pixabay_catalog_reads_items(tmp_path: Path):
    path = tmp_path / "pixabay-sfx.json"
    path.write_text(
        json.dumps({"items": [{"id": "a", "roles": ["ui"], "url": "https://example.com/a.mp3"}]}),
        encoding="utf-8",
    )
    items = load_pixabay_catalog(path)
    assert len(items) == 1
    assert items[0]["id"] == "a"


def test_fill_from_pixabay_skips_empty_urls(tmp_path: Path):
    catalog = [{"id": "empty", "roles": ["ui"], "url": ""}]
    http = MagicMock()
    result = fill_from_pixabay_catalog("ui", catalog=catalog, bank_dir=tmp_path, http=http, ffmpeg="ffmpeg")
    assert result is None
    http.download.assert_not_called()


def test_accept_for_role_rejects_unanalyzable(tmp_path: Path):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not-audio")
    assert accept_for_role(junk, "impact", ffmpeg="ffmpeg") is False
