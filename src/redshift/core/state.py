"""Run state machine and checkpoints (§4.1, §20).

The contract that makes `resume` possible: a stage writes its artifact first,
then records its own hash. On the next run, a stage whose hash matches and whose
artifact is still valid is skipped entirely — no budget spent, no network.

The hash is `sha256(input artifact + prompt version + config version)`, so a
changed prompt or a changed config invalidates the stage, but re-running the
same pipeline twice does not.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .log import get_logger

log = get_logger("state")

Status = Literal["pending", "running", "done", "failed", "skipped", "degraded"]

STAGES = (
    "s00_topic_scout",
    "s01_research",
    "s02_script",
    "s03_shotlist",
    "s04_asset_hunter",
    "s05_critic",
    "s06_gapfill",
    "s07_meme_director",
    "s08_infographic",
    "s09_voice_avatar",
    "s10_timeline",
    "s11_render",
    "s12_qc",
    "s13_publish",
)

MAX_ATTEMPTS = 3


@dataclass
class StageState:
    status: Status = "pending"
    hash: str = ""
    artifact: str = ""
    cost: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    error: str = ""
    traceback: str = ""
    partial: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", 0, 0.0, {}, None)}


@dataclass
class RunState:
    """`runs/<run_id>/state.json`."""

    run_id: str
    topic_id: str = ""
    language: str = "ru"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    stages: dict[str, StageState] = field(default_factory=dict)
    budget_used: dict[str, int] = field(default_factory=dict)
    degradation_level: int = 0
    assets_locked: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    notes: list[str] = field(default_factory=list)

    # -- persistence --------------------------------------------------------

    @classmethod
    def path_for(cls, runs_dir: Path, run_id: str) -> Path:
        return Path(runs_dir) / run_id / "state.json"

    @classmethod
    def load(cls, runs_dir: Path, run_id: str) -> RunState:
        path = cls.path_for(runs_dir, run_id)
        if not path.exists():
            return cls(run_id=run_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("state.json unreadable (%s); starting a fresh state", exc)
            return cls(run_id=run_id)

        stages = {
            name: StageState(**{k: v for k, v in data.items() if k in StageState.__dataclass_fields__})
            for name, data in (raw.get("stages") or {}).items()
        }
        known = {f for f in cls.__dataclass_fields__ if f != "stages"}
        return cls(stages=stages, **{k: v for k, v in raw.items() if k in known})

    def save(self, runs_dir: Path) -> Path:
        """Atomic: tmp file + os.replace, never a half-written state (§20.1)."""
        self.updated_at = time.time()
        path = self.path_for(runs_dir, self.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "topic_id": self.topic_id,
            "language": self.language,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stages": {name: stage.to_dict() for name, stage in self.stages.items()},
            "budget_used": self.budget_used,
            "degradation_level": self.degradation_level,
            "assets_locked": self.assets_locked,
            "needs_human_review": self.needs_human_review,
            "notes": self.notes,
        }
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    # -- stage bookkeeping --------------------------------------------------

    def stage(self, name: str) -> StageState:
        return self.stages.setdefault(name, StageState())

    def is_done(self, name: str, expected_hash: str, runs_dir: Path) -> bool:
        """True when the stage can be skipped: same hash and artifact intact."""
        state = self.stages.get(name)
        if state is None or state.status != "done" or state.hash != expected_hash:
            return False
        if not state.artifact:
            return True
        artifact = Path(runs_dir) / self.run_id / state.artifact
        if not artifact.exists():
            log.info("%s: artifact missing, will re-run", name, extra={"stage": name})
            return False
        if artifact.suffix == ".json":
            try:
                json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.info("%s: artifact corrupt, will re-run", name, extra={"stage": name})
                return False
        return True

    def mark_running(self, name: str, stage_hash: str) -> None:
        state = self.stage(name)
        state.status = "running"
        state.hash = stage_hash
        state.attempts += 1
        state.started_at = time.time()
        state.error = ""
        state.traceback = ""

    def mark_done(self, name: str, artifact: str = "", cost: dict[str, Any] | None = None) -> None:
        state = self.stage(name)
        state.status = "done"
        state.artifact = artifact
        state.cost = cost or {}
        state.finished_at = time.time()

    def mark_failed(self, name: str, exc: BaseException, partial: str = "") -> None:
        state = self.stage(name)
        state.status = "failed"
        state.error = f"{type(exc).__name__}: {exc}"
        state.traceback = "".join(traceback.format_exception(exc))[-4000:]
        state.partial = partial
        state.finished_at = time.time()

    def mark_degraded(self, name: str, reason: str) -> None:
        state = self.stage(name)
        state.status = "degraded"
        state.error = reason
        state.finished_at = time.time()
        self.notes.append(f"{name}: degraded — {reason}")

    def exhausted(self, name: str) -> bool:
        """Three failures and the stage falls back instead of retrying forever."""
        return self.stage(name).attempts >= MAX_ATTEMPTS

    def degrade_to(self, level: int, reason: str) -> None:
        if level > self.degradation_level:
            log.warning(
                "degradation level %d → %d: %s",
                self.degradation_level,
                level,
                reason,
                extra={"run_id": self.run_id, "stage": "orchestrator"},
            )
            self.degradation_level = level
            self.notes.append(f"LVL{level}: {reason}")
        if level >= 3:
            self.needs_human_review = True

    def next_stage(self) -> str | None:
        for name in STAGES:
            state = self.stages.get(name)
            if state is None or state.status not in {"done", "skipped", "degraded"}:
                return name
        return None

    def pending_stages(self) -> Iterator[str]:
        start = self.next_stage()
        if start is None:
            return
        yield from STAGES[STAGES.index(start) :]


def stage_hash(*parts: Any) -> str:
    """Deterministic hash of a stage's inputs (§4.1)."""
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, Path):
            digest.update(part.read_bytes() if part.exists() else b"")
        elif isinstance(part, bytes):
            digest.update(part)
        elif isinstance(part, str):
            digest.update(part.encode("utf-8"))
        else:
            digest.update(json.dumps(part, sort_keys=True, default=str).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def new_run_id(topic_id: str, when: float | None = None) -> str:
    """`2026-08-10_jwst-atmosphere_01` — sortable and readable in an artifact list."""
    stamp = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
    slug = "".join(char if char.isalnum() or char in "-_" else "-" for char in topic_id).strip("-")
    return f"{stamp}_{slug or 'run'}_01"
