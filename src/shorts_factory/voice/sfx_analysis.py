"""Measure the sound bank so placement can be driven by shape, not filenames.

A folder of sounds tells you almost nothing from its filenames: `thumbpiano_ab_1`
and `35913__altemark__bd` carry no hint of what they are for, and a file called
`air-effect` turns out to be two different things depending on where its energy
sits. What *does* carry that information is the waveform — when the energy
peaks, how fast it gets there, how long it takes to die, and where it sits in
the spectrum. This module measures those with FFmpeg and turns them into roles
the placement engine can ask for by name.

Two of the measurements change the edit audibly:

* ``peak_at`` — when the loudest moment happens. Starting a sound *on* a cut
  lands the audible hit late whenever the attack is slow, which is most whooshes;
  the placement engine subtracts this value so the hit lands on the frame.
* ``lufs`` — how loud the file really is. A hand-collected bank easily spans
  20 dB, so without per-file normalisation some sounds bury the narration and
  others disappear under it.

Everything degrades quietly: with no FFmpeg the scan yields nothing and the
library falls back to filename tags, exactly as it did before.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_utils import get_logger
from ..media.ffmpeg import is_available

log = get_logger("sfx_analysis")

#: Envelope resolution. 1024 samples at 44.1 kHz is 23.2 ms — short enough to
#: catch a percussive attack, long enough that the RMS reading stays stable.
WINDOW_SAMPLES = 1024
ANALYSIS_RATE = 44_100
WINDOW = WINDOW_SAMPLES / ANALYSIS_RATE

#: A window this far below the peak counts as "not sounding yet" (onset search)
#: and "effectively over" (tail search).
ONSET_DROP_DB = 20.0
TAIL_DROP_DB = 40.0

#: Below this peak-to-median spread a sound has no shape — it is a drone or a
#: music bed, and it must never be used as a montage accent.
FLAT_SPREAD_DB = 8.0
#: Length past which a shapeless sound is a music bed rather than a room tone.
BED_SECONDS = 8.0
#: Nothing shorter than this can be a drone, however flat it measures: a 0.2 s
#: clap has no room to be atmosphere.
DRONE_MIN_SECONDS = 1.5
#: A rise longer than this is a track, not an accent — the montage would have to
#: trim away most of it, and the trim would take the musical shape with it.
MAX_RISER_SECONDS = 6.0

#: Target loudness per role, in LUFS, so a hand-collected bank spanning 20 dB
#: lands at one consistent level under the narration.
ROLE_TARGET_LUFS = {
    "impact": -16.0,
    "thump": -19.0,
    "sub_drop": -17.0,
    "whoosh": -18.0,
    "swoosh": -18.0,
    "transition": -18.0,
    "riser": -20.0,
    "pop": -20.0,
    "click": -22.0,
    "ui": -22.0,
    "glitch": -21.0,
    "power_down": -22.0,
    "power_up": -22.0,
    "host_exit": -22.0,
    "host_enter": -22.0,
    "ambience": -30.0,
}
DEFAULT_TARGET_LUFS = -19.0
#: Roles the placement engine asks for on a normal video. A gap here is not an
#: error — it just means that beat falls through to generation.
REQUIRED_ROLES = (
    "impact",
    "thump",
    "whoosh",
    "transition",
    "riser",
    "pop",
    "click",
    "ui",
    "glitch",
    "sub_drop",
    "power_down",
    "power_up",
)
#: Headroom kept below 0 dBFS after gain so a limiter never has to rescue the mix.
PEAK_CEILING_DBTP = -1.0

#: Spectral centroid boundaries, in Hz, for the four bands the roles care about.
BAND_EDGES = ((300.0, "sub"), (1_200.0, "low"), (4_000.0, "mid"))

_METADATA_RE = re.compile(r"^lavfi\.(?P<key>[\w.]+)=(?P<value>-?[\d.]+|-?inf|nan)$", re.MULTILINE)
_SUMMARY_RE = re.compile(
    r"^\s*Input (?P<field>Integrated|True Peak|LRA):\s*(?P<value>-?[\d.]+|-?inf)",
    re.MULTILINE,
)


@dataclass
class AudioProfile:
    """What one file in the bank actually sounds like."""

    path: Path
    duration: float = 0.0
    lufs: float = 0.0
    peak_dbtp: float = 0.0
    lra: float = 0.0
    #: Seconds from file start to the loudest window — the alignment offset.
    peak_at: float = 0.0
    #: Seconds from the sound becoming audible to its peak.
    attack: float = 0.0
    #: Seconds from the peak until it has decayed by 40 dB.
    tail: float = 0.0
    #: Peak level minus median level, in dB. Low means "no shape".
    spread: float = 0.0
    shape: str = "unknown"
    band: str = "mid"
    centroid: float = 0.0
    roles: list[str] = field(default_factory=list)
    #: True when `lufs` came from the envelope instead of a gated loudness
    #: measurement, which happens for anything shorter than about 3 seconds.
    lufs_estimated: bool = False

    @property
    def usable_as_accent(self) -> bool:
        """Drones and music beds are excluded from montage accents by design."""
        return self.shape not in {"drone", "bed"} and bool(self.roles)

    def to_index_entry(self) -> dict[str, object]:
        """One `items[]` row for `assets/sfx/index.json`.

        The keys the existing library reader understands (`file`, `tags`,
        `title`, `duration`) come first; the measurements ride along under
        `analysis` so a hand-edited index keeps working untouched.
        """
        return {
            "file": self.path.name,
            "title": self.path.stem,
            "duration": round(self.duration, 3),
            "tags": list(self.roles),
            "analysis": {
                "lufs": round(self.lufs, 1),
                "peak_dbtp": round(self.peak_dbtp, 1),
                "lra": round(self.lra, 1),
                "peak_at": round(self.peak_at, 3),
                "attack": round(self.attack, 3),
                "tail": round(self.tail, 3),
                "spread": round(self.spread, 1),
                "shape": self.shape,
                "band": self.band,
                "centroid": round(self.centroid, 1),
                "lufs_estimated": self.lufs_estimated,
            },
        }


# --------------------------------------------------------------------------- #
# Classification — pure functions, so the rules are testable without FFmpeg
# --------------------------------------------------------------------------- #


def describe_envelope(levels: Sequence[float], duration: float) -> dict[str, float]:
    """Reduce an RMS envelope to the handful of numbers the rules use.

    ``levels`` is a list of per-window RMS readings in dBFS, oldest first.
    Silent windows arrive as ``-inf`` from FFmpeg and are floored so the
    statistics stay finite.
    """
    finite = [level for level in levels if level > -120.0]
    if not finite:
        return {"peak_at": 0.0, "attack": 0.0, "tail": duration, "spread": 0.0, "rise": 0.0}

    window = duration / len(levels) if levels and duration > 0 else WINDOW
    peak = max(finite)
    peak_index = max(range(len(levels)), key=lambda index: levels[index])

    onset_index = peak_index
    for index in range(peak_index, -1, -1):
        if levels[index] < peak - ONSET_DROP_DB:
            break
        onset_index = index

    tail_index = len(levels) - 1
    for index in range(peak_index, len(levels)):
        if levels[index] < peak - TAIL_DROP_DB:
            tail_index = index
            break

    # How much of the file is spent getting louder. A riser is nearly all rise;
    # a one-shot is nearly none.
    return {
        "peak_at": peak_index * window,
        "attack": max(0.0, (peak_index - onset_index) * window),
        "tail": max(0.0, (tail_index - peak_index) * window),
        "spread": peak - statistics.median(finite),
        "rise": peak_index / max(1, len(levels) - 1),
    }


def classify_shape(*, duration: float, peak_at: float, attack: float, spread: float, rise: float) -> str:
    """Name the gesture a sound makes: `oneshot`, `swell`, `riser`, `drone`, `bed`.

    The one rule worth spelling out separates a long cinematic effect from a
    piece of music, because both are long and both have dynamics. A cinematic
    hit peaks early and then decays for the rest of the file; a track keeps
    building and peaks late. So for anything past `BED_SECONDS`, a late peak
    means music.
    """
    if duration <= 0:
        return "unknown"
    if duration >= BED_SECONDS and rise >= 0.5:
        return "bed"
    if duration >= DRONE_MIN_SECONDS and spread < FLAT_SPREAD_DB:
        return "bed" if duration >= BED_SECONDS else "drone"

    position = peak_at / duration
    if position <= 0.18 and attack <= 0.15:
        return "oneshot"
    if rise >= 0.6:
        return "riser" if duration <= MAX_RISER_SECONDS else "bed"
    if position < 0.6:
        return "swell"
    return "riser" if duration <= MAX_RISER_SECONDS else "bed"


def estimate_level(levels: Sequence[float]) -> float:
    """Approximate level, in dB, from the envelope alone.

    `loudnorm` needs about three seconds of material before its gated
    measurement means anything, and most of a sound bank is shorter than that —
    it reports `-inf` and would make every percussive hit look silent. Averaging
    the power of the windows that are actually sounding gives a usable stand-in:
    not a true LUFS reading, but consistent between files, which is all the
    normalisation step needs.
    """
    sounding = [level for level in levels if level > -60.0]
    if not sounding:
        return -70.0
    peak = max(sounding)
    # Only the part of the sound that carries it, so a long silent tail cannot
    # drag a loud hit down into looking quiet.
    body = [level for level in sounding if level >= peak - 25.0]
    power = sum(10.0 ** (level / 10.0) for level in body) / len(body)
    return 10.0 * math.log10(power) if power > 0 else -70.0


def gain_db_for(profile: AudioProfile, role: str) -> float:
    """How much to lift or cut this file so `role` sits at its target level.

    Clamped by the file's own true peak: a sound already touching 0 dBFS is
    never pushed further, whatever the target says.
    """
    target = ROLE_TARGET_LUFS.get(role, DEFAULT_TARGET_LUFS)
    wanted = target - profile.lufs
    headroom = PEAK_CEILING_DBTP - profile.peak_dbtp
    return round(max(-24.0, min(wanted, headroom, 18.0)), 1)


def classify_band(centroid: float) -> str:
    """Bucket the spectral centre of mass. Drives sub-vs-tick role choices."""
    for edge, name in BAND_EDGES:
        if centroid < edge:
            return name
    return "high"


def roles_for(*, shape: str, band: str, duration: float, tail: float, position: float = 0.0) -> list[str]:
    """Which `audio_fx` types this sound can serve.

    Deliberately generous where it is harmless (a bass drum is a fine `impact`
    *and* a fine `thump`) and strict where it is not: nothing shapeless is ever
    offered as an accent, because a 6-second air-conditioner hum placed on a cut
    reads as a mistake rather than as design.
    """
    if shape in {"drone", "bed"}:
        return ["ambience"] if shape == "drone" else []

    roles: list[str] = []
    if shape == "oneshot":
        # Weight is what makes an impact read as an impact, so only the bottom
        # of the spectrum is offered for one. A bright tonal note with a long
        # ring — a kalimba, a bell — is a UI blip no matter how long it sustains.
        if band in {"sub", "low"}:
            roles += ["impact", "thump"]
            if tail >= 1.0:
                roles.append("sub_drop")
        elif duration <= 0.25:
            roles += ["click", "ui", "power_down", "power_up"]
        else:
            roles += ["pop", "ui", "click"]
            if band == "high":
                roles.append("glitch")
    elif shape == "swell":
        roles += ["whoosh", "swoosh", "transition"]
        if band in {"sub", "low"}:
            roles.append("sub_drop")
        # Placement aligns by peak, so a swell that keeps climbing past its
        # midpoint can carry a pre-climax build as well as a transition — it
        # just gets started earlier.
        if duration >= 2.0 and position >= 0.4:
            roles.append("riser")
        # Something that hits and then rings out for seconds is a cinematic
        # braam: the one sound big enough to open a video on.
        if tail >= 2.5:
            roles.append("impact")
    elif shape == "riser":
        roles += ["riser", "transition"]
        if duration <= 2.5:
            roles.append("swoosh")

    # A long tail is usable but only once trimmed, which the placement engine
    # does; flag it so reports can explain the edit.
    if tail >= 2.5:
        roles.append("long_tail")
    return list(dict.fromkeys(roles))


def profile_from_metrics(
    path: Path,
    *,
    duration: float,
    lufs: float,
    peak_dbtp: float,
    lra: float,
    levels: Sequence[float],
    centroid: float,
) -> AudioProfile:
    """Assemble a profile from raw measurements. The FFmpeg-free seam for tests."""
    estimated = lufs <= -70.0
    if estimated:
        lufs = estimate_level(levels)
    envelope = describe_envelope(levels, duration)
    shape = classify_shape(
        duration=duration,
        peak_at=envelope["peak_at"],
        attack=envelope["attack"],
        spread=envelope["spread"],
        rise=envelope["rise"],
    )
    band = classify_band(centroid)
    return AudioProfile(
        path=path,
        duration=duration,
        lufs=lufs,
        peak_dbtp=peak_dbtp,
        lra=lra,
        peak_at=envelope["peak_at"],
        attack=envelope["attack"],
        tail=envelope["tail"],
        spread=envelope["spread"],
        shape=shape,
        band=band,
        centroid=centroid,
        roles=roles_for(
            shape=shape,
            band=band,
            duration=duration,
            tail=envelope["tail"],
            position=envelope["peak_at"] / duration if duration > 0 else 0.0,
        ),
        lufs_estimated=estimated,
    )


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


def _run(command: list[str], timeout: float = 120.0) -> str:
    """Run FFmpeg and return stdout+stderr. Empty string on any failure."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("ffmpeg call failed: %s", exc)
        return ""
    return f"{completed.stdout}\n{completed.stderr}"


