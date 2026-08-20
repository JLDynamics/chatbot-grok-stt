from dataclasses import dataclass, field


@dataclass
class ModuleArguments:
    """Options shared by the single supported browser voice pipeline."""

    stt: str = field(default="parakeet-tdt", metadata={"choices": ("parakeet-tdt",)})
    llm_backend: str = field(default="responses-api", metadata={"choices": ("responses-api",)})
    tts: str = field(default="csm", metadata={"choices": ("csm",)})
    log_level: str = field(default="info", metadata={"help": "Python logging level."})
    enable_live_transcription: bool = field(
        default=True,
        metadata={"help": "Show partial Parakeet transcripts while the user is speaking."},
    )
    live_transcription_update_interval: float = field(
        default=0.5,
        metadata={"help": "Seconds between partial transcript updates."},
    )
    live_transcription_min_silence_ms: int = field(
        default=500,
        metadata={"help": "Silence required to end a live-transcribed turn."},
    )
    turn_quality_gate: bool = field(
        default=True,
        metadata={
            "help": "Suppress finalized filler-only and explicit non-speech transcripts before the LLM. "
            "Short commands are always allowed. Enabled by default; pass --no_turn_quality_gate to disable it."
        },
    )
