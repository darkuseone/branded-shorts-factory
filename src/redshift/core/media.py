"""FFmpeg utilities and the free measurements L0 runs on (§9.5, §19.E1).

Everything here is deterministic and costs nothing, which is the whole point:
the numbers computed in this module let the critic throw away 50–70% of
candidates before a single vision token is spent.

Frames are pulled through ffmpeg as raw bytes and measured in plain Python. At
64×64 (hash) and 160×284 (stats) that is microseconds, and it keeps the runtime
free of numpy/Pillow.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .log import get_logger

log = get_logger("media")

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"})

HASH_SIZE = 8  # 8x8 grayscale → 64-bit average hash
STATS_W, STATS_H = 160, 284  # 9:16-ish, enough for sharpness and colour stats

#: Brand-safe hue windows in degrees: crimson, deep blue, cyan accent.
BRAND_HUES = ((330.0, 360.0), (0.0, 20.0), (200.0, 260.0), (170.0, 200.0))


@dataclass
class Probe:
    path: Path
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    has_audio: bool = False
    codec: str = ""

    @property
    def is_video(self) -> bool:
        return self.duration > 0.05 and self.path.suffix.lower() not in IMAGE_SUFFIXES

    @property
    def short_side(self) -> int:
        return min(self.width, self.height) if self.width and self.height else 0


@dataclass
class Measurements:
    """Everything §9.5 requires, computed locally."""

    phash: str = ""
    motion_score: float = 0.0
    sharpness: float = 0.0
    acid_share: float = 0.0
    colors: list[str] = field(default_factory=list)
    mean_luma: float = 0.0


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(command: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, capture_output=True, timeout=timeout, check=False)


def probe(path: Path, ffprobe: str = "ffprobe") -> Probe | None:
    """Inspect a media file. `None` when ffprobe is unavailable or refuses it."""
    path = Path(path)
    if not path.exists() or not have(ffprobe):
        return None
    result = _run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=60,
    )
    if result.returncode != 0:
        log.debug("ffprobe rejected %s", path.name, extra={"stage": "media"})
        return None
    try:
        payload = json.loads(result.stdout or b"{}")
    except json.JSONDecodeError:
        return None

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _float((payload.get("format") or {}).get("duration"))
    if not duration and video:
        duration = _float(video.get("duration"))
    return Probe(
        path=path,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        duration=duration,
        fps=_fraction((video or {}).get("avg_frame_rate")),
        has_audio=audio is not None,
        codec=str((video or {}).get("codec_name") or ""),
    )


def grab_gray(path: Path, at: float, size: tuple[int, int], ffmpeg: str = "ffmpeg") -> bytes | None:
    """One greyscale frame as raw bytes, `size[0] * size[1]` long."""
    if not have(ffmpeg):
        return None
    width, height = size
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *(["-ss", f"{at:.3f}"] if at > 0 else []),
        "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={width}:{height},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]  # fmt: skip
    result = _run(command)
    data = result.stdout
    return data if len(data) >= width * height else None


def grab_rgb(path: Path, at: float, size: tuple[int, int], ffmpeg: str = "ffmpeg") -> bytes | None:
    if not have(ffmpeg):
        return None
    width, height = size
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *(["-ss", f"{at:.3f}"] if at > 0 else []),
        "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]  # fmt: skip
    result = _run(command)
    data = result.stdout
    return data if len(data) >= width * height * 3 else None


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #


def average_hash(frame: bytes, size: int = HASH_SIZE) -> str:
    """64-bit average hash. Cheap, and plenty for near-duplicate detection."""
    pixels = frame[: size * size]
    if len(pixels) < size * size:
        return ""
    mean = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value >= mean:
            bits |= 1 << index
    return f"{bits:016x}"


def hamming(left: str, right: str) -> int:
    """Distance between two hashes; 64 (max) when either is missing."""
    if not left or not right or len(left) != len(right):
        return 64
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def laplacian_variance(frame: bytes, width: int, height: int) -> float:
    """Sharpness proxy: variance of a 4-neighbour Laplacian."""
    if len(frame) < width * height or width < 3 or height < 3:
        return 0.0
    values: list[float] = []
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1, 2):  # every other column: same signal, half the work
            index = row + x
            values.append(
                float(
                    4 * frame[index]
                    - frame[index - 1]
                    - frame[index + 1]
                    - frame[index - width]
                    - frame[index + width]
                )
            )
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def frame_difference(first: bytes, second: bytes) -> float:
    """Mean absolute difference of two greyscale frames, normalised to 0..1."""
    length = min(len(first), len(second))
    if not length:
        return 0.0
    total = sum(abs(first[i] - second[i]) for i in range(0, length, 3))
    return min(1.0, (total / (length / 3)) / 255.0)


def acid_share(rgb: bytes) -> tuple[float, list[str]]:
    """Share of loud pixels outside the brand hues, plus a few dominant colours.

    The brandbook bans acid palettes (§10.1); this is how that ban is enforced
    without a model looking at the frame.
    """
    if len(rgb) < 3:
        return 0.0, []
    loud = 0
    total = 0
    buckets: dict[tuple[int, int, int], int] = {}
    for index in range(0, len(rgb) - 2, 3 * 7):  # sample every 7th pixel
        r, g, b = rgb[index], rgb[index + 1], rgb[index + 2]
        total += 1
        buckets[(r // 32, g // 32, b // 32)] = buckets.get((r // 32, g // 32, b // 32), 0) + 1
        high, low = max(r, g, b), min(r, g, b)
        saturation = (high - low) / high if high else 0.0
        if saturation > 0.85 and not _hue_in_brand(_hue(r, g, b, high, low)):
            loud += 1
    dominant = sorted(buckets.items(), key=lambda item: -item[1])[:5]
    colors = [f"#{r * 32:02x}{g * 32:02x}{b * 32:02x}" for (r, g, b), _ in dominant]
    return (loud / total if total else 0.0), colors


def measure(path: Path, probe_result: Probe | None = None, ffmpeg: str = "ffmpeg") -> Measurements:
    """Run every free measurement on one asset."""
    info = probe_result or probe(path, ffmpeg.replace("ffmpeg", "ffprobe"))
    duration = info.duration if info else 0.0
    mid = duration * 0.4 if duration > 0.2 else 0.0

    hash_frame = grab_gray(path, mid, (HASH_SIZE, HASH_SIZE), ffmpeg)
    stats_frame = grab_gray(path, mid, (STATS_W, STATS_H), ffmpeg)
    rgb_frame = grab_rgb(path, mid, (STATS_W // 2, STATS_H // 2), ffmpeg)

    motion = 0.0
    if duration > 0.6:
        later = grab_gray(path, min(duration * 0.7, duration - 0.05), (STATS_W, STATS_H), ffmpeg)
        if stats_frame and later:
            motion = frame_difference(stats_frame, later)

    acid, colors = acid_share(rgb_frame) if rgb_frame else (0.0, [])
    return Measurements(
        phash=average_hash(hash_frame) if hash_frame else "",
        motion_score=round(motion, 4),
        sharpness=round(laplacian_variance(stats_frame, STATS_W, STATS_H), 2) if stats_frame else 0.0,
        acid_share=round(acid, 4),
        colors=colors,
        mean_luma=round(sum(stats_frame) / len(stats_frame) / 255.0, 4) if stats_frame else 0.0,
    )


# --------------------------------------------------------------------------- #
# Contact sheet — the single biggest token saving in the project (§19.E1)
# --------------------------------------------------------------------------- #


def contact_sheet(
    paths: list[Path],
    destination: Path,
    *,
    grid: tuple[int, int] = (3, 3),
    tile: tuple[int, int] = (360, 640),
    jpeg_q: int = 72,
    frame_at: float = 0.40,
    ffmpeg: str = "ffmpeg",
) -> Path | None:
    """Nine candidates on one numbered sheet — one vision call instead of nine."""
    if not paths or not have(ffmpeg):
        return None
    cols, rows = grid
    chosen = paths[: cols * rows]
    destination.parent.mkdir(parents=True, exist_ok=True)

    command: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for path in chosen:
        info = probe(path, ffmpeg.replace("ffmpeg", "ffprobe"))
        seek = (info.duration * frame_at) if info and info.duration > 0.3 else 0.0
        if seek > 0:
            command += ["-ss", f"{seek:.3f}"]
        command += ["-i", str(path)]

    filters = []
    for index in range(len(chosen)):
        # The number must survive downscaling — the model reads it to answer.
        filters.append(
            f"[{index}:v]scale={tile[0]}:{tile[1]}:force_original_aspect_ratio=increase,"
            f"crop={tile[0]}:{tile[1]},"
            f"drawbox=x=0:y=0:w=96:h=76:color=black@0.65:t=fill,"
            f"drawtext=text='{index + 1}':x=26:y=8:fontsize=64:fontcolor=white[t{index}]"
        )
    inputs = "".join(f"[t{i}]" for i in range(len(chosen)))
    filters.append(f"{inputs}xstack=inputs={len(chosen)}:layout={_xstack_layout(len(chosen), cols)}[out]")

    command += ["-filter_complex", ";".join(filters), "-map", "[out]", "-frames:v", "1"]
    command += ["-q:v", str(max(2, min(31, jpeg_q // 3))), str(destination)]

    result = _run(command, timeout=180)
    if result.returncode != 0 or not destination.exists():
        log.warning(
            "contact sheet failed: %s",
            result.stderr.decode("utf-8", "replace").strip()[:200],
            extra={"stage": "media"},
        )
        return None
    return destination


def _xstack_layout(count: int, cols: int) -> str:
    """`0_0|w0_0|w0+w1_0|…` — xstack wants explicit cell offsets."""
    cells = []
    for index in range(count):
        col, row = index % cols, index // cols
        x = "0" if col == 0 else "+".join(f"w{i}" for i in range(col))
        y = "0" if row == 0 else "+".join(f"h{i * cols}" for i in range(row))
        cells.append(f"{x}_{y}")
    return "|".join(cells)


def scene_cuts(path: Path, threshold: float = 0.30, ffmpeg: str = "ffmpeg") -> list[float]:
    """Detect cuts, for the rhythm QC checks (Q06-Q08) and reference analysis."""
    if not have(ffmpeg):
        return []
    result = _run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-filter:v",
         f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        timeout=600,
    )  # fmt: skip
    cuts: list[float] = []
    for line in result.stderr.decode("utf-8", "replace").splitlines():
        if "pts_time:" not in line:
            continue
        try:
            cuts.append(float(line.split("pts_time:")[1].split()[0]))
        except (IndexError, ValueError):
            continue
    return cuts


def _hue(r: int, g: int, b: int, high: int, low: int) -> float:
    if high == low:
        return 0.0
    delta = high - low
    if high == r:
        hue = ((g - b) / delta) % 6
    elif high == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    return hue * 60.0


def _hue_in_brand(hue: float) -> bool:
    return any(low <= hue <= high for low, high in BRAND_HUES)


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _fraction(value: object) -> float:
    text = str(value or "")
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    return _float(value)
