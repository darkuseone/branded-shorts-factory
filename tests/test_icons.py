"""The icon bank (§9.4) and the free-tier accounting behind it.

The point of these tests is that a logo never costs more than it should and
never breaks a card when it is absent.
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from redshift.clients.magnific import DailyQuota, LibraryAsset, _asset, _extract_url, _items
from redshift.core.config import load_config
from redshift.core.schemas import InfographicItem, InfographicSpec
from redshift.render.icons import IconBank, data_uri, magnific_fetcher, slugify
from redshift.render.infographics import BrandTokens, build_html

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def bank(tmp_path: Path) -> IconBank:
    (tmp_path / "assets" / "icons").mkdir(parents=True)
    return IconBank(tmp_path)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


def test_slug_is_stable_across_the_ways_a_name_gets_written():
    assert slugify("OpenAI") == slugify("openai") == "openai"
    assert slugify("Hugging Face") == "huggingface"
    assert slugify("  NVIDIA  ") == "nvidia"


def test_cyrillic_names_slug_without_being_emptied():
    # A regex that only knows ASCII silently turns "Нвидиа" into "" and every
    # Russian entity collapses onto the same icon.
    assert slugify("Нвидиа") == "nvidia"
    assert slugify("Роскосмос") == "роскосмос"


def test_aliases_point_at_the_mark_people_actually_mean():
    assert slugify("Claude") == "anthropic"
    assert slugify("ChatGPT") == "openai"


# --------------------------------------------------------------------------- #
# Resolution order
# --------------------------------------------------------------------------- #


def test_a_committed_icon_is_used_without_any_fetch(bank, tmp_path):
    (tmp_path / "assets" / "icons" / "openai.png").write_bytes(PNG)
    calls = []
    bank.fetch = lambda name, dest: calls.append(name)
    assert bank.resolve("OpenAI").startswith("data:image/png;base64,")
    assert calls == [], "a committed icon must never reach the network"


def test_resolution_is_memoised_per_run(bank, tmp_path):
    calls = []

    def fetch(name, destination):
        calls.append(name)
        return None

    bank.fetch = fetch
    bank.resolve("Nvidia")
    bank.resolve("NVIDIA")
    assert len(calls) == 1, "the same entity is fetched at most once per run"


def test_a_fetched_icon_lands_in_the_bank_for_next_time(bank, tmp_path):
    def fetch(name, destination):
        destination.write_bytes(PNG)
        return destination

    bank.fetch = fetch
    assert bank.resolve("Hugging Face").startswith("data:image/png;base64,")
    assert (tmp_path / "assets" / "icons" / "huggingface.png").exists()


def test_a_missing_icon_degrades_instead_of_raising(bank):
    assert bank.resolve("Some Startup") == ""
    assert "some-startup" in bank.missing


def test_a_fetcher_that_explodes_does_not_take_the_card_down(bank):
    def fetch(name, destination):
        raise RuntimeError("provider is down")

    bank.fetch = fetch
    assert bank.resolve("OpenAI") == ""


def test_oversized_files_are_refused_as_icons(tmp_path):
    fat = tmp_path / "fat.png"
    fat.write_bytes(b"\x89PNG" + b"\x00" * 600_000)
    assert data_uri(fat) == "", "a stock photo is not a logo"


# --------------------------------------------------------------------------- #
# The free allowance
# --------------------------------------------------------------------------- #


def test_quota_counts_down_and_then_refuses(tmp_path):
    quota = DailyQuota(tmp_path / "q.json", limit=2)
    assert quota.remaining == 2
    assert quota.take() and quota.take()
    assert not quota.take(), "the plan allows a fixed number a day, not one more"
    assert quota.remaining == 0


def test_quota_resets_on_a_new_day(tmp_path):
    path = tmp_path / "q.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"date": yesterday, "count": 100}), encoding="utf-8")
    assert DailyQuota(path, limit=100).remaining == 100


def test_a_corrupt_ledger_does_not_block_downloads(tmp_path):
    path = tmp_path / "q.json"
    path.write_text("{not json", encoding="utf-8")
    assert DailyQuota(path, limit=5).take()


def test_fetcher_stops_when_the_day_is_spent(tmp_path):
    """Quota exhaustion is a normal outcome — the card draws a glyph instead."""

    class Client:
        def is_available(self):
            return True

        def search_library(self, query, *, kind="image", limit=8):
            return [LibraryAsset(id="1", url="https://example.test/logo.png", width=64, height=64)]

        def download_library_asset(self, asset, destination):
            return None  # quota refused it

        dry_run = False

    fetch = magnific_fetcher(Client())
    assert fetch("OpenAI", tmp_path / "openai.png") is None


def test_fetcher_skips_assets_too_large_to_be_a_mark(tmp_path):
    seen = []

    class Client:
        def is_available(self):
            return True

        def search_library(self, query, *, kind="image", limit=8):
            return [LibraryAsset(id="1", url="https://example.test/wall.png", width=4096, height=4096)]

        def download_library_asset(self, asset, destination):
            seen.append(asset.id)
            return destination

    assert magnific_fetcher(Client())("OpenAI", tmp_path / "openai.png") is None
    assert seen == [], "a 4K plate is a background, not an icon"


def test_asset_suffix_follows_the_url(tmp_path):
    assert LibraryAsset(id="1", url="https://x.test/a/logo.svg?v=2").suffix == ".svg"
    assert LibraryAsset(id="1", url="https://x.test/a/clip", kind="video").suffix == ".mp4"


# --------------------------------------------------------------------------- #
# Provider response shapes
# --------------------------------------------------------------------------- #


def test_library_items_are_found_wherever_the_api_puts_them():
    assert len(_items({"results": [{"id": 1}]})) == 1
    assert len(_items({"data": [{"id": 1}, {"id": 2}]})) == 2
    assert _items({"error": "nope"}) == []


def test_an_item_without_a_usable_url_is_dropped():
    assert _asset({"id": "1", "url": "not-a-url"}, "image") is None
    assert _asset({"id": "1", "download_url": "https://x.test/a.png"}, "image") is not None


def test_a_generation_url_is_found_through_nesting():
    assert _extract_url({"output": {"image_url": "https://x.test/a.png"}}) == "https://x.test/a.png"
    assert _extract_url({"status": "processing"}) is None


# --------------------------------------------------------------------------- #
# The card actually carries the mark
# --------------------------------------------------------------------------- #


def test_a_resolved_mark_reaches_the_card_as_an_inline_image(tmp_path):
    (tmp_path / "assets" / "icons").mkdir(parents=True)
    (tmp_path / "assets" / "icons" / "openai.png").write_bytes(PNG)
    spec = InfographicSpec(
        shot_id="s1",
        template="PROCESS_ARROW",
        title="Взлом",
        items=[
            InfographicItem(label="OpenAI"),
            InfographicItem(label="Hugging Face"),
            InfographicItem(label="1681 репозиторий", icon="alert"),
        ],
    )
    markup = build_html(spec, BrandTokens.from_config(load_config()), IconBank(tmp_path))
    assert '<img src="data:image/png;base64,' in markup
    # The unresolved ones still draw, so the card is never half-empty.
    assert 'class="glyph"' in markup


def test_a_card_renders_identically_with_no_bank_at_all():
    spec = InfographicSpec(
        shot_id="s1",
        template="ICON_FACT_LIST",
        title="Итоги",
        items=[InfographicItem(label="скорость", value="7 км/с", icon="rocket")],
    )
    tokens = BrandTokens.from_config(load_config())
    assert build_html(spec, tokens) == build_html(spec, tokens, None)
