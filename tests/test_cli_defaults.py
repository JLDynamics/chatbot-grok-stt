import pytest

from chatbot.cli import parse_command
from chatbot.s2s_pipeline import parse_arguments


def test_cli_has_browser_server_only():
    command, remaining = parse_command(["serve", "--port", "9876"])
    assert command == "serve"
    assert remaining == ["--port", "9876"]


def test_current_defaults_are_mac_voice_profile():
    args = parse_arguments([])
    assert args.realtime_server_kwargs.port == 8766
    assert args.stt_backend.name == "grok-stt"
    assert args.llm_backend.name == "responses-api"
    assert args.tts_backend.name == "kokoro"
    assert args.llm_backend.config["model_name"] == "openai/gpt-5.6-luna"
    assert args.tts_backend.config["voice"] == "bm_fable"
    assert args.tts_backend.config["model_name"] == "mlx-community/Kokoro-82M-bf16"
    assert args.module_kwargs.turn_quality_gate is True


def test_turn_quality_gate_can_be_disabled():
    args = parse_arguments(["--no_turn_quality_gate"])

    assert args.module_kwargs.turn_quality_gate is False


def test_removed_commands_and_backends_are_rejected():
    with pytest.raises(SystemExit):
        parse_command(["local"])
    with pytest.raises(SystemExit):
        parse_arguments(["--stt", "whisper"])
