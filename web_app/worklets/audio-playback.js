// @ts-check
/**
 * AudioWorkletProcessor that plays back Float32 mono samples received from
 * the main thread, upsampling whatever incoming rate the server uses
 * (16 kHz PCM16 for this pipeline) to the AudioContext rate (typically 48 kHz).
 *
 * Resampling uses a bandlimited Lanczos-3 (windowed-sinc) interpolator rather
 * than naive linear interpolation. Linear upsampling leaves "images" of the
 * baseband above the input Nyquist, which the ear hears as a high, gritty
 * layer of noise on top of the voice. The windowed-sinc kernel bandlimits the
 * output and removes that artifact.
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
// A Lanczos-3 kernel spans 3 samples each side of the interpolation point.
const LAN_ORDER = 3;
// Minimum queued audio (ms) before playback starts. Gives a slow,
// near-real-time TTS model a small head start so its output queue
// doesn't run dry between chunks (audible stutter). Higher = smoother but more
// latency; lower = faster first sound but more underruns.
const PRE_BUFFER_MS = 200;
const PRE_BUFFER_MAX_MS = 500;
// Compact the sample buffer once the retired prefix grows past this many
// samples (about 4 s at 16 kHz) so memory stays bounded over a long session.
const COMPACT_THRESHOLD = 65536;

class AudioPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._inputRate = 16000;
    this._stepRatio = this._inputRate / sampleRate;
    this._buf = new Float32Array(0);
    this._start = 0; // absolute index of the oldest retained sample
    this._length = 0;
    this._pos = 0; // absolute read position (fractional, in input samples)
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
            this._pushChunk(data.samples);
            if (!this._playing && this._queuedSamples() >= this._preBufferSamples) {
              this._playing = true;
              this._fadeIn = FADE_FRAMES;
              this._fadeOut = 0;
            }
          }
          break;
        case "clear":
          this._buf = new Float32Array(0);
          this._start = 0;
          this._length = 0;
          this._pos = 0;
          this._fadeOut = FADE_FRAMES;
          this._underrunPending = false;
          break;
      }
    };
  }

  /** Append a chunk of input samples to the tail of the play buffer. */
  _pushChunk(samples) {
    this._compactIfNeeded();
    const needed = this._length + samples.length;
    if (this._buf.length < needed) {
      const grown = new Float32Array(Math.max(needed, this._buf.length * 2, 4096));
      grown.set(this._buf.subarray(0, this._length));
      this._buf = grown;
    }
    this._buf.set(samples, this._length);
    this._length += samples.length;
  }

  /** Drop the consumed prefix once it grows large, keeping the read window. */
  _compactIfNeeded() {
    if (this._start < COMPACT_THRESHOLD) return;
    const keep = this._buf.subarray(this._start, this._length);
    this._buf = keep.slice ? keep.slice() : keep;
    this._length -= this._start;
    this._pos -= this._start;
    this._start = 0;
  }

  /** Absolute input-sample index -> value, clamped at the buffer edges. */
  _sampleAt(idx) {
    if (this._length === 0) return 0;
    if (idx < this._start) return this._buf[0];
    if (idx >= this._start + this._length) return this._buf[this._length - 1];
    return this._buf[idx - this._start];
  }

  /** Lanczos-3 windowed-sinc kernel. */
  _lanczos(x) {
    if (x === 0) return 1;
    const ax = Math.abs(x);
    if (ax >= LAN_ORDER) return 0;
    const px = Math.PI * x;
    const px3 = px / LAN_ORDER;
    return (Math.sin(px) / px) * (Math.sin(px3) / px3);
  }

  /** Bandlimited interpolation at fractional input position `pos`. */
  _interpolate(pos) {
    const base = Math.floor(pos);
    const frac = pos - base;
    let acc = 0;
    let norm = 0;
    // Lanczos-3 support: samples base-2 .. base+3 for a fractional offset in [0,1).
    for (let j = -2; j <= 3; j++) {
      const w = this._lanczos(j - frac);
      if (w === 0) continue;
      acc += w * this._sampleAt(base + j);
      norm += w;
    }
    if (norm === 0) return this._sampleAt(base);
    return acc / norm;
  }

  /** Number of unplayed input samples (relative to the read position). */
  _queuedSamples() {
    const consumed = this._playing ? Math.floor(this._pos) - this._start : 0;
    return Math.max(0, this._length - consumed);
  }

  process(_, outputs) {
    const channels = outputs[0];
    if (!channels || channels.length === 0) return true;
    const out = channels[0];
    if (!this._playing && this._queuedSamples() >= this._preBufferSamples) {
      this._playing = true;
      this._fadeIn = FADE_FRAMES;
      this._fadeOut = 0;
    }

    for (let i = 0; i < out.length; i++) {
      let sample = 0;

      if (this._playing) {
        // If we've consumed everything buffered, ramp out and mark an underrun.
        if (this._pos >= this._start + this._length) {
          if (this._fadeOut === 0) {
            this._fadeOut = FADE_FRAMES;
            this._underrunPending = true;
          }
          sample = this._lastSample;
        } else {
          sample = this._interpolate(this._pos);
          this._lastSample = sample;
          this._pos += this._stepRatio;
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
