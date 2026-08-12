from dataclasses import dataclass, field


@dataclass
class RealtimeServerArguments:
    host: str = field(
        default="127.0.0.1",
        metadata={"help": "Listen address. The browser launcher uses loopback only."},
    )
    port: int = field(default=8765, metadata={"help": "Realtime WebSocket server port."})
