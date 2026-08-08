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
    #: Meme catalog fields from assets/memes/index.json.
    humor: str = ""
    beats: list[str] = field(default_factory=list)
    intensity: str = "soft"
    safe_for_science: bool = True
    trim_start: float = 0.0
    max_use: float = 0.0

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
        blob = f"{self.name} {' '.join(self.tags)}".lower()
        if any(token in blob for token in ("doppler", "passing-car", "train", "traffic")):
            return False
        # Hot real-world beds (e.g. −6 LUFS car pass) must not punch cuts.
        if (
            self.lufs > -12.0
            and any(token in blob for token in ("car", "pass", "whoosh"))
            and ("doppler" in blob or "passing" in blob)
        ):
            return False
        # Without a scan we cannot know — trust the tags and let the picker decide.
        return True if not shape else bool(self.tags)

    def matches(self, wanted: list[str]) -> int:
        """Number of requested tags this item satisfies."""
        haystack = set(self.tags) | set(_tags_from_name(self.path))
        if self.humor:
            haystack.add(self.humor.lower())
        haystack.update(beat.lower() for beat in self.beats)
        return sum(1 for tag in wanted if tag.lower() in haystack)


class _FolderLibrary:
    suffixes: frozenset[str] = frozenset()
    label = "library"

    def __init__(self, directory: Path):
        self.directory = directory
        self._items: list[LibraryItem] | None = None

    def invalidate(self) -> None:
        """Drop the cached listing so the next read re-scans the folder."""
        self._items = None

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
                    humor=str(entry.get("humor") or ""),
                    beats=[str(beat) for beat in (entry.get("beats") or [])],
                    intensity=str(entry.get("intensity") or "soft"),
                    safe_for_science=bool(entry.get("safe_for_science", True)),
                    trim_start=float(entry.get("trim_start") or 0.0),
                    max_use=float(entry.get("max_use") or 0.0),
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


#: Filenames / tags that must never be montage accents (real-world pass-bys).
SFX_ACCENT_BLACKLIST = (
    "doppler",
    "passing-car",
    "passing_car",
    "train",
    "traffic",
    "highway",
    "vehicle",
    "automobile",
)

#: Hard channel kit — only these files may be montage accents (plan: 5 sounds).
CHANNEL_SFX_BY_ROLE: dict[str, tuple[str, ...]] = {
    "impact": ("35920__altemark__rimshot",),
    "whoosh": ("air-effect-single-sharp",),
    "swoosh": ("air-effect-single-sharp",),
    "transition": ("air-effect-single-sharp",),
    "ui": ("35917__altemark__claves2",),
    "click": ("35917__altemark__claves2",),
    "thump": ("35926__altemark__tom1",),
    # Soft subscribe bell (thumbpiano) instead of snare pop.
    "pop": (
        "35273__linse__thumbpiano_gb_1",
        "35274__linse__thumbpiano_gb_2",
        "35265__linse__thumbpiano_d_1",
    ),
    "bell": (
        "35273__linse__thumbpiano_gb_1",
        "35274__linse__thumbpiano_gb_2",
    ),
    "riser": ("air-effect-single-sharp",),
    "power_down": ("35917__altemark__claves2",),
    "power_up": ("35917__altemark__claves2",),
    "sub_drop": ("35926__altemark__tom1",),
    "glitch": ("35917__altemark__claves2",),
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
        """Best sound for one effect from the fixed 5-sound channel kit."""
        allow = CHANNEL_SFX_BY_ROLE.get(fx_type) or CHANNEL_SFX_BY_ROLE.get("ui")
        if allow:
            for name in allow:
                for item in self.items:
                    name_match = item.name.lower() == name.lower() or name.lower() in item.name.lower()
                    if name_match and not _sfx_blacklisted(item):
                        return item

        wanted = list(SFX_SYNONYMS.get(fx_type, (fx_type,)))
        if extra_tags:
            wanted += [tag.lower() for tag in extra_tags]

        kit_present = any(_in_channel_kit(item) for item in self.items)
        scored = [
            (item.matches(wanted), item)
            for item in self.items
            if item.usable_as_accent
            and not _sfx_blacklisted(item)
            and (not kit_present or _in_channel_kit(item))
        ]
        hits = [(score, item) for score, item in scored if score > 0]
        if not hits:
            return None

        best = max(score for score, _ in hits)
        pool = sorted((item for score, item in hits if score == best), key=lambda item: item.name)
        index = self._used.get(fx_type, 0)
        self._used[fx_type] = index + 1
        return pool[index % len(pool)]


def _sfx_blacklisted(item: LibraryItem) -> bool:
    blob = f"{item.name} {' '.join(item.tags)}".lower()
    return any(token in blob for token in SFX_ACCENT_BLACKLIST)


def _in_channel_kit(item: LibraryItem) -> bool:
    names = {name.lower() for names in CHANNEL_SFX_BY_ROLE.values() for name in names}
    stem = item.name.lower()
    return any(name in stem for name in names)


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

    def pick_for_beat(
        self,
        *,
        beat: str,
        tags: list[str],
        humor: str = "",
        science_safe: bool = True,
        prefer_short: bool = True,
    ) -> LibraryItem | None:
        """Pick one meme for an irony beat, preferring short safe clips."""
        wanted = [tag.lower() for tag in tags]
        if humor:
            wanted.append(humor.lower())
        if beat:
            wanted.append(beat.lower())

        pool: list[tuple[int, float, LibraryItem]] = []
        for item in self.items:
            if science_safe and not item.safe_for_science:
                continue
            if item.intensity == "hard" and science_safe:
                continue
            # Still allow strong tag matches even if beat list misses.
            if (
                beat
                and item.beats
                and beat not in item.beats
                and humor not in item.beats
                and item.matches(wanted) < 2
            ):
                continue
            score = item.matches(wanted)
            if beat and beat in item.beats:
                score += 3
            if humor and item.humor == humor:
                score += 2
            if score <= 0:
                continue
            usable = item.max_use or (item.duration if item.duration else 1.2)
            # Prefer punchy clips under ~1.6s of usable length.
            brevity = 2.0 if usable <= 1.6 else (1.0 if usable <= 2.2 else 0.0)
            pool.append((score, brevity if prefer_short else 0.0, item))

        if not pool:
            # Soft fallback: ignore science_safe for soft intensity only.
            return self.find(wanted, limit=1)[0] if self.find(wanted, limit=1) else None

        pool.sort(key=lambda row: (-row[0], -row[1], row[2].name))
        return pool[0][2]
