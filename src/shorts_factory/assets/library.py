"""User-supplied libraries: music tracks, the sound-effect bank and memes.

All three are just folders the user fills by hand. An optional `index.json`
next to the files adds tags; without it, tags are derived from filenames, so
dropping `shock-pikachu.gif` into `assets/memes/` already makes it findable by
"shock", and `whoosh-fast-01.wav` into `assets/sfx/` findable by "whoosh".
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
    #: Measurements from `sfxscan`. Empty when the bank has never been scanned —
    #: placement then falls back to filename matching and starts the sound on
    #: the beat instead of aligning by peak.
    analysis: dict[str, object] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def peak_at(self) -> float:
        return float(self.analysis.get("peak_at") or 0.0)

    @property
    def lufs(self) -> float:
        return float(self.analysis.get("lufs") or -20.0)

    @property
    def peak_dbtp(self) -> float:
        return float(self.analysis.get("peak_dbtp") or 0.0)

    @property
    def attack(self) -> float:
        return float(self.analysis.get("attack") or 0.0)

    @property
    def tail(self) -> float:
        return float(self.analysis.get("tail") or 0.0)

    @property
    def shape(self) -> str:
        return str(self.analysis.get("shape") or "")

    @property
    def usable_as_accent(self) -> bool:
        shape = self.shape
        if shape in {"drone", "bed"}:
            return False
        # Without a scan we cannot know — trust the tags and let the picker decide.
        return True if not shape else bool(self.tags)

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
            analysis = entry.get("analysis") if isinstance(entry.get("analysis"), dict) else {}
            items.append(
                LibraryItem(
                    path=path,
                    tags=[str(tag).lower() for tag in tags],
                    title=str(entry.get("title") or path.stem),
                    duration=float(entry.get("duration") or 0.0),
                    analysis=dict(analysis),
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
        for item in self.items:
            if item.title and item.title.lower() == wanted:
                return item
        # Fall back to a prefix / substring match so "pulse" finds "Digital Pulse".
        for item in self.items:
            if item.name.lower().startswith(wanted) or wanted in item.name.lower():
                return item
            if item.title and wanted in item.title.lower():
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


#: Words that mean the same thing as an `audio_fx.type`. Sound packs name
#: files however they like, so a "boom" should still answer a request for an
#: "impact".
SFX_SYNONYMS: dict[str, tuple[str, ...]] = {
    "whoosh": ("whoosh", "swoosh", "swish", "woosh", "transition", "sweep"),
    "swoosh": ("swoosh", "whoosh", "swish", "sweep"),
    "impact": ("impact", "hit", "boom", "punch", "slam", "braam"),
    "riser": ("riser", "rise", "buildup", "build", "uplifter", "tension"),
    "sub_drop": ("sub", "subdrop", "drop", "bass", "808", "boom"),
    "pop": ("pop", "bubble", "blip", "plop", "bounce"),
    "click": ("click", "tick", "tap", "snap"),
    "glitch": ("glitch", "stutter", "digital", "error", "static"),
    "transition": ("transition", "whoosh", "swoosh", "sweep", "cut"),
    "ui": ("ui", "blip", "beep", "notify", "notification", "confirm"),
    "thump": ("thump", "pulse", "impact", "hit", "kick"),
    "power_down": ("power_down", "powerdown", "tick", "click", "ui"),
    "power_up": ("power_up", "powerup", "tick", "click", "ui"),
}


class SfxLibrary(_FolderLibrary):
    """Your own sound design, matched to `audio_fx` entries by type.

    Preferred over generation: it costs nothing, it is instant, and using the
    same handful of sounds across videos is what makes a channel sound like
    itself. Generation only fills in what the folder cannot answer.
    """

    suffixes = AUDIO_SUFFIXES
    label = "sfx"

    def __init__(self, directory: Path):
        super().__init__(directory)
        self._used: dict[str, int] = {}

    def pick(self, fx_type: str, extra_tags: list[str] | None = None) -> LibraryItem | None:
        """Best sound for one effect, rotating between equally good matches.

        Rotation matters: six identical whooshes in one Short is exactly the
        cheap-edit sound this project exists to avoid. Music beds and drones
        are excluded even when their tags would otherwise match — they are
        not accents, and placing one on a cut reads as a mistake.
        """
        wanted = list(SFX_SYNONYMS.get(fx_type, (fx_type,)))
        if extra_tags:
            wanted += [tag.lower() for tag in extra_tags]

        scored = [
            (item.matches(wanted), item)
            for item in self.items
            if item.usable_as_accent
        ]
        hits = [(score, item) for score, item in scored if score > 0]
        if not hits:
            return None

        best = max(score for score, _ in hits)
        pool = sorted((item for score, item in hits if score == best), key=lambda item: item.name)
        index = self._used.get(fx_type, 0)
        self._used[fx_type] = index + 1
        return pool[index % len(pool)]


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
