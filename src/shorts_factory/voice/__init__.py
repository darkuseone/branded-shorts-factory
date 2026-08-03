"""Voice, avatar and audio design."""

from .audio_design import suggest_audio_fx
from .captions import CaptionCue, CaptionWord, build_cues
from .elevenlabs import ElevenLabsClient, SfxClip, VoiceClip, WordTiming
from .heygen import AvatarClip, HeyGenClient

__all__ = [
    "AvatarClip",
    "CaptionCue",
    "CaptionWord",
    "ElevenLabsClient",
    "HeyGenClient",
    "SfxClip",
    "VoiceClip",
    "WordTiming",
    "build_cues",
    "suggest_audio_fx",
]
