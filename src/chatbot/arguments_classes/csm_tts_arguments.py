from dataclasses import dataclass, field


@dataclass
class CsmTTSHandlerArguments:
    """Sesame CSM-1B conversational TTS settings."""

    csm_tts_model_name: str = field(
        default="mlx-community/csm-1b-8bit",
        metadata={"help": "Sesame CSM-1B MLX model repo."},
    )
    csm_tts_voice: str = field(
        default="conversational_b",
        metadata={"help": "CSM default voice prompt (conversational_a or conversational_b)."},
    )
    csm_tts_temperature: float = field(
        default=0.55,
        metadata={"help": "CSM sampling temperature; lower is steadier and clearer."},
    )
    csm_tts_stream: bool = field(
        default=True,
        metadata={"help": "Stream audio as CSM generates it (lower latency, may underrun). Disable to buffer the whole sentence for gapless playback."},
    )
    csm_tts_gen_max_audio_length_ms: float = field(
        default=20000.0,
        metadata={"help": "Cap on generated audio length (ms) before CSM stops generating."},
    )
    csm_tts_gen_streaming_interval: float = field(
        default=0.5,
        metadata={"help": "Seconds of audio CSM accumulates before yielding each streaming chunk."},
    )
