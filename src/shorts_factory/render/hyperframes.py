"""HyperFrames CLI adapter — lint → check → render.

Rendering goes through HyperFrames and nothing else. The CLI is invoked as a
subprocess (`npx hyperframes …`) so the project stays a plain Python package,
and the exact binary, extra flags and Docker usage are all overridable from the
environment because CI and laptops disagree about all three.

    HYPERFRAMES_CMD          e.g. "npx --yes hyperframes" (default)
    HYPERFRAMES_DOCKER       "1" to pass --docker to render (default in CI)
    HYPERFRAMES_RENDER_ARGS  extra flags appended verbatim to render
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..errors import RenderError
from ..logging_utils import get_logger

log = get_logger("hyperframes")

SKILL_INSTALL_HINT = "npx skills add heygen-com/hyperframes"
_VIDEO_SUFFIXES = (".mp4", ".webm", ".mov")


@dataclass
class StepResult:
    step: str
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False

    @property
    def tail(self) -> str:
        text = (self.stderr or self.stdout or "").strip()
        return text[-800:]

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "skipped": self.skipped,
            "output": self.tail[-400:],
        }


@dataclass
class RenderResult:
    output: Path | None
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output is not None and self.output.exists()

    def to_dict(self) -> dict[str, object]:
        return {
            "output": str(self.output) if self.output else None,
            "ok": self.ok,
            "steps": [step.to_dict() for step in self.steps],
        }


class HyperFramesRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        raw = os.environ.get("HYPERFRAMES_CMD")
        self.command = tuple(shlex.split(raw)) if raw else settings.hyperframes_cmd
        self.extra_render_args = shlex.split(os.environ.get("HYPERFRAMES_RENDER_ARGS", ""))

    # -- environment --------------------------------------------------------

    @property
    def executable(self) -> str:
        return self.command[0]

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def doctor(self, project_dir: Path | None = None) -> StepResult:
        if not self.is_available():
            return StepResult("doctor", False, -1, stderr=f"{self.executable} not found", skipped=True)
        # Bounded: `doctor` may pull the CLI from npm on first use, but an
        # environment check must never become the slowest part of a run.
        return self._run(["doctor"], project_dir or Path.cwd(), "doctor", timeout=180, check=False)

    # -- pipeline steps -----------------------------------------------------

    def lint(self, project_dir: Path) -> StepResult:
        return self._run(["lint"], project_dir, "lint")

    def check(self, project_dir: Path) -> StepResult:
        return self._run(["check"], project_dir, "check")

    def render(self, project_dir: Path, output: Path) -> StepResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        # HyperFrames ≥0.7 takes the project dir + `-o/--output` (not `index.html --out`).
        args = ["render", ".", "--output", str(output)]
        if self.settings.render_with_docker:
            args.append("--docker")
        args += self.extra_render_args
        return self._run(args, project_dir, "render", timeout=3600)

    def run_pipeline(self, project_dir: Path, output: Path) -> RenderResult:
        """lint → check → render, stopping at the first hard failure."""
        result = RenderResult(output=None)

        if not self.is_available():
            message = (
                f"{self.executable} not found. Install Node 22+ and the HyperFrames "
                f"skills with `{SKILL_INSTALL_HINT}`."
            )
            if self.settings.dry_run:
                log.warning("%s (dry run, continuing)", message, extra={"stage": "render"})
                result.steps.append(StepResult("render", False, -1, stderr=message, skipped=True))
                return result
            raise RenderError(message)

        for step in (self.lint, self.check):
            step_result = step(project_dir)
            result.steps.append(step_result)
            if not step_result.ok:
                raise RenderError(f"hyperframes {step_result.step} failed:\n{step_result.tail}")

        if self.settings.dry_run:
            log.info("dry run: stopping before render", extra={"stage": "render"})
            result.steps.append(StepResult("render", True, 0, skipped=True))
            return result

        render_result = self.render(project_dir, output)
        result.steps.append(render_result)
        if not render_result.ok:
            raise RenderError(f"hyperframes render failed:\n{render_result.tail}")

        result.output = output if output.exists() else _find_output(project_dir)
        if result.output is None:
            raise RenderError(
                "hyperframes render reported success but no video file was produced "
                f"(looked for {output} and any video in {project_dir})"
            )
        return result

    # -- subprocess ---------------------------------------------------------

    def _run(
        self,
        args: list[str],
        cwd: Path,
        step: str,
        *,
        timeout: float = 900,
        check: bool = True,
    ) -> StepResult:
        command = [*self.command, *args]
        log.info("$ %s", " ".join(shlex.quote(part) for part in command), extra={"stage": "render"})
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            if check:
                raise RenderError(f"{self.executable} not found: {exc}") from exc
            return StepResult(step, False, -1, stderr=str(exc))
        except subprocess.TimeoutExpired as exc:
            message = f"hyperframes {step} timed out after {timeout:.0f}s"
            if check:
                raise RenderError(message) from exc
            return StepResult(step, False, -1, stderr=message)

        result = StepResult(
            step=step,
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if not result.ok:
            log.warning("%s exited %d: %s", step, result.exit_code, result.tail, extra={"stage": "render"})
        return result


def _find_output(project_dir: Path) -> Path | None:
    """Fall back to the newest video in the project when --out was ignored."""
    videos = [
        path for path in project_dir.rglob("*") if path.suffix.lower() in _VIDEO_SUFFIXES and path.is_file()
    ]
    if not videos:
        return None
    return max(videos, key=lambda path: path.stat().st_mtime)