def _loudness(path: Path, ffmpeg: str) -> tuple[float, float, float]:
    """Integrated loudness, true peak and range, via `loudnorm`'s own analysis."""
    output = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=print_format=summary",
            "-f",
            "null",
            "-",
        ]
    )
    found = {match.group("field"): match.group("value") for match in _SUMMARY_RE.finditer(output)}
    return (
        _to_float(found.get("Integrated"), -70.0),
        _to_float(found.get("True Peak"), 0.0),
        _to_float(found.get("LRA"), 0.0),
    )


def _envelope_and_centroid(path: Path, ffmpeg: str) -> tuple[list[float], float]:
    """RMS per fixed window plus the median spectral centroid, in one pass.

    `asetnsamples` pins the window length so the envelope means the same thing
    for every file regardless of its native block size.
    """
    chain = (
        f"aresample={ANALYSIS_RATE},asetnsamples=n={WINDOW_SAMPLES},"
        "astats=metadata=1:reset=1,aspectralstats=measure=centroid,"
        "ametadata=print:file=-"
    )
    output = _run([ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-af", chain, "-f", "null", "-"])

    levels: list[float] = []
    centroids: list[float] = []
    for match in _METADATA_RE.finditer(output):
        key, raw = match.group("key"), match.group("value")
        if key.endswith("astats.Overall.RMS_level"):
            levels.append(_to_float(raw, -120.0))
        elif ".centroid" in key:
            value = _to_float(raw, 0.0)
            if value > 0:
                centroids.append(value)
    return levels, (statistics.median(centroids) if centroids else 0.0)


def analyze(path: Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> AudioProfile | None:
    """Measure one file. ``None`` when FFmpeg is unavailable or the file is unreadable."""
    if not path.exists() or not is_available(ffmpeg):
        return None

    from ..media.ffmpeg import probe  # local import keeps the media layer optional

    info = probe(path, ffprobe)
    duration = info.duration if info and info.duration else 0.0
    levels, centroid = _envelope_and_centroid(path, ffmpeg)
    if not levels:
        log.debug("no envelope for %s; skipping", path.name)
        return None
    if duration <= 0:
        duration = len(levels) * WINDOW

    lufs, peak_dbtp, lra = _loudness(path, ffmpeg)
    return profile_from_metrics(
        path,
        duration=duration,
        lufs=lufs,
        peak_dbtp=peak_dbtp,
        lra=lra,
        levels=levels,
        centroid=centroid,
    )


def scan(
    directory: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    workers: int = 8,
    suffixes: frozenset[str] | None = None,
) -> list[AudioProfile]:
    """Measure every audio file in a folder, in parallel."""
    from ..assets.library import AUDIO_SUFFIXES

    wanted = suffixes or AUDIO_SUFFIXES
    if not directory.is_dir():
        log.warning("sfx folder missing: %s", directory)
        return []

    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in wanted)
    if not files:
        return []
    if not is_available(ffmpeg):
        log.warning("ffmpeg unavailable; cannot analyse the sound bank")
        return []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda p: analyze(p, ffmpeg=ffmpeg, ffprobe=ffprobe), files))
    return [profile for profile in results if profile is not None]


