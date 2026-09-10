"""Speech-to-text through xAI's hosted endpoint instead of a local model.

Trades 4 GB of resident weights and the GPU work for a network round trip.
Measured against local Parakeet on the same 5.7s clip: ~370ms per turn versus
~60ms, no resident memory instead of 4.03 GB, and a transcript that arrives
punctuated and capitalized, which Parakeet does not provide.

Authentication reuses the Grok CLI's session so there is nothing metered to
buy. That path is undocumented -- xAI publishes API-key auth for this endpoint
and says nothing about the CLI's token -- so it may stop working without
notice, and every failure here degrades to an empty transcript with an error
code rather than taking the turn down.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Iterator, Optional

import httpx
import numpy as np

from chatbot.pipeline.handler_types import STTIn, STTOut
from chatbot.pipeline.messages import Transcription
from chatbot.STT.base_stt_handler import BaseSTTHandler

logger = logging.getLogger(__name__)


class GrokAuthError(RuntimeError):
    """No usable credential. Distinct from a request that failed in flight."""


def load_bearer_token(auth_path: str | pathlib.Path) -> str:
    """Newest unexpired Grok CLI token, or XAI_API_KEY when that is set.

    Read per request rather than cached: the CLI session lasts hours, and a
    cached token would strand a long-running server with no obvious cause.
    """
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if api_key:
        return api_key

    path = pathlib.Path(auth_path).expanduser()
    try:
        entries = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise GrokAuthError(f"no Grok session at {path} and XAI_API_KEY is unset") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GrokAuthError(f"could not read {path}: {exc}") from exc

    best: tuple[datetime, str] | None = None
    for entry in entries.values() if isinstance(entries, dict) else []:
        if not isinstance(entry, dict):
            continue
        token = entry.get("key")
        if not token:
            continue
        try:
            expires = datetime.fromisoformat(str(entry.get("expires_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if best is None or expires > best[0]:
            best = (expires, token)

    if best is None:
        raise GrokAuthError(f"no token in {path}")
    if best[0] <= datetime.now(timezone.utc):
        raise GrokAuthError(f"Grok session expired at {best[0].isoformat()}; run `grok` to refresh it")
    return best[1]


def to_pcm16(audio: np.ndarray) -> bytes:
    """Float samples in [-1, 1] to the raw little-endian PCM the endpoint wants."""
    clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


class GrokSTTHandler(BaseSTTHandler):
    """Transcribe finalized utterances with xAI's batch endpoint."""

    def setup(
        self,
        url: str = "https://api.x.ai/v1/stt",
        language: Optional[str] = "en",
        timeout_s: float = 20.0,
        auth_path: str = "~/.grok/auth.json",
        gen_kwargs: dict[str, Any] | None = None,
    ) -> None:
        # The registry strips the ``grok_stt`` prefix from the argument names
        # before calling this, so these are the short forms.
        self.url = url
        self.start_language = language
        self.last_language = language if language and language != "auto" else "en"
        self.timeout_s = timeout_s
        self.auth_path = auth_path
        self.sample_rate = 16000
        self._client = httpx.Client(timeout=timeout_s)
        self._reset_turn_cache()
        # Surface a missing session at startup rather than on the first spoken
        # turn, where it would look like the microphone failed.
        try:
            load_bearer_token(self.auth_path)
            logger.info("Grok STT ready (%s)", self.url)
        except GrokAuthError as exc:
            logger.warning("Grok STT has no usable credential yet: %s", exc)

    def _reset_turn_cache(self) -> None:
        self._turn_id: str | None = None
        self._sent_samples = 0
        self._turn_text = ""

    def warmup(self) -> None:
        """No weights to load; the first request pays only the TLS handshake."""

    def process(self, vad_audio: STTIn) -> Iterator[STTOut]:
        audio = np.asarray(vad_audio.audio, dtype=np.float32)
        started_s = perf_counter()

        # VAD re-sends the whole turn after each pause, so upload only what is
        # past the mark. The resume point is a detected end of speech, so it
        # sits in silence rather than mid-word. If a revision was dropped, its
        # audio is still past the mark and goes up with the next one.
        reuse = (
            vad_audio.turn_id is not None and vad_audio.turn_id == self._turn_id and 0 < self._sent_samples < len(audio)
        )
        tail = audio[self._sent_samples :] if reuse else audio

        text, language, error = self._transcribe(tail)
        if error is None:
            text = " ".join(part for part in ((self._turn_text if reuse else ""), text) if part)
            if vad_audio.turn_id is None:
                self._reset_turn_cache()
            else:
                self._turn_id = vad_audio.turn_id
                self._sent_samples = len(audio)
                self._turn_text = text
            if language:
                self.last_language = language

        logger.info(
            "Grok STT turn=%s rev=%s sent=%.1fs total=%.3fs chars=%d%s",
            vad_audio.turn_id,
            vad_audio.turn_revision,
            len(tail) / self.sample_rate,
            perf_counter() - started_s,
            len(text),
            f" error={error}" if error else "",
        )

        yield Transcription(
            text=text,
            language_code=self.last_language,
            turn_id=vad_audio.turn_id,
            turn_revision=vad_audio.turn_revision,
            speech_stopped_at_s=vad_audio.created_at_s,
            error=error,
            active_speech_ms=vad_audio.active_speech_ms,
        )

    def _transcribe(self, audio: np.ndarray) -> tuple[str, str | None, str | None]:
        """(text, language, error). Any failure yields empty text plus a code."""
        if len(audio) == 0:
            return "", None, None
        try:
            token = load_bearer_token(self.auth_path)
        except GrokAuthError as exc:
            logger.error("Grok STT auth failed: %s", exc)
            return "", None, "stt_auth_failed"

        data: dict[str, Any] = {
            "audio_format": "pcm",
            "sample_rate": str(self.sample_rate),
        }
        # Formatting is what gives punctuation and capitals, and the endpoint
        # rejects it without an explicit language ("Field 'language' is required
        # when 'format' is true"). So a fixed language buys punctuation, and
        # "auto" trades it for detection.
        if self.start_language and self.start_language != "auto":
            data["language"] = self.start_language
            data["format"] = "true"

        try:
            response = self._client.post(
                self.url,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("audio.pcm", to_pcm16(audio), "application/octet-stream")},
                data=data,
            )
        except httpx.HTTPError as exc:
            logger.error("Grok STT request failed: %s", exc)
            return "", None, "stt_failed"

        if response.status_code == 401:
            logger.error("Grok STT rejected the credential (401)")
            return "", None, "stt_auth_failed"
        if response.status_code != 200:
            logger.error("Grok STT returned %s: %s", response.status_code, response.text[:200])
            return "", None, "stt_failed"

        try:
            body = response.json()
        except ValueError:
            logger.error("Grok STT returned a non-JSON body")
            return "", None, "stt_failed"

        text = str(body.get("text") or body.get("transcript") or "").strip()
        language = body.get("language")
        return text, (str(language) if language else None), None

    def on_session_end(self) -> None:
        super().on_session_end()
        self._reset_turn_cache()
        self.last_language = self.start_language or "en"

    def cleanup(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            client.close()
