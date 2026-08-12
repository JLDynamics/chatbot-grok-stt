"""
Smart Progressive Streaming Handler

Provides frequent partial transcriptions (every 500ms) with:
- Growing window up to 15s for accuracy
- Sentence-boundary-aware window sliding for audio > 15s
- Fixed sentences + active transcription
"""

from dataclasses import dataclass
from typing import Any, Generator

import numpy as np


@dataclass
class PartialTranscription:
    """Result from progressive streaming."""

    fixed_text: str  # Sentences that won't change
    active_text: str  # Current partial transcription
    timestamp: float  # Current position in audio
    is_final: bool  # True if this is the last update


class SmartProgressiveStreamingHandler:
    """
    Smart progressive streaming with sentence-aware window management.

    Strategy:
    1. Emit partial transcriptions every 500ms
    2. Use growing window (up to 15s) for better accuracy
    3. When audio > 15s, slide window using sentence boundaries:
       - Keep completed sentences as "fixed"
       - Only re-transcribe the "active" portion
    """

    def __init__(
        self,
        model: Any,
        emission_interval: float = 0.5,
        max_window_size: float = 15.0,
        sentence_buffer: float = 2.0,
    ) -> None:
        """
        Args:
            model: Parakeet model with sentence alignment
            emission_interval: Emit partial transcription every N seconds (default 500ms)
            max_window_size: Maximum window size before sliding (default 15s)
            sentence_buffer: Keep last N seconds of sentences in active window (default 2s)
        """
        self.model = model
        self.emission_interval = emission_interval
        self.max_window_size = max_window_size
        self.sentence_buffer = sentence_buffer
        self.sample_rate = 16000

        # State for incremental streaming
        self.reset()

    def reset(self) -> None:
        """Reset state for new streaming session."""
        self.fixed_sentences: list[str] = []
        self.fixed_end_time: float = 0.0
        self.last_transcribed_length: int = 0

    def _decode_window(self, audio_window: np.ndarray) -> Any:
        """
        Decode an audio window and return an object with:
          - text: full transcript for the window
          - sentences: list of objects with .text and .end (seconds)
        """
        import mlx.core as mx

        audio_mx = mx.array(audio_window, dtype=mx.float32)
        return self.model.decode_chunk(audio_mx, verbose=False)

    def transcribe_incremental(self, audio: np.ndarray) -> PartialTranscription:
        """
        Transcribe audio incrementally (for live streaming).

        Call this repeatedly with growing audio buffer.
        Returns a single PartialTranscription for current state.
        """
        # Skip if not enough new audio
        current_length = len(audio)
        if current_length < self.sample_rate * 0.5:  # Need at least 500ms
            return PartialTranscription(
                fixed_text=" ".join(self.fixed_sentences),
                active_text="",
                timestamp=current_length / self.sample_rate,
                is_final=False,
            )

        # Skip if no new audio since last transcription
        if current_length == self.last_transcribed_length:
            return PartialTranscription(
                fixed_text=" ".join(self.fixed_sentences),
                active_text="",
                timestamp=current_length / self.sample_rate,
                is_final=False,
            )

        self.last_transcribed_length = current_length

        # Extract window for transcription (from last fixed sentence to end)
        window_start_samples = int(self.fixed_end_time * self.sample_rate)
        audio_window = audio[window_start_samples:]

        # Transcribe current window
        result = self._decode_window(audio_window)

        # Check if window exceeds max_window_size
        window_duration = len(audio_window) / self.sample_rate

        if window_duration >= self.max_window_size and len(result.sentences) > 1:
            # Window is too large - fix some sentences
            cutoff_time = window_duration - self.sentence_buffer

            # Find sentences to fix
            new_fixed_sentences = []
            new_fixed_end_time = self.fixed_end_time

            for sentence in result.sentences:
                sentence_abs_time = self.fixed_end_time + sentence.end

                if sentence.end < cutoff_time:
                    # Fix this sentence
                    new_fixed_sentences.append(sentence.text.strip())
                    new_fixed_end_time = sentence_abs_time
                else:
                    break

            if new_fixed_sentences:
                self.fixed_sentences.extend(new_fixed_sentences)
                self.fixed_end_time = new_fixed_end_time

                # Re-transcribe from new fixed point
                window_start_samples = int(self.fixed_end_time * self.sample_rate)
                audio_window = audio[window_start_samples:]
                result = self._decode_window(audio_window)

        # Build output
        fixed_text = " ".join(self.fixed_sentences)
        active_text = result.text.strip()
        timestamp = len(audio) / self.sample_rate

        return PartialTranscription(fixed_text=fixed_text, active_text=active_text, timestamp=timestamp, is_final=False)

    def transcribe_progressive(self, audio: np.ndarray) -> Generator[PartialTranscription, None, None]:
        """
        Transcribe audio with smart progressive emissions.

        Yields PartialTranscription with:
        - fixed_text: Completed sentences (won't change)
        - active_text: Current partial transcription
        - timestamp: Current position
        - is_final: True for last update
        """
        position = 0  # Start of current window (in samples)
        fixed_sentences = []  # List of completed sentence texts
        fixed_end_time = 0.0  # End time of last fixed sentence

        while position < len(audio):
            # Determine current window end
            remaining = (len(audio) - position) / self.sample_rate

            if remaining <= self.emission_interval:
                # Last chunk - process everything remaining
                window_end = len(audio)
                is_final = True
            else:
                # Regular chunk
                window_end = min(len(audio), position + int(self.emission_interval * self.sample_rate))
                is_final = False

            # Extract window for transcription
            # Window includes: fixed_end_time to current position
            # (we re-transcribe from last fixed sentence to get better context)
            window_start_samples = int(fixed_end_time * self.sample_rate)
            audio_window = audio[window_start_samples:window_end]

            # Transcribe current window
            result = self._decode_window(audio_window)

            # Check if window exceeds max_window_size
            window_duration = (window_end - window_start_samples) / self.sample_rate

            if window_duration >= self.max_window_size and len(result.sentences) > 1:
                # Window is too large - need to fix some sentences
                # Strategy: Keep sentences up to (max_window - sentence_buffer) as fixed
                cutoff_time = window_duration - self.sentence_buffer

                # Find sentences that are completed and before cutoff
                new_fixed_sentences = []
                new_fixed_end_time = fixed_end_time

                for sentence in result.sentences:
                    sentence_abs_time = fixed_end_time + sentence.end

                    if sentence.end < cutoff_time:
                        # This sentence is complete and old enough to fix
                        new_fixed_sentences.append(sentence.text.strip())
                        new_fixed_end_time = sentence_abs_time
                    else:
                        # Stop here - keep remaining sentences as active
                        break

                if new_fixed_sentences:
                    fixed_sentences.extend(new_fixed_sentences)
                    fixed_end_time = new_fixed_end_time

                    # Re-transcribe from new fixed_end_time
                    window_start_samples = int(fixed_end_time * self.sample_rate)
                    audio_window = audio[window_start_samples:window_end]
                    result = self._decode_window(audio_window)

            # Build output
            fixed_text = " ".join(fixed_sentences)
            active_text = result.text.strip()
            timestamp = window_end / self.sample_rate

            yield PartialTranscription(
                fixed_text=fixed_text, active_text=active_text, timestamp=timestamp, is_final=is_final
            )

            # Move to next position
            position = window_end

            if is_final:
                break