def write_index(directory: Path, profiles: list[AudioProfile]) -> Path:
    """Write `index.json` next to the sounds, preserving hand-written titles."""
    target = directory / "index.json"
    existing: dict[str, dict] = {}
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            for entry in (raw.get("items") if isinstance(raw, dict) else raw) or []:
                if isinstance(entry, dict) and entry.get("file"):
                    existing[str(entry["file"])] = entry
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("existing index.json unreadable, rewriting: %s", exc)

    items = []
    for profile in profiles:
        entry = profile.to_index_entry()
        previous = existing.get(profile.path.name, {})
        # A human-chosen title or extra tags always win over the measurement.
        if previous.get("title") and previous["title"] != profile.path.stem:
            entry["title"] = previous["title"]
        extra = [tag for tag in previous.get("tags", []) if tag not in entry["tags"]]
        if extra:
            entry["tags"] = list(entry["tags"]) + extra
            entry["manual_tags"] = extra
        items.append(entry)

    payload = {
        "_comment": (
            "Generated by `python -m shorts_factory sfxscan`. Tags come from measured shape and "
            "spectrum, not filenames. Hand-added tags and titles are preserved on rescan."
        ),
        "items": items,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def coverage(profiles: list[AudioProfile], wanted: Sequence[str]) -> dict[str, int]:
    """How many bank sounds can serve each requested role. Zero means a gap."""
    return {role: sum(1 for profile in profiles if role in profile.roles) for role in wanted}


def _to_float(value: object, default: float) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return default if result != result else result  # NaN guard
