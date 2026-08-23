from dataclasses import dataclass, field


@dataclass
class VibeVoiceTTSHandlerArguments:
    """Microsoft VibeVoice streaming TTS settings."""

    vibevoice_tts_model_name: str = field(
        default="mlx-community/VibeVoice-Realtime-0.5B-8bit",
        metadata={"help": "VibeVoice MLX model repo."},
    )
    vibevoice_tts_voice: str = field(
        default="en-Emma_woman",
        metadata={"help": "VibeVoice voice cache name from the model repo (e.g. en-Emma_woman, en-Carter_man)."},
    )
    vibevoice_tts_max_tokens: int = field(
        default=1024,
        metadata={"help": "Safety ceiling on generated tokens; the model stops on EOS, so this only bounds runaway output."},
    )
    vibevoice_tts_cfg_scale: float = field(
        default=1.5,
        metadata={"help": "Classifier-free guidance scale; higher is more distinct but can sound harsh."},
    )
    vibevoice_tts_ddpm_steps: int | None = field(
        default=None,
        metadata={"help": "Diffusion inference steps; leave unset for the model default (higher = slower but cleaner)."},
    )
    vibevoice_tts_gen_noise_gate: bool = field(
        default=True,
        metadata={"help": "Apply a gentle downward expander that hides the model's output noise floor (hiss) during pauses."},
    )
    vibevoice_tts_gen_noise_gate_threshold: float = field(
        default=0.010,
        metadata={"help": "RMS threshold below which the noise gate attenuates; set lower to gate less, higher to gate more aggressively."},
    )
    vibevoice_tts_gen_spectral_denoise: bool = field(
        default=True,
        metadata={"help": "Apply causal spectral noise reduction to remove the faint hiss inside the voice."},
    )
    vibevoice_tts_gen_spectral_denoise_floor: float = field(
        default=0.04,
        metadata={"help": "Spectral denoise floor: how far the noise can be suppressed (lower = more suppression, more risk of artifacts)."},
    )
