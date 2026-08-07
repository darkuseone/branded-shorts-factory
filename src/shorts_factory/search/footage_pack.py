"""Smart footage packing for dense Shorts edits.

Goals
-----
* Mix **video** (≈4–5s) and **photo** (≈1–3s) plates so one frame never lingers.
* Photos always get motion (kenburns / parallax / slide) — never static.
* Prefer meaning-matched local stock, Magnific Freepik staging, and news cards.
* Output a cut list that agents bake into a single HyperFrames-safe master bed.

This module is intentionally offline-friendly: it ranks already-staged files under
``jobs/<id>/broll/`` (including ``stock/`` and ``news/``). Online agents stage
Magnific stock / OG stills into those folders first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}

# Soft targets for Aleko-style pacing.
PHOTO_DUR = (1.2, 2.8)
VIDEO_DUR = (3.6, 5.0)

PHOTO_MOTIONS = ("kenburns", "parallax", "zoom_in", "pan_left", "pan_right")


@dataclass(frozen=True)
class FootageCut:
    """One plate in the Action / fullscreen bed."""

    path: str
    start: float
    duration: float
    kind: str  # photo | video | host_full | host_split
    motion: str
    theme: str
    layout: str  # fullscreen | split_top | host_full | host_bottom


def clamp_duration(kind: str, duration: float) -> float:
    lo, hi = PHOTO_DUR if kind == "photo" else VIDEO_DUR
    return max(lo, min(hi, float(duration)))


def motion_for_photo(index: int, preferred: str | None = None) -> str:
    if preferred and preferred in PHOTO_MOTIONS:
        return preferred
    return PHOTO_MOTIONS[index % len(PHOTO_MOTIONS)]


def _stem_tokens(path: Path) -> set[str]:
    raw = path.stem.lower().replace("-", "_").replace(" ", "_")
    return {t for t in raw.split("_") if t}


def score_asset(path: Path, themes: Iterable[str]) -> float:
    """Higher is better meaning match against theme keywords."""
    tokens = _stem_tokens(path)
    score = 0.0
    for theme in themes:
        for word in theme.lower().replace("-", " ").split():
            if len(word) < 3:
                continue
            if word in tokens or any(word in t for t in tokens):
                score += 2.0
            elif word[:4] in "".join(tokens):
                score += 0.5
    # Prefer larger / sharper looking files lightly.
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
        score += min(2.0, size_mb / 8.0)
    except OSError:
        pass
    if path.suffix.lower() in VIDEO_EXTS:
        score += 0.25
    return score


def list_staged_assets(broll_dir: Path) -> list[Path]:
    """Collect usable staged stills/videos (skip masters / tiny stubs)."""
    skip_stems = {
        "aleko-master",
        "aleko_master",
        "action-bed",
        "action_bed",
        "broll",
        "master",
        "v1_clip",
        "black",
    }
    found: list[Path] = []
    for path in sorted(broll_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.stem.lower() in skip_stems:
            continue
        if path.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS:
            continue
        if path.stat().st_size < 32_768:
            continue
        # Prefer stock/news plates; deprioritize tiny cached graphics later via score.
        found.append(path)
    return found


def pick_for_themes(
    assets: list[Path],
    themes: list[str],
    *,
    used: set[str] | None = None,
    prefer: str | None = None,
    allow_reuse: bool = False,
) -> Path | None:
    used = used or set()
    pool = list(assets) if allow_reuse else [a for a in assets if str(a) not in used]
    ranked = sorted(pool, key=lambda p: score_asset(p, themes), reverse=True)
    if prefer in {"photo", "video"}:
        want = IMAGE_EXTS if prefer == "photo" else VIDEO_EXTS
        typed = [p for p in ranked if p.suffix.lower() in want]
        if typed:
            return typed[0]
    return ranked[0] if ranked else None


def build_dense_cut_list(
    *,
    broll_dir: Path,
    duration: float = 42.0,
    host_windows: list[tuple[float, float, str]] | None = None,
) -> list[FootageCut]:
    """Build a dense Aleko cut list with frequent photo/video swaps.

    ``host_windows`` entries are ``(start, end, layout)`` where layout is
    ``host_full`` or ``split``. Non-host gaps are filled with short plates.
    """
    assets = list_staged_assets(broll_dir)
    host_windows = host_windows or [
        (0.0, 2.2, "host_full"),
        (8.2, 10.6, "split"),
        (19.0, 21.4, "split"),
        (30.2, 32.4, "split"),
        (36.5, 42.0, "host_full"),
    ]

    # Theme packs mapped roughly to script beats.
    beat_themes: list[tuple[float, float, list[str]]] = [
        (2.2, 8.2, ["breach", "soc", "news", "ransomware", "server", "neon", "hacker", "hf"]),
        (10.6, 19.0, ["ai", "openai", "access", "exploit", "network", "mind", "code", "zeroday"]),
        (21.4, 30.2, ["dataset", "cheat", "news", "ai_server", "brute", "newspaper", "hud"]),
        (32.4, 36.5, ["swarm", "neon", "hacker", "mind", "threat", "binary"]),
    ]

    cuts: list[FootageCut] = []
    used: set[str] = set()
    photo_i = 0

    def add_host(start: float, end: float, layout: str) -> None:
        dur = max(0.4, end - start)
        cuts.append(
            FootageCut(
                path="",
                start=start,
                duration=dur,
                kind="host_full" if layout == "host_full" else "host_split",
                motion="none",
                theme="host",
                layout="host_full" if layout == "host_full" else "host_bottom",
            )
        )

    def fill_gap(start: float, end: float, themes: list[str]) -> None:
        nonlocal photo_i
        t = start
        alternate_photo = True
        while t < end - 0.25:
            remaining = end - t
            if remaining < 1.05:
                break
            prefer = "photo" if alternate_photo else "video"
            asset = pick_for_themes(assets, themes, used=used, prefer=prefer)
            if asset is None:
                asset = pick_for_themes(assets, themes, used=used)
            if asset is None:
                # Reuse photos with a new motion rather than leave a hole.
                asset = pick_for_themes(assets, themes, prefer="photo", allow_reuse=True)
            if asset is None:
                break
            kind = "photo" if asset.suffix.lower() in IMAGE_EXTS else "video"
            if kind == "photo":
                dur = min(remaining, 2.0 if remaining > 2.5 else max(1.15, remaining))
                motion = motion_for_photo(photo_i)
                photo_i += 1
            else:
                # Keep video plates snappy so the edit keeps cutting.
                dur = min(remaining, 4.2 if remaining >= 4.0 else max(2.6, remaining))
                motion = "none"
            dur = min(dur, remaining)
            if kind == "video" and dur < 2.4:
                photo = pick_for_themes(assets, themes, used=used, prefer="photo") or pick_for_themes(
                    assets, themes, prefer="photo", allow_reuse=True
                )
                if photo is not None:
                    asset = photo
                    kind = "photo"
                    motion = motion_for_photo(photo_i)
                    photo_i += 1
                    dur = min(remaining, 1.8)
            used.add(str(asset))
            cuts.append(
                FootageCut(
                    path=str(asset),
                    start=t,
                    duration=dur,
                    kind=kind,
                    motion=motion,
                    theme=",".join(themes[:3]),
                    layout="fullscreen",
                )
            )
            t += dur
            alternate_photo = not alternate_photo

    cursor = 0.0
    for hs, he, layout in sorted(host_windows, key=lambda x: x[0]):
        if hs > cursor:
            themes = ["cyber", "ai", "news"]
            for a, b, th in beat_themes:
                if a < hs and b > cursor:
                    themes = th
                    break
            fill_gap(cursor, hs, themes)
        add_host(hs, he, layout)
        cursor = he
    if cursor < duration:
        fill_gap(cursor, duration, ["cyber", "ai", "news"])

    cuts.sort(key=lambda c: c.start)
    return cuts


def cuts_to_json(cuts: list[FootageCut]) -> list[dict[str, Any]]:
    return [asdict(c) for c in cuts]
