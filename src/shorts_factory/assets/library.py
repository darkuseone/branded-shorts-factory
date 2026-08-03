"""User-supplied libraries: music tracks and the meme bank.

Both are just folders the user fills by hand. An optional `index.json` next to
the files adds tags; without it, tags are derived from filenames, so dropping
`shock-pikachu.gif` into `assets/memes/` already makes it findable by "shock".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_utils import get_logger

log = get_logger("library")

AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"})
MEME_SUFFIXES = frozenset({".gif", ".mp4", ".webm", ".png", ".jpg", ".jpeg", ".webp"})

_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _tags_from_name(path: Path) -> list[str]:
    return [part.lower() for part in _SPLIT_RE.split(path.stem) if len(part) > 1 and not part.isdigit()]


@dataclass
class LibraryItem:
    path: Path
    tags: list[str] = field(default_factory=list)
    title: str = ""
    duration: float = 0.0

    @property
    def name(self) -> str:
        return self.path.stem

    def matches(self, wanted: list[str]) -> int:
        """Number of requested tags this item satisfies."""
        haystack = set(self.tags) | set(_tags_from_name(self.path))
        return sum(1 for tag in wanted if tag.lower() in haystack)


class _FolderLibrary:
    suffixes: frozenset[str] = frozenset()
    label = "library"

    def __init__(self, directory: Path):
        self.directory = directory
        self._items: list[LibraryItem] | None = None

    @property
    def items(self) -> list[LibraryItem]:
        if self._items is None:
            self._items = self._load()
        return self._items

    def _load(self) -> list[LibraryItem]:
        if not self.directory.is_dir():
            log.info("%s folder missing: %s", self.label, self.directory)
            return []

        index: dict[str, dict] = {}
        index_file = self.directory / "index.json"
        if index_file.exists():
            try:
                raw = json.loads(index_file.read_text(encoding="utf-8"))
                entries = raw.get("items", raw) if isinstance(raw, dict) else raw
                for entry in entries or []:
                    if isinstance(entry, dict) and entry.get("file"):
                        index[str(entry["file"])] = entry
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("%s index.json unreadable: %s", self.label, exc)

        items: list[LibraryItem] = []
        for path in sorted(self.directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in self.suffixes:
                continue
            entry = index.get(path.name, {})
            tags = entry.get("tags") or _tags_from_name(path)
            items.append(
                LibraryItem(
                    path=path,
                    tags=[str(tag).lower() for tag in tags],
                    title=str(entry.get("title") or path.stem),
                    duration=float(entry.get("duration") or 0.0),
                )
            )
        log.info("%s: %d item(s) in %s", self.label, len(items), self.directory)
        return items

    def by_name(self, name: str) -> LibraryItem | None:
        if not name:
            return None
        wanted = name.strip().lower()
        for item in self.items:
            if item.path.name.lower() == wanted or item.name.lower() == wanted:
                return item
        # Fall back to a prefix match so "pulse" finds "pulse_01.mp3".
        for item in self.items:
            if item.name.lower().startswith(wanted):
                return item
        return None


class MusicLibrary(_FolderLibrary):
    suffixes = AUDIO_SUFFIXES
    label = "music"

    def resolve(self, requested: str) -> LibraryItem | None:
        """Find the requested track, or fall back to the first one available."""
        item = self.by_name(requested)
        if item:
            return item
        if requested:
            log.warning("music track %r not found in %s", requested, self.directory)
        return self.items[0] if self.items else None


class MemeLibrary(_FolderLibrary):
    suffixes = MEME_SUFFIXES
    label = "memes"

    def find(self, tags: list[str], limit: int = 1) -> list[LibraryItem]:
        """Best-matching memes for the requested tags. Empty when nothing fits.

        Memes are only ever inserted on an explicit tag request, so an empty
        result is a normal, quiet outcome — never a fallback to something random.
        """
        if not tags:
            return []
        scored = [(item.matches(tags), item) for item in self.items]
        hits = [(score, item) for score, item in scored if score > 0]
        hits.sort(key=lambda pair: (-pair[0], pair[1].name))
        if not hits:
            log.info("no meme matches tags %s", tags)
        return [item for _, item in hits[:limit]]
