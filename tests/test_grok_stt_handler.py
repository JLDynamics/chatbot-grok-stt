import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from chatbot.pipeline.messages import Transcription, VADAudio
from chatbot.STT.grok_stt_handler import GrokAuthError, GrokSTTHandler, load_bearer_token, to_pcm16


def _auth_file(tmp_path, token="tok-abc", hours=5):
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"https://auth.x.ai::client": {"key": token, "expires_at": expires.isoformat()}}))
    return path


def _handler(tmp_path, monkeypatch, responses):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    handler = object.__new__(GrokSTTHandler)
    handler.url = "https://api.x.ai/v1/stt"
    handler.start_language = None
    handler.last_language = "en"
    handler.timeout_s = 5.0
    handler.auth_path = str(_auth_file(tmp_path))
    handler.sample_rate = 16000
    handler._reset_turn_cache()
    handler.sent = []

    class FakeClient:
        def post(self, url, headers, files, data):
            handler.sent.append(len(files["file"][1]) // 2)  # samples uploaded
            return responses.pop(0)

    handler._client = FakeClient()
    return handler


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_pcm16_conversion_clips_and_scales():
    pcm = to_pcm16(np.array([0.0, 1.0, -1.0, 2.0, -2.0], dtype=np.float32))
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [0, 32767, -32767, 32767, -32767]


def test_token_is_read_from_the_grok_session(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert load_bearer_token(_auth_file(tmp_path, token="tok-xyz")) == "tok-xyz"


def test_api_key_env_overrides_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "sk-explicit")
    assert load_bearer_token(_auth_file(tmp_path, token="tok-xyz")) == "sk-explicit"


def test_expired_session_is_an_auth_error(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(GrokAuthError, match="expired"):
        load_bearer_token(_auth_file(tmp_path, hours=-1))


def test_missing_session_is_an_auth_error(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(GrokAuthError):
        load_bearer_token(tmp_path / "nope.json")


def test_transcribes_a_turn(tmp_path, monkeypatch):
    handler = _handler(tmp_path, monkeypatch, [FakeResponse(body={"text": "Hello there.", "language": "en"})])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1", turn_revision=0)))
    assert len(out) == 1 and isinstance(out[0], Transcription)
    assert out[0].text == "Hello there."
    assert out[0].error is None


def test_only_new_audio_is_uploaded_after_a_pause(tmp_path, monkeypatch):
    """VAD resends the whole turn each pause; only the new part may go up."""
    handler = _handler(
        tmp_path,
        monkeypatch,
        [
            FakeResponse(body={"text": "hello there", "language": "en"}),
            FakeResponse(body={"text": "and again", "language": "en"}),
        ],
    )
    a = VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1", turn_revision=0)
    b = VADAudio(audio=np.zeros(24000, dtype=np.float32), turn_id="t1", turn_revision=1)

    assert list(handler.process(a))[0].text == "hello there"
    assert list(handler.process(b))[0].text == "hello there and again"
    assert handler.sent == [16000, 8000], "the second request must carry only the new 8000 samples"


def test_a_new_turn_uploads_from_the_start(tmp_path, monkeypatch):
    handler = _handler(
        tmp_path,
        monkeypatch,
        [FakeResponse(body={"text": "one"}), FakeResponse(body={"text": "two"})],
    )
    list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))
    out = list(handler.process(VADAudio(audio=np.zeros(8000, dtype=np.float32), turn_id="t2")))
    assert out[0].text == "two"
    assert handler.sent == [16000, 8000]


def test_http_error_degrades_to_an_empty_transcript(tmp_path, monkeypatch):
    handler = _handler(tmp_path, monkeypatch, [FakeResponse(status_code=500, text="boom")])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))
    assert out[0].text == ""
    assert out[0].error == "stt_failed"


def test_rejected_credential_is_reported_as_auth_failure(tmp_path, monkeypatch):
    handler = _handler(tmp_path, monkeypatch, [FakeResponse(status_code=401, text="nope")])
    out = list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))
    assert out[0].error == "stt_auth_failed"


def test_a_failed_request_does_not_advance_the_turn_mark(tmp_path, monkeypatch):
    """Otherwise the audio in the failed request would never be transcribed."""
    handler = _handler(
        tmp_path,
        monkeypatch,
        [FakeResponse(status_code=500, text="boom"), FakeResponse(body={"text": "recovered"})],
    )
    list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1", turn_revision=0)))
    out = list(handler.process(VADAudio(audio=np.zeros(24000, dtype=np.float32), turn_id="t1", turn_revision=1)))
    assert out[0].text == "recovered"
    assert handler.sent == [16000, 24000], "after a failure the next request must resend from the start"


def test_formatting_is_requested_only_with_an_explicit_language(tmp_path, monkeypatch):
    """The endpoint rejects format=true unless a language is given.

    A live call returned 400 "Field 'language' is required when 'format' is
    true" -- the mocked tests could not see it, so pin the request shape here.
    """
    sent = []

    def capture(url, headers, files, data):
        sent.append(dict(data))
        return FakeResponse(body={"text": "ok"})

    handler = _handler(tmp_path, monkeypatch, [])
    handler._client.post = capture

    handler.start_language = "en"
    list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t1")))
    assert sent[-1]["language"] == "en"
    assert sent[-1]["format"] == "true", "a fixed language should buy punctuation"

    handler._reset_turn_cache()
    handler.start_language = "auto"
    list(handler.process(VADAudio(audio=np.zeros(16000, dtype=np.float32), turn_id="t2")))
    assert "language" not in sent[-1]
    assert "format" not in sent[-1], "auto-detect must not ask for formatting"
