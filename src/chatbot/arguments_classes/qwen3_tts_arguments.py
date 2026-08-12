from dataclasses import dataclass, field


@dataclass
class Qwen3TTSHandlerArguments:
    """Apple-Silicon Qwen3 CustomVoice settings."""

    qwen3_tts_model_name: str = field(
        default="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        metadata={"help": "Qwen3 1.7B CustomVoice model; mapped to its MLX conversion."},
    )
    qwen3_tts_temperature: float = field(default=0.5, metadata={"help": "Voice sampling temperature."})
    qwen3_tts_speaker: str = field(default="Ryan", metadata={"help": "Built-in CustomVoice speaker."})
    qwen3_tts_mlx_quantization: str = field(
        default="8bit",
        metadata={"choices": ("bf16", "4bit", "6bit", "8bit")},
    )
    qwen3_tts_language: str = field(default="auto", metadata={"help": "Synthesis language."})
