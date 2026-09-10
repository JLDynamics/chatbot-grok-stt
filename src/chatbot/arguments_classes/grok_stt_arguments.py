from dataclasses import dataclass, field


@dataclass
class GrokSTTHandlerArguments:
    """xAI hosted speech-to-text, authenticated with the Grok CLI session."""

    grok_stt_url: str = field(
        default="https://api.x.ai/v1/stt",
        metadata={"help": "xAI batch speech-to-text endpoint."},
    )
    grok_stt_language: str | None = field(
        default="en",
        metadata={
            "help": "Language code sent with each request. A fixed language also enables formatting, which is "
            "what returns punctuation and capitals. Pass 'auto' to let xAI detect the language instead, "
            "which the endpoint does not allow together with formatting."
        },
    )
    grok_stt_timeout_s: float = field(
        default=20.0,
        metadata={"help": "Per-request timeout. A slow turn falls back to an empty transcript, not a hang."},
    )
    grok_stt_auth_path: str = field(
        default="~/.grok/auth.json",
        metadata={
            "help": "Grok CLI session file. Its token is read fresh per request because it expires in hours. "
            "XAI_API_KEY overrides it when set."
        },
    )
