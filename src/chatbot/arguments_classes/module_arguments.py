from dataclasses import dataclass, field


@dataclass
class ModuleArguments:
    """Options shared by the single supported browser voice pipeline."""

    stt: str = field(default="native-stt", metadata={"choices": ("native-stt",)})
    llm_backend: str = field(default="responses-api", metadata={"choices": ("responses-api",)})
    tts: str = field(default="kokoro", metadata={"choices": ("kokoro", "siri", "vibevoice")})
    log_level: str = field(default="info", metadata={"help": "Python logging level."})
    turn_quality_gate: bool = field(
        default=True,
        metadata={
            "help": "Suppress finalized filler, number/letter ASR noise, one-word junk, and explicit non-speech "
            "transcripts before the LLM. Commands and greetings are always allowed. Enabled by default; "
            "pass --no_turn_quality_gate to disable it."
        },
    )
