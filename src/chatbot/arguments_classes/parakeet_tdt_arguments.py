from dataclasses import dataclass, field


@dataclass
class ParakeetTDTSTTHandlerArguments:
    """Apple-Silicon MLX Parakeet settings."""

    parakeet_tdt_model_name: str = field(
        default="mlx-community/parakeet-tdt-0.6b-v3",
        metadata={"help": "MLX Parakeet model on Hugging Face."},
    )
    parakeet_tdt_language: str | None = field(
        default=None,
        metadata={"help": "Optional language code; otherwise detect automatically."},
    )
