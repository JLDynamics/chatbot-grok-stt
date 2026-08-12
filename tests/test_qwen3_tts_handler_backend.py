from types import SimpleNamespace

import pytest

from chatbot.TTS.qwen3_tts_handler import (
    MAX_UTTERANCE_TOKENS,
    MIN_UTTERANCE_TOKENS,
    Qwen3TTSHandler,
)


def _handler(quantization="8bit"):
    handler = Qwen3TTSHandler.__new__(Qwen3TTSHandler)
    handler.mlx_quantization = quantization
    return handler


def test_qwen_repo_maps_to_selected_mlx_customvoice_quantization():
    handler = _handler("8bit")
    assert handler._resolve_model_name("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice") == (
        "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
    )


@pytest.mark.parametrize("quantization", ["bf16", "4bit", "6bit", "8bit"])
def test_supported_mlx_quantizations(quantization):
    assert Qwen3TTSHandler._normalize_quantization(quantization) == quantization


def test_clone_or_design_models_are_rejected():
    handler = _handler()
    with pytest.raises(ValueError, match="CustomVoice"):
        handler._resolve_model_name("mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign")


def test_speaker_matching_is_case_insensitive():
    handler = _handler()
    handler.speaker = "ryan"
    handler.model = SimpleNamespace(get_supported_speakers=lambda: ["Ryan", "Serena"])
    assert handler._resolve_speaker() == "Ryan"


def test_token_estimate_is_bounded():
    handler = _handler()
    assert handler._estimate_max_tokens("Hi") == MIN_UTTERANCE_TOKENS
    assert handler._estimate_max_tokens("word " * 10000) == MAX_UTTERANCE_TOKENS
