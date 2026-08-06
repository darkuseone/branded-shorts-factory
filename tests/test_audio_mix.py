"""Pre-mix filter graph: music + SFX sidechain under voice."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from shorts_factory.render.audio_mix import SFX_DUCK_DB, build_mix
from shorts_factory.spec import MusicSettings
from shorts_factory.voice.elevenlabs import SfxClip, VoiceClip


def test_sfx_sidechain_and_duck_db_in_filter_graph(tmp_path: Path):
    voice = tmp_path / "voice.wav"
    sfx = tmp_path / "hit.wav"
    music = tmp_path / "bed.mp3"
    for path in (voice, sfx, music):
        path.write_bytes(b"\x00" * 64)
    out = tmp_path / "mix.mp3"

    captured: dict[str, list[str]] = {}

    def fake_run(command, **_kwargs):
        captured["command"] = list(command)
        out.write_bytes(b"ok")
        class Result:
            returncode = 0
            stderr = ""
        return Result()

    with patch("shorts_factory.render.audio_mix.is_available", return_value=True), patch(
        "shorts_factory.render.audio_mix.subprocess.run", side_effect=fake_run
    ):
        result = build_mix(
            out,
            duration=10.0,
            voice_clips=[VoiceClip(segment_id="a", path=voice, start=0.0, duration=2.0, text="hi")],
            sfx_clips=[SfxClip(fx_id="fx1", path=sfx, start=1.0, duration=0.5, volume=0.4)],
            music_path=music,
            music=MusicSettings(volume=0.2, ducking=True),
        )

    assert result.ok
    command = captured["command"]
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "asplit=3[voice][voicekey_m][voicekey_s]" in filter_complex
    assert "[sfxraw][voicekey_s]sidechaincompress=" in filter_complex
    assert f"volume={10 ** (SFX_DUCK_DB / 20.0):.4f}" in filter_complex
    assert "[music][voicekey_m]sidechaincompress=" in filter_complex
