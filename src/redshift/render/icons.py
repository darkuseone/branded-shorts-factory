"""Logos and app icons for infographic cards (§9.4).

A card that says "OpenAI → Hugging Face" reads as a diagram when both names
carry their real mark, and as a slide when they carry a grey monogram. So the
icon is worth fetching — but never worth blocking on, and never worth paying
for. The order is fixed:

1. ``assets/icons/<slug>.<ext>`` — committed to the repository. Free, offline,
   identical on every runner. This is where every download ends up, so the
   second video that mentions a company pays nothing at all.
2. The Magnific subscription library. Free, but rationed to 100 downloads a
   day, so the quota ledger gates it.
3. A drawn monogram. Always available, never pretty, never absent.

Resolution returns a `data:` URI rather than a path because the card HTML is
screenshotted by a headless browser that must make no network or file requests
of its own — everything the page needs travels inside the page.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from ..core.log import get_logger

log = get_logger("icons")

#: Where committed marks and plates live, relative to the repository root.
#: Anything fetched lands here and is meant to be committed, so the second
#: video that needs the same thing costs nothing and needs no network.
ICON_DIR = Path("assets/icons")

#: Reusable stock the cards are built from — plates, arrows, badges. Same
#: idea as the icon bank, one folder per kind so the bank stays browsable.
STOCK_ROOT = Path("assets/stock")
STOCK_KINDS = ("plates", "arrows", "badges", "textures")

SUFFIXES = (".svg", ".png", ".webp", ".jpg", ".jpeg")

#: Icons are small; anything larger is a stock photo that wandered in.
MAX_ICON_BYTES = 512_000

#: Names whose slug does not match how the library indexes them.
ALIASES = {
    "hugging face": "huggingface",
    "hugging-face": "huggingface",
    "hf": "huggingface",
    "open ai": "openai",
    "gpt": "openai",
    "chatgpt": "openai",
    "гугл": "google",
    "гугл деепмind": "google-deepmind",
    "深度求索": "deepseek",
    "нвидиа": "nvidia",
    "нвидия": "nvidia",
    "оpenai": "openai",
    "опенай": "openai",
    "антропик": "anthropic",
    "клод": "anthropic",
    "claude": "anthropic",
    "гитхаб": "github",
    "майкрософт": "microsoft",
    "гугл клауд": "google-cloud",
}


def slugify(name: str) -> str:
    """A stable filename for an entity, Cyrillic included."""
    lowered = unicodedata.normalize("NFKC", str(name)).strip().casefold()
    if lowered in ALIASES:
        lowered = ALIASES[lowered]
    slug = re.sub(r"[^\w]+", "-", lowered, flags=re.UNICODE).strip("-")
    return slug[:64]


def data_uri(path: Path) -> str:
    """Inline a file so the rendered page needs no filesystem of its own."""
    try:
        payload = path.read_bytes()
    except OSError:
        return ""
    if not payload or len(payload) > MAX_ICON_BYTES:
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


class IconBank:
    """Resolves an entity name to something a card can draw.

    `fetch` is injected so tests and `--dry-run` never touch the network; when
    it is None the bank is local-only, which is also what happens on a runner
    with no Magnific key.
    """

    def __init__(
        self,
        root: Path | str = ".",
        *,
        fetch: Callable[[str, Path], Path | None] | None = None,
        kind: str = "",
    ):
        self.root = Path(root)
        # An empty kind is the icon bank; a named kind is a stock shelf next
        # to it, so plates and arrows accumulate the same way marks do.
        self.dir = self.root / (STOCK_ROOT / kind if kind else ICON_DIR)
        self.fetch = fetch
        self._memo: dict[str, str] = {}
        #: Names looked up this run that ended on the drawn fallback, so a
        #: report can say which logos are worth committing by hand.
        self.missing: list[str] = []

    def local_path(self, name: str) -> Path | None:
        slug = slugify(name)
        if not slug:
            return None
        for suffix in SUFFIXES:
            candidate = self.dir / f"{slug}{suffix}"
            if candidate.is_file() and candidate.stat().st_size:
                return candidate
        return None

    def resolve(self, name: str) -> str:
        """Return a data URI for `name`, or "" to let the caller draw a glyph."""
        slug = slugify(name)
        if not slug:
            return ""
        if slug in self._memo:
            return self._memo[slug]

        found = self.local_path(name)
        if found is None and self.fetch is not None:
            destination = self.dir / f"{slug}.png"
            try:
                fetched = self.fetch(name, destination)
            except Exception as exc:  # a logo is never worth failing a run over
                log.warning("icon fetch failed for %s: %s", slug, exc)
                fetched = None
            if fetched is not None and fetched.is_file():
                found = fetched

        uri = data_uri(found) if found is not None else ""
        if not uri and slug not in self.missing:
            self.missing.append(slug)
        self._memo[slug] = uri
        return uri


def magnific_fetcher(client, *, kind: str = "image") -> Callable[[str, Path], Path | None]:
    """Adapt a `MagnificClient` into the `fetch` callable `IconBank` expects.

    Queries the library the way a stock library is actually indexed — the bare
    name first, then narrowed — and takes the first asset small enough to be a
    mark rather than a photograph.
    """

    def fetch(name: str, destination: Path) -> Path | None:
        if not client.is_available():
            return None
        for query in (f"{name} logo", f"{name} icon", name):
            assets = client.search_library(query, kind=kind, limit=6)
            for asset in assets:
                if asset.width and asset.height and max(asset.width, asset.height) > 2048:
                    continue
                target = destination.with_suffix(asset.suffix)
                saved = client.download_library_asset(asset, target)
                if saved is not None:
                    log.info("icon %s ← magnific library", destination.stem)
                    return saved
        return None

    return fetch
