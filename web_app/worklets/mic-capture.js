// @ts-check
/**
 * AudioWorkletProcessor that resamples the AudioContext rate (typically 48 kHz)
 * down to 16 kHz, packs the result as little-endian Int16 PCM, and posts it
 * back to the main thread in fixed-size chunks.
 *
 * The Chatbot voice WebSocket route expects the
 * `input_audio_buffer.append` payload at 16 kHz PCM16 mono.
 *
 * Design notes:
 *   - 48 -> 16 is an exact 3:1 ratio so we use a 3-tap boxcar average as a
 *     cheap low-pass before decimating. Good enough for voice STT; we lose
 *     a tiny bit of >8 kHz content which the pipeline discards anyway.
 *   - Any other input rate (e.g. 44.1 kHz) is low-passed with a 2nd-order
 *     Butterworth at 7.5 kHz, then linearly interpolated. Without the LPF,
 *     decimation aliases ultrasonic energy into the speech band.
 *   - Output frames are emitted at the cadence dictated by `chunkMs`
 *     (default 40 ms = 640 samples = 1280 bytes). The OpenAI Realtime
 *     server batches incoming audio so the cadence is flexible; 20-100 ms
 *     is the sweet spot.
 *   - Float -> Int16 saturates to [-1, 1] before scaling.
 *   - Optional noise gate: 8 ms sub-window RMS decides open/closed so a
 *     quiet 40 ms chunk that starts with a word onset still opens. The
 *     gain ramps (fast attack, hold, slow release) so onsets survive and
 *     quiet tails don't click. The gate only affects the audio we SEND;
 *     the main-thread visualiser taps the raw mic separately.
 *   - Scratch storage is a grow-once linear buffer (copyWithin to consume)
 *     so the worklet does not allocate on the audio thread after warmup.
 */

const TARGET_RATE = 16000;
const DEFAULT_CHUNK_MS = 40;
const INITIAL_SCRATCH = 8192;
// Gate envelope timing (fixed; only the threshold is user-tunable).
const GATE_ATTACK_MS = 5; // open almost instantly so word onsets survive
const GATE_HOLD_MS = 250; // stay open this long after the level drops back under
const GATE_RELEASE_MS = 80; // then fade closed over this long (no click)
const GATE_WINDOW_MS = 8; // sub-chunk window used to catch short onsets
const LPF_CUTOFF_HZ = 7500; // below 16 kHz Nyquist, with a little margin

class MicCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const chunkMs = options?.processorOptions?.chunkMs ?? DEFAULT_CHUNK_MS;
    this._inputRate = sampleRate;
    this._ratio = this._inputRate / TARGET_RATE;
    this._chunkSamples16k = Math.round((TARGET_RATE * chunkMs) / 1000);
    this._scratch = new Float32Array(INITIAL_SCRATCH);
    this._scratchLen = 0;
    this._decimated = new Float32Array(this._chunkSamples16k);
    this._enabled = true;
    this._exact48k = Math.abs(this._ratio - 3) < 1e-6;

    // Noise gate state. Disabled by default (pure passthrough).
    this._gateEnabled = false;
    this._thresholdLin = 0; // linear amplitude; signal RMS must exceed this to open
    this._gateGain = 1; // smoothed gain currently applied
    this._holdRemaining = 0; // samples left before the gate may start closing
    this._attackCoef = Math.exp(-1 / ((GATE_ATTACK_MS / 1000) * TARGET_RATE));
    this._releaseCoef = Math.exp(-1 / ((GATE_RELEASE_MS / 1000) * TARGET_RATE));
    this._holdSamples = Math.round((GATE_HOLD_MS / 1000) * TARGET_RATE);
    this._gateWindow = Math.max(1, Math.round((GATE_WINDOW_MS / 1000) * TARGET_RATE));

    // Butterworth low-pass for non-48 kHz capture (anti-alias before decimate).
    this._lpEnabled = !this._exact48k;
    this._lpZ1 = 0;
    this._lpZ2 = 0;
    if (this._lpEnabled) {
      const K = Math.tan(Math.PI * LPF_CUTOFF_HZ / this._inputRate);
      const norm = 1 / (1 + Math.SQRT2 * K + K * K);
      this._lpB0 = K * K * norm;
      this._lpB1 = 2 * this._lpB0;
      this._lpB2 = this._lpB0;
      this._lpA1 = 2 * (K * K - 1) * norm;
      this._lpA2 = (1 - Math.SQRT2 * K + K * K) * norm;
    }

    this.port.onmessage = (e) => {
      const data = e.data;
      if (data?.kind === "enable") this._enabled = !!data.value;
      else if (data?.kind === "gate") {
        this._gateEnabled = !!data.enabled;
        // dB -> linear amplitude. When off, threshold 0 keeps the gate open.
        this._thresholdLin = data.enabled ? Math.pow(10, data.thresholdDb / 20) : 0;
      }
    };
  }

  /**
   * Grow the scratch buffer if needed. After warmup this is a no-op.
   * @param {number} needed
   */
  _ensureScratch(needed) {
    if (needed <= this._scratch.length) return;
    const next = new Float32Array(Math.max(this._scratch.length * 2, needed));
    next.set(this._scratch.subarray(0, this._scratchLen));
    this._scratch = next;
  }

  /**
   * Append `incoming` to the internal scratch buffer, then emit as many
   * full output chunks as we have material for.
   * @param {Float32Array} incoming
   */
  _ingest(incoming) {
    if (incoming.length === 0) return;
    this._ensureScratch(this._scratchLen + incoming.length);
    if (this._lpEnabled) {
      let z1 = this._lpZ1;
      let z2 = this._lpZ2;
      const b0 = this._lpB0;
      const b1 = this._lpB1;
      const b2 = this._lpB2;
      const a1 = this._lpA1;
      const a2 = this._lpA2;
      const dest = this._scratch;
      let o = this._scratchLen;
      for (let i = 0; i < incoming.length; i++) {
        const x = incoming[i];
        const y = b0 * x + z1;
        z1 = b1 * x - a1 * y + z2;
        z2 = b2 * x - a2 * y;
        dest[o++] = y;
      }
      this._lpZ1 = z1;
      this._lpZ2 = z2;
      this._scratchLen = o;
    } else {
      this._scratch.set(incoming, this._scratchLen);
      this._scratchLen += incoming.length;
    }
    this._maybeEmit();
  }

  /**
   * True if any 8 ms window (or the whole chunk if shorter) is above threshold.
   * Catches word onsets that a single 40 ms RMS would average away.
   * @param {Float32Array} dec
   * @param {number} n
   * @param {number} threshold
   */
  _anyWindowOpen(dec, n, threshold) {
    const w = this._gateWindow;
    for (let start = 0; start < n; start += w) {
      const end = Math.min(n, start + w);
      let sumSq = 0;
      for (let i = start; i < end; i++) sumSq += dec[i] * dec[i];
      if (Math.sqrt(sumSq / (end - start)) >= threshold) return true;
    }
    return false;
  }

  _maybeEmit() {
    const r = this._ratio;
    const n = this._chunkSamples16k;
    const needIn = Math.ceil(n * r);
    const dec = this._decimated;
    const src = this._scratch;
    while (this._scratchLen >= needIn) {
      // 1. Decimate to 16 kHz floats and accumulate energy for the gate/meter.
      let sumSq = 0;
      if (this._exact48k) {
        // 48 kHz -> 16 kHz fast path with boxcar lowpass.
        for (let i = 0; i < n; i++) {
          const idx = i * 3;
          const s = (src[idx] + src[idx + 1] + src[idx + 2]) / 3;
          dec[i] = s;
          sumSq += s * s;
        }
      } else {
        // Generic path: linear interpolation of the already-lowpassed buffer.
        for (let i = 0; i < n; i++) {
          const srcPos = i * r;
          const idx = Math.floor(srcPos);
          const frac = srcPos - idx;
          const a = src[idx];
          const b = idx + 1 < this._scratchLen ? src[idx + 1] : a;
          const s = a + (b - a) * frac;
          dec[i] = s;
          sumSq += s * s;
        }
      }
      const rms = Math.sqrt(sumSq / n);

      // 2. Decide the gate target for this chunk, then ramp sample-by-sample.
      let target = 1;
      if (this._gateEnabled) {
        if (this._anyWindowOpen(dec, n, this._thresholdLin)) {
          this._holdRemaining = this._holdSamples; // re-arm the hold
        } else if (this._holdRemaining > 0) {
          this._holdRemaining -= n; // coasting through the hold window
        } else {
          target = 0;
        }
      }

      // 3. Apply the (smoothed) gain and pack to Int16.
      const out = new Int16Array(n);
      let gain = this._gateGain;
      for (let i = 0; i < n; i++) {
        const coef = target > gain ? this._attackCoef : this._releaseCoef;
        gain = target + (gain - target) * coef;
        const s = dec[i] * gain;
        const clamped = s < -1 ? -1 : s > 1 ? 1 : s;
        out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      }
      this._gateGain = gain;

      // Shift unused samples to the front without allocating.
      const consumed = Math.floor(n * r);
      const remain = this._scratchLen - consumed;
      if (remain > 0) src.copyWithin(0, consumed, this._scratchLen);
      this._scratchLen = remain;

      // Live input level for the Settings meter (raw RMS, pre-gate).
      this.port.postMessage({ kind: "level", rms });

      if (this._enabled) {
        this.port.postMessage(out.buffer, [out.buffer]);
      }
      // When disabled (mic muted) we silently consume input so the worklet
      // stays alive and the buffer never grows unbounded.
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0 || !input[0]) return true;
    const mono = input[0];
    if (mono.length > 0) this._ingest(mono);
    return true;
  }
}

registerProcessor("mic-capture", MicCaptureProcessor);
