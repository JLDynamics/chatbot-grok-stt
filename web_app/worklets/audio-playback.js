// @ts-check
/**
 * AudioWorkletProcessor that plays back Float32 mono samples received from
 * the main thread, upsampling whatever incoming rate the server uses
 * (16 kHz PCM16 for this pipeline) to the AudioContext rate (typically 48 kHz).
 *
 * Lifecycle / messaging:
 *
 *   main -> worklet:
 *     { kind: "config", inputRate: 16000 }              one-shot at startup
 *     { kind: "audio", samples: Float32Array }          (transferable) per chunk
 *     { kind: "clear" }                                 wipe queue (barge-in)
 *
 *   worklet -> main:
 *     { kind: "stats", queuedMs, played }               every ~250 ms
 *     { kind: "underrun" }                              every time the queue
 *                                                      runs dry mid-playback
 *
 * Underrun strategy: output silence. We do NOT hold the last sample (that
 * tends to produce audible clicks/buzzes when long gaps appear between
 * TTS chunks). A short ramp-out + ramp-in at boundaries would be nicer but
 * the server's 30 ms cadence makes underruns visible only at end of turn.
 */

const STATS_INTERVAL_FRAMES = 12000;
const FADE_FRAMES = 32;
// Minimum queued audio (ms) before playback starts. Gives a slow,
// near-real-time TTS model (e.g. CSM) a small head start so its output queue
// doesn't run dry between chunks (audible stutter). Higher = smoother but more
// latency; lower = faster first sound but more underruns.
const PRE_BUFFER_MS = 200;
const PRE_BUFFER_MAX_MS = 500;

class AudioPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._inputRate = 16000;
    this._stepRatio = this._inputRate / sampleRate;
    this._queue = [];
    this._readIdx = 0;
    this._fracPos = 0;
    this._playing = false;
    this._framesSinceStats = 0;
    this._totalPlayed = 0;
    this._fadeIn = 0;
    this._fadeOut = 0;
    this._underrunPending = false;
    this._lastSample = 0;
    this._preBufferMs = PRE_BUFFER_MS;
    this._preBufferSamples = Math.round((this._inputRate * this._preBufferMs) / 1000);

    this.port.onmessage = (e) => {
      const data = e.data;
      if (!data || typeof data !== "object") return;
      switch (data.kind) {
        case "config":
          if (typeof data.inputRate === "number" && data.inputRate > 0) {
            this._inputRate = data.inputRate;
            this._stepRatio = this._inputRate / sampleRate;
            this._preBufferSamples = Math.round((this._inputRate * this._preBufferMs) / 1000);
          }
          break;
        case "audio":
          if (data.samples instanceof Float32Array && data.samples.length > 0) {
            this._queue.push(data.samples);
            if (!this._playing && this._queuedSamples() >= this._preBufferSamples) {
              this._playing = true;
              this._fadeIn = FADE_FRAMES;
              this._fadeOut = 0;
            }
          }
          break;
        case "clear":
          this._queue.length = 0;
          this._readIdx = 0;
          this._fracPos = 0;
          this._fadeOut = FADE_FRAMES;
          this._underrunPending = false;
          break;
      }
    };
  }

  _queuedSamples() {
    let total = -this._readIdx;
    for (const buf of this._queue) total += buf.length;
    return Math.max(0, total);
  }

  /** Linear-interp read at the current fractional position. */
  _readInterpolated() {
    if (this._queue.length === 0) return null;
    const head = this._queue[0];
    const idx = this._readIdx;
    const frac = this._fracPos;

    let a = head[idx];
    let b;
    if (idx + 1 < head.length) {
      b = head[idx + 1];
    } else if (this._queue.length > 1) {
      b = this._queue[1][0];
    } else {
      b = a;
    }
    return a + (b - a) * frac;
  }

  /** Advance the read position by `stepRatio`; pop consumed buffers. */
  _advance() {
    this._fracPos += this._stepRatio;
    while (this._fracPos >= 1) {
      this._fracPos -= 1;
      this._readIdx += 1;
    }
    while (this._queue.length > 0 && this._readIdx >= this._queue[0].length) {
      this._readIdx -= this._queue[0].length;
      this._queue.shift();
    }
  }

  process(_, outputs) {
    const channels = outputs[0];
    if (!channels || channels.length === 0) return true;
    // The node is created with outputChannelCount: [1]; ignore extra channels.
    const out = channels[0];
    if (!this._playing && this._queuedSamples() >= this._preBufferSamples) {
      this._playing = true;
      this._fadeIn = FADE_FRAMES;
      this._fadeOut = 0;
    }

    for (let i = 0; i < out.length; i++) {
      let sample = 0;

      if (this._playing) {
        const v = this._readInterpolated();
        if (v === null) {
          // Underrun: linear ramp-out (same slope as a barge-in fade).
          if (this._fadeOut === 0) {
            this._fadeOut = FADE_FRAMES;
            this._underrunPending = true;
          }
          sample = this._lastSample;
        } else {
          sample = v;
          this._lastSample = v;
          this._advance();
        }

        if (this._fadeIn > 0) {
          const gain = 1 - this._fadeIn / FADE_FRAMES;
          sample *= gain;
          this._fadeIn -= 1;
        }
        if (this._fadeOut > 0) {
          const gain = this._fadeOut / FADE_FRAMES;
          sample *= gain;
          this._fadeOut -= 1;
          if (this._fadeOut === 0) {
            this._playing = false;
            this._lastSample = 0;
            if (this._underrunPending) {
              this._underrunPending = false;
              if (this._preBufferMs < PRE_BUFFER_MAX_MS) {
                this._preBufferMs = Math.min(PRE_BUFFER_MAX_MS, this._preBufferMs + 150);
                this._preBufferSamples = Math.round((this._inputRate * this._preBufferMs) / 1000);
              }
              this.port.postMessage({ kind: "underrun" });
            }
          }
        }

        this._totalPlayed += 1;
      }

      out[i] = sample;
    }

    this._framesSinceStats += out.length;
    if (this._framesSinceStats >= STATS_INTERVAL_FRAMES) {
      this._framesSinceStats = 0;
      const queuedSamples = this._queuedSamples();
      const queuedMs = (queuedSamples / this._inputRate) * 1000;
      this.port.postMessage({ kind: "stats", queuedMs, played: this._totalPlayed });
    }

    return true;
  }
}

registerProcessor("audio-playback", AudioPlaybackProcessor);
