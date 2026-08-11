"""The orchestrator: one JSON in, one finished MP4 out.

Stages run in a fixed order and each one degrades rather than aborts, so a
missing credential costs you a track — not the run. What actually happened is
always written to `build/reports/<id>.json`.

Two-phase flow (HeyGen MCP lives in chat, secrets live in Actions):

    prepare → voice.wav + words.json artifact
    (chat)  → HeyGen avatar → commit jobs/<id>/avatar.mp4
    render  → timeline + host modes + mix + HyperFrames

Full local path still works: ``build`` does prepare + avatar API + render.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .assets.brand import Brandbook, apply_brandbook_to_spec
from .assets.library import MemeLibrary, MusicLibrary, SfxLibrary
from .assets.meme_policy import (
    MemeDecision,
    MemeHistory,
    MemePolicyConfig,
    decide_meme,
    ensure_meme_visual,
    history_path,
)
from .config import Settings
from .errors import RenderError
from .generative.budget import TokenBudget
from .logging_utils import get_logger, stage
from .qa.gate import QAReport
from .render.audio_mix import build_mix
from .render.backfill import backfill_empty_slots
from .render.composition import CompositionWriter
from .render.host_presence import HostPlan, plan_host
from .render.hyperframes import HyperFramesRunner, RenderResult
from .render.rhythm import RetimeReport, retime_visuals
from .render.timeline import Timeline, build_timeline, use_mixed_audio
from .resolver import ResolvedVisual, VisualResolver
from .spec import Spec, SpecIssue
from .voice.audio_design import finalize_audio_fx, resolve_from_library, suggest_audio_fx
from .voice.captions import CaptionCue, build_cues
from .voice.elevenlabs import ElevenLabsClient, SfxClip, VoiceClip
from .voice.heygen import AvatarClip, HeyGenClient

log = get_logger("pipeline")


@dataclass
class RunResult:
    """Everything one run produced, successful or not."""

    spec_id: str
    output: Path | None = None
    composition_dir: Path | None = None
    report_path: Path | None = None
    prepare_dir: Path | None = None
    timeline: Timeline | None = None
    qa: QAReport = field(default_factory=QAReport)
    resolved: list[ResolvedVisual] = field(default_factory=list)
    voice_clips: list[VoiceClip] = field(default_factory=list)
    sfx_clips: list[SfxClip] = field(default_factory=list)
    captions: list[CaptionCue] = field(default_factory=list)
    avatar: AvatarClip | None = None
    avatar_start: float = 0.0
    music: Path | None = None
    render: RenderResult | None = None
    budget: TokenBudget | None = None
    spec_issues: list[SpecIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meme_decision: MemeDecision | None = None
    rhythm: RetimeReport | None = None

    @property
    def rendered(self) -> bool:
        return self.output is not None and self.output.exists()

    @property
    def broll_coverage(self) -> float:
        """Fraction of the video with real footage behind it, 0.0-1.0."""
        if self.timeline is None or not self.timeline.duration:
            return 0.0
        from .render.timeline import covered_seconds

        return min(1.0, covered_seconds(self.timeline) / self.timeline.duration)

    @property
    def needs_review(self) -> bool:
        return bool(self.qa.manual_review) or bool(self.qa.rejected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "output": str(self.output) if self.output else None,
            "composition_dir": str(self.composition_dir) if self.composition_dir else None,
            "prepare_dir": str(self.prepare_dir) if self.prepare_dir else None,
            "rendered": self.rendered,
            "needs_review": self.needs_review,
            "spec_issues": [str(issue) for issue in self.spec_issues],
            "warnings": self.warnings,
            "qa": self.qa.to_dict(),
            "rhythm": self.rhythm.to_dict() if self.rhythm else None,
            "budget": self.budget.to_dict() if self.budget else None,
            "search": {item.visual.id: item.search.to_dict() for item in self.resolved if item.search},
            "voice": {
                "clips": [clip.to_dict() for clip in self.voice_clips],
                "sfx": [clip.to_dict() for clip in self.sfx_clips],
                "captions": len(self.captions),
                "avatar": self.avatar.to_dict() if self.avatar else None,
                "music": str(self.music) if self.music else None,
            },
            "meme": (
                {
                    "allowed": self.meme_decision.allowed,
                    "reason": self.meme_decision.reason,
                    "beat": self.meme_decision.beat,
                }
                if self.meme_decision
                else None
            ),
            "timeline": self.timeline.to_dict() if self.timeline else None,
            "render": self.render.to_dict() if self.render else None,
        }


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        resolver: VisualResolver | None = None,
        runner: HyperFramesRunner | None = None,
    ):
        settings.paths.ensure()
        self.settings = settings
        self.budget = TokenBudget.from_budgets(settings.budgets)
        self.resolver = resolver or VisualResolver(settings, budget=self.budget)
        self.voice_client = ElevenLabsClient(settings)
        self.avatar_client = HeyGenClient(settings)
        self.runner = runner or HyperFramesRunner(settings)
        self.music_library = MusicLibrary(settings.paths.music_library)
        self.sfx_library = SfxLibrary(settings.paths.sfx_library)
        self.meme_library = MemeLibrary(settings.paths.meme_library)
        self._brandbook: Brandbook | None = None

    # -- entry points -------------------------------------------------------

    def plan(self, spec: Spec, issues: list[SpecIssue] | None = None) -> RunResult:
        """Everything except audio, avatar and rendering — a cheap dry pass."""
        result = RunResult(spec_id=spec.id, spec_issues=list(issues or []), budget=self.budget)
        self._apply_brand(spec, result)
        self._apply_meme_policy(spec, result)
        # No voice yet on this path, so the retimer works from the scenario's
        # own segment timings — still content-driven, just without the word
        # alignment that lets cuts land between words.
        result.rhythm = retime_visuals(spec, [])
        self._resolve_visuals(spec, result)
        result.captions = build_cues(spec.all_segments, [], spec.captions)
        result.music = self._resolve_music(spec, result)
        result.timeline = build_timeline(
            spec,
            result.resolved,
            captions=result.captions,
            music_path=result.music,
        )
        result.warnings.extend(result.timeline.warnings)

        composition = CompositionWriter(
            spec,
            result.timeline,
            self.settings.paths.composition / spec.id,
            host=plan_host(spec),
        ).write()
        result.composition_dir = composition.directory

        self._write_report(spec, result)
        return result

    def prepare(self, spec: Spec, issues: list[SpecIssue] | None = None) -> RunResult:
        """Phase 1: brand, voice (+ word timings), visuals, QA. No avatar, no render.

        Writes ``build/prepare/<id>/`` with narration, ``words.json`` and a report —
        the artifact the chat agent downloads before calling HeyGen MCP.
        """
        result = RunResult(spec_id=spec.id, spec_issues=list(issues or []), budget=self.budget)

        with stage("brand", log):
            self._apply_brand(spec, result)

        with stage("memes", log):
            self._apply_meme_policy(spec, result)

        with stage("voice", log) as info:
            self._synthesize_voice(spec, result)
            info["clips"] = len(result.voice_clips)
            info["fx"] = len(result.sfx_clips)

        with stage("rhythm", log) as info:
            result.rhythm = retime_visuals(spec, result.voice_clips)
            result.warnings.extend(result.rhythm.notes)
            info["median_s"] = round(result.rhythm.median_s, 2)
            info["spread_s"] = result.rhythm.spread_s

        with stage("visuals", log) as info:
            self._resolve_visuals(spec, result)
            info["qa"] = result.qa.summary()

        with stage("captions", log) as info:
            result.captions = build_cues(spec.all_segments, result.voice_clips, spec.captions)
            info["cues"] = len(result.captions)

        with stage("music", log):
            result.music = self._resolve_music(spec, result)

        with stage("prepare_artifact", log) as info:
            result.prepare_dir = self._write_prepare_artifact(spec, result)
            info["dir"] = str(result.prepare_dir)

        self._write_report(spec, result)
        return result

    def run(self, spec: Spec, issues: list[SpecIssue] | None = None) -> RunResult:
        """The full build (prepare stages + avatar + render)."""
        result = RunResult(spec_id=spec.id, spec_issues=list(issues or []), budget=self.budget)

        with stage("brand", log):
            self._apply_brand(spec, result)

        with stage("memes", log):
            self._apply_meme_policy(spec, result)

        with stage("voice", log) as info:
            self._synthesize_voice(spec, result)
            info["clips"] = len(result.voice_clips)
            info["fx"] = len(result.sfx_clips)

        with stage("avatar", log) as info:
            self._generate_avatar(spec, result)
            info["ok"] = result.avatar is not None

        with stage("rhythm", log) as info:
            result.rhythm = retime_visuals(spec, result.voice_clips)
            result.warnings.extend(result.rhythm.notes)
            info["median_s"] = round(result.rhythm.median_s, 2)
            info["spread_s"] = result.rhythm.spread_s

        with stage("visuals", log) as info:
            self._resolve_visuals(spec, result)
            info["qa"] = result.qa.summary()

        with stage("captions", log) as info:
            result.captions = build_cues(spec.all_segments, result.voice_clips, spec.captions)
            info["cues"] = len(result.captions)

        with stage("music", log):
            result.music = self._resolve_music(spec, result)

        with stage("timeline", log) as info:
            result.timeline = build_timeline(
                spec,
                result.resolved,
                voice_clips=result.voice_clips,
                sfx_clips=result.sfx_clips,
                captions=result.captions,
                avatar=result.avatar,
                avatar_start=result.avatar_start,
                music_path=result.music,
            )
            self._mix_audio(spec, result)
            result.warnings.extend(result.timeline.warnings)
            info["elements"] = len(result.timeline.elements)

        with stage("compose", log) as info:
            composition_dir = self.settings.paths.composition / spec.id
            writer = CompositionWriter(spec, result.timeline, composition_dir, host=plan_host(spec))
            composition = writer.write()
            result.composition_dir = composition.directory
            info["media"] = composition.media_files

        with stage("render", log) as info:
            output = self.settings.paths.output / f"{spec.id}.mp4"
            try:
                result.render = self.runner.run_pipeline(composition.directory, output)
                result.output = result.render.output
                result.warnings.extend(result.render.warnings)
                info["output"] = result.output.name if result.output else "none"
            except RenderError as exc:
                result.warnings.append(str(exc))
                log.error("%s", exc, extra={"stage": "render"})

        self._record_meme_history(spec, result)
        self._write_report(spec, result)
        return result

    def render_only(self, spec: Spec, issues: list[SpecIssue] | None = None) -> RunResult:
        """Phase 3: require ``jobs/<id>/avatar.*``; skip HeyGen API calls."""
        if not self.avatar_client.prefers_external(spec.id, spec.avatar):
            spec.avatar = replace(spec.avatar, provider="external")
        return self.run(spec, issues)

    # -- stages -------------------------------------------------------------

    def _apply_brand(self, spec: Spec, result: RunResult) -> None:
        book = Brandbook.load(self.settings.paths.brand_dir)
        self._brandbook = book
        apply_brandbook_to_spec(spec, book)
        if book is None:
            result.warnings.append("no brandbook found; using per-video brand_elements")

    def _apply_meme_policy(self, spec: Spec, result: RunResult) -> None:
        raw = self._brandbook.memes if self._brandbook else {}
        policy = MemePolicyConfig.from_brandbook(raw)
        history = MemeHistory.load(history_path(self.settings.paths.root))
        decision = decide_meme(spec, policy, history)
        result.meme_decision = decision

        if decision.allowed:
            ensure_meme_visual(spec, decision)
        else:
            kept = []
            for visual in spec.visuals:
                if visual.type != "meme":
                    kept.append(visual)
                    continue
                result.warnings.append(f"meme slot {visual.id} skipped: {decision.reason}")
            spec.visuals = kept

        log.info(
            "meme policy: %s (%s)",
            "allow" if decision.allowed else "deny",
            decision.reason,
            extra={"stage": "memes"},
        )

    def _record_meme_history(self, spec: Spec, result: RunResult) -> None:
        used = any(item.visual.type == "meme" and item.qa.outcome == "accepted" for item in result.resolved)
        path = history_path(self.settings.paths.root)
        history = MemeHistory.load(path)
        history.record(spec.id, used_meme=used)
        try:
            history.save(path)
        except OSError as exc:
            result.warnings.append(f"could not update meme history: {exc}")

    def _synthesize_voice(self, spec: Spec, result: RunResult) -> None:
        if self.voice_client.is_available:
            result.voice_clips = self.voice_client.synthesize_script(spec.all_segments, spec.voice)
            if len(result.voice_clips) < len(spec.all_segments):
                result.warnings.append(
                    f"only {len(result.voice_clips)}/{len(spec.all_segments)} narration segments "
                    "were rendered"
                )
        else:
            fallback = self._load_avatar_voice(spec)
            if fallback:
                result.voice_clips = fallback
                result.warnings.append(
                    "narration loaded from jobs/<id>/voice_from_avatar.mp3 "
                    f"(ElevenLabs unavailable: {self.voice_client.unavailable_reason()})"
                )
            else:
                result.warnings.append(f"narration skipped: {self.voice_client.unavailable_reason()}")

        self._resolve_sound_design(spec, result)

    def _load_avatar_voice(self, spec: Spec) -> list[VoiceClip]:
        """Reuse HeyGen/MCP avatar audio when ElevenLabs is not configured.

        Looks for ``jobs/<id>/voice_from_avatar.mp3``, or extracts audio from
        ``jobs/<id>/avatar.mp4``. Returns one continuous clip at t=0 so the mix
        stays in sync with the muted avatar video; captions fall back to estimates.
        """
        job_dir = self.settings.paths.root / "jobs" / spec.id
        voice_path = job_dir / "voice_from_avatar.mp3"
        if not voice_path.exists():
            avatar_path = job_dir / "avatar.mp4"
            if not avatar_path.exists():
                return []
            voice_path.parent.mkdir(parents=True, exist_ok=True)
            target = self.settings.paths.voice / f"{spec.id}_from_avatar.mp3"
            target.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                self.settings.ffmpeg_cmd,
                "-y",
                "-i",
                str(avatar_path),
                "-vn",
                "-acodec",
                "libmp3lame",
                "-q:a",
                "2",
                str(target),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("could not extract avatar audio: %s", exc, extra={"stage": "voice"})
                return []
            voice_path = target
            with contextlib.suppress(OSError):
                shutil.copy2(target, job_dir / "voice_from_avatar.mp3")
        else:
            target = self.settings.paths.voice / f"{spec.id}_from_avatar.mp3"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(voice_path, target)
            voice_path = target

        duration = float(spec.spoken_duration or spec.duration_target)
        first = spec.all_segments[0] if spec.all_segments else None
        if first is None:
            return []
        return [
            VoiceClip(
                segment_id=first.id,
                path=voice_path,
                start=0.0,
                duration=duration,
                text=" ".join(segment.text for segment in spec.all_segments),
                words=[],
            )
        ]

    def _resolve_sound_design(self, spec: Spec, result: RunResult) -> None:
        """Your own sounds first; generate only what the bank cannot cover."""
        effects = finalize_audio_fx(spec.audio_fx, spec) if spec.audio_fx else suggest_audio_fx(spec)
        if not effects:
            return

        from_library, missing = resolve_from_library(effects, self.sfx_library)
        generated: list[SfxClip] = []
        if missing:
            if self.voice_client.is_available:
                generated = self.voice_client.generate_all_fx(missing)
                for clip in generated:
                    self.voice_client.cache_fx_to_bank(clip, self.settings.paths.sfx_library)
            elif self.sfx_library.items:
                result.warnings.append(
                    f"{len(missing)} effect(s) have no match in {self.settings.paths.sfx_library} "
                    f"and could not be generated: {self.voice_client.unavailable_reason()}"
                )

        result.sfx_clips = sorted(from_library + generated, key=lambda clip: clip.start)

    def _generate_avatar(self, spec: Spec, result: RunResult) -> None:
        """Load or render the presenter for the window it is supposed to speak in."""
        if not spec.avatar.enabled:
            return

        wanted = spec.avatar.segments
        segments = [s for s in spec.all_segments if not wanted or s.id in wanted]
        if not segments:
            result.warnings.append("avatar.segments matched no script segments; presenter skipped")
            return

        window_start = min(segment.start for segment in segments)
        window_end = max(segment.end for segment in segments)
        result.avatar_start = window_start

        gaps = [
            (a.end, b.start) for a, b in zip(segments, segments[1:], strict=False) if b.start - a.end > 1.5
        ]
        if gaps:
            result.warnings.append(
                "avatar.segments are not contiguous ("
                + ", ".join(f"{a:g}–{b:g}s silent" for a, b in gaps)
                + "); the presenter stays on screen through the gaps"
            )

        if self.avatar_client.prefers_external(spec.id, spec.avatar):
            result.avatar = self.avatar_client.generate(
                spec.avatar,
                spec.voice,
                text=" ".join(segment.text for segment in segments),
                audio_path=None,
                duration_hint=window_end - window_start,
                spec_id=spec.id,
            )
            if result.avatar is None:
                result.warnings.append("avatar track missing; the render continues without a presenter")
            return

        clips = [clip for clip in result.voice_clips if not wanted or clip.segment_id in wanted]
        narration: Path | None = None
        if clips:
            shifted = [replace(clip, start=max(0.0, clip.start - window_start)) for clip in clips]
            mix = build_mix(
                self.settings.paths.voice / "narration_for_avatar.mp3",
                duration=window_end - window_start,
                voice_clips=shifted,
                sfx_clips=[],
                music_path=None,
                music=spec.music,
                ffmpeg=self.settings.ffmpeg_cmd,
            )
            if mix.ok:
                narration = mix.path
            elif len(shifted) == 1 and shifted[0].start < 0.05:
                narration = shifted[0].path
            else:
                result.warnings.append(
                    f"could not build a narration track for the avatar ({mix.reason}); using text mode"
                )

        result.avatar = self.avatar_client.generate(
            spec.avatar,
            spec.voice,
            text=" ".join(segment.text for segment in segments),
            audio_path=narration,
            duration_hint=window_end - window_start,
            spec_id=spec.id,
        )
        if result.avatar is None:
            result.warnings.append("avatar track missing; the render continues without a presenter")

    def _resolve_visuals(self, spec: Spec, result: RunResult) -> None:
        result.resolved = self.resolver.resolve_all(spec)
        # QA is recorded before the backfill so the report says what the gates
        # actually decided; the reuse is reported separately, as the editorial
        # choice it is rather than as a slot that passed.
        for item in result.resolved:
            result.qa.add(item.qa)
        result.warnings.extend(backfill_empty_slots(result.resolved))
        for item in result.resolved:
            if item.qa.outcome == "rejected":
                result.warnings.append(
                    f"visual {item.visual.id} could not be filled: "
                    + ("; ".join(item.qa.notes) or "no candidates passed QA")
                )
            elif item.qa.outcome == "manual_review":
                result.warnings.append(
                    f"visual {item.visual.id} needs a human look: "
                    + (item.qa.vision.reason if item.qa.vision else "vision gate unavailable")
                )
        log.info("%s", result.qa.summary(), extra={"stage": "visuals"})

    def _resolve_music(self, spec: Spec, result: RunResult) -> Path | None:
        if not spec.music.track and self._brandbook is not None:
            track = self._brandbook.music_for_rubric(spec.rubric)
            if track:
                spec.music = replace(spec.music, track=track)

        if not spec.music.track and not self.music_library.items:
            return None
        item = self.music_library.resolve(spec.music.track)
        if item is None:
            result.warnings.append(
                f"no music track available (looked for {spec.music.track!r} in "
                f"{self.settings.paths.music_library})"
            )
            return None
        return item.path

    def _mix_audio(self, spec: Spec, result: RunResult) -> None:
        if not (result.voice_clips or result.sfx_clips or result.music):
            return
        assert result.timeline is not None
        mix = build_mix(
            self.settings.paths.voice / f"{spec.id}_mix.mp3",
            duration=spec.duration_target,
            voice_clips=result.voice_clips,
            sfx_clips=result.sfx_clips,
            music_path=result.music,
            music=spec.music,
            ffmpeg=self.settings.ffmpeg_cmd,
        )
        if mix.ok and mix.path is not None:
            use_mixed_audio(result.timeline, mix.path, spec.duration_target)
        else:
            result.warnings.append(
                f"pre-mix unavailable ({mix.reason}); audio tracks are handed to the renderer separately"
            )

    def _write_prepare_artifact(self, spec: Spec, result: RunResult) -> Path:
        """Persist voice + word timings for the chat-side HeyGen step."""
        target = self.settings.paths.workdir / "prepare" / spec.id
        target.mkdir(parents=True, exist_ok=True)

        words_payload: list[dict[str, Any]] = []
        for clip in result.voice_clips:
            dest = target / f"{clip.segment_id}{clip.path.suffix}"
            if clip.path.exists():
                shutil.copy2(clip.path, dest)
            words_payload.append(
                {
                    "segment_id": clip.segment_id,
                    "start": clip.start,
                    "duration": clip.duration,
                    "text": clip.text,
                    "file": dest.name,
                    "words": [
                        {"word": word.word, "start": word.start, "end": word.end} for word in clip.words
                    ],
                }
            )

        if result.voice_clips:
            narration = target / "voice.mp3"
            mix = build_mix(
                narration,
                duration=spec.spoken_duration or spec.duration_target,
                voice_clips=result.voice_clips,
                sfx_clips=[],
                music_path=None,
                music=spec.music,
                ffmpeg=self.settings.ffmpeg_cmd,
            )
            if not mix.ok and result.voice_clips[0].path.exists():
                shutil.copy2(result.voice_clips[0].path, narration)

        motion = ""
        if self._brandbook and isinstance(self._brandbook.avatar, dict):
            motion = str(self._brandbook.avatar.get("motion_prompt") or "")

        (target / "words.json").write_text(
            json.dumps(
                {
                    "id": spec.id,
                    "duration_target": spec.duration_target,
                    "look_id": spec.avatar.avatar_id,
                    "motion_prompt": motion,
                    "segments": words_payload,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (target / "report.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("prepare artifact at %s", target, extra={"stage": "prepare"})
        return target

    # -- reporting ----------------------------------------------------------

    def _write_report(self, spec: Spec, result: RunResult) -> None:
        path = self.settings.paths.reports / f"{spec.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        result.report_path = path
        log.info("report written to %s", path, extra={"stage": "report"})


def _host_plan(spec: Spec) -> HostPlan:
    """Build host presentation windows from segment modes."""
    return plan_host(spec)
