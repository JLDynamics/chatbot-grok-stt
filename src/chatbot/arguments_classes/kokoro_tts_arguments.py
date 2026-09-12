from dataclasses import dataclass, field


@dataclass
class KokoroTTSHandlerArguments:
    """Kokoro-82M TTS settings (matches the official speech-to-speech setup)."""

    kokoro_tts_model_name: str = field(
        default="mlx-community/Kokoro-82M-bf16",
        metadata={"help": "Kokoro-82M MLX model repo (Apple Silicon)."},
    )
    kokoro_tts_voice: str = field(
        default="bm_fable",
        metadata={
            "help": "Kokoro voice, e.g. bm_fable (British male), af_heart (American female), jf_alpha (Japanese)."
        },
    )
    kokoro_tts_lang_code: str = field(
        default="b",
        metadata={
            "help": "Kokoro language code: a (American), b (British), e/j/f/i/p/h (Spanish/Japanese/French/Italian/Portuguese/Hindi)."
        },
    )
    kokoro_tts_speed: float = field(
        default=1.0,
        metadata={"help": "Speech speed multiplier; >1 speeds up, <1 slows down."},
    )
    kokoro_tts_blocksize: int = field(
        default=512,
        metadata={"help": "Audio chunk size in samples for streaming output."},
    )
    kokoro_tts_gen_noise_gate: bool = field(
        default=True,
        metadata={"help": "Apply a gentle downward expander that hides the output noise floor during pauses."},
    )
    kokoro_tts_gen_noise_gate_threshold: float = field(
        default=0.010,
        metadata={"help": "RMS threshold below which the noise gate attenuates."},
    )
    kokoro_tts_gen_spectral_denoise: bool = field(
        default=True,
        metadata={"help": "Apply causal spectral noise reduction to remove the faint hiss inside the voice."},
    )
    kokoro_tts_gen_spectral_denoise_floor: float = field(
        default=0.04,
        metadata={"help": "Spectral denoise floor; lower removes more hiss (more risk of a processed texture)."},
    )
