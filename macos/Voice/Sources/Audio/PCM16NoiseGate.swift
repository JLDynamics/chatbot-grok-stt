import Foundation

/// Cheap close-talk gate for 16 kHz PCM16 mono.
///
/// Apple AGC is off on the capture path so upstairs speech stays quiet.
/// This gate is only a safety net: pass anything above a modest energy
/// floor, and reject loud muffled rumble (through-floor boom). A hard
/// brightness *and* energy rule was zeroing real close speech after
/// Apple's noise suppress, so Silero saw a long padded segment with
/// ~450ms of active speech and dropped the turn.
///
/// Cost is one integer pass per tap — not a per-sample envelope.
public final class PCM16NoiseGate: @unchecked Sendable {
    private let lock = NSLock()
    private let sampleRate: Double = 16_000.0
    private let bytesPerSample: Int = 2

    public var isEnabled: Bool = true
    /// Nearby speech must reach this level to *open* the gate.
    public var thresholdDBFS: Double = -48.0
    /// Veto only very muffled loud energy (sub-bass rumble). Real speech
    /// after Apple NS still sits well above this.
    public var minBrightness: Double = 0.035
    public var confirmChunks: Int = 1

    private static let holdMs = 250.0
    private static let holdMarginDB = 10.0

    private var holdSamplesRemaining: Int = 0
    private var pendingCloseChunks: Int = 0
    private var zeroScratch: [UInt8] = []

    public init(thresholdDBFS: Double = -48.0, isEnabled: Bool = true) {
        self.thresholdDBFS = thresholdDBFS
        self.isEnabled = isEnabled
    }

    public func reset() {
        lock.lock()
        defer { lock.unlock() }
        holdSamplesRemaining = 0
        pendingCloseChunks = 0
    }

    /// Process a chunk of 16-bit little-endian PCM samples.
    /// Returns the gated PCM bytes and whether the gate is currently open.
    public func process(_ bytes: [UInt8]) -> (bytes: [UInt8], isOpen: Bool) {
        guard isEnabled, bytes.count >= bytesPerSample, bytes.count.isMultiple(of: bytesPerSample) else {
            return (bytes, true)
        }

        lock.lock()
        defer { lock.unlock() }

        let sampleCount = bytes.count / bytesPerSample
        var sumSquares: Int64 = 0
        var sumAbsDelta: Int64 = 0
        var previous: Int32 = 0
        bytes.withUnsafeBytes { raw in
            let ptr = raw.bindMemory(to: Int16.self)
            for i in 0..<sampleCount {
                let value = Int32(ptr[i])
                sumSquares += Int64(value) * Int64(value)
                if i > 0 {
                    sumAbsDelta += Int64(abs(value - previous))
                }
                previous = value
            }
        }

        let rms = sqrt(Double(sumSquares) / Double(sampleCount))
        let brightness = Double(sumAbsDelta) / (Double(max(sampleCount - 1, 1)) * max(rms, 1.0))
        let openFloor = pow(10.0, thresholdDBFS / 20.0) * 32768.0
        let holdFloor = pow(10.0, (thresholdDBFS - Self.holdMarginDB) / 20.0) * 32768.0
        let muffledRumble = brightness < minBrightness
        let closeTalk = rms >= openFloor && !muffledRumble
        let alreadyOpen = holdSamplesRemaining > 0

        if closeTalk {
            pendingCloseChunks += 1
        } else {
            pendingCloseChunks = 0
        }

        let confirmed = pendingCloseChunks >= max(confirmChunks, 1)
        if confirmed || (alreadyOpen && rms >= holdFloor) {
            holdSamplesRemaining = Int((Self.holdMs / 1000.0) * sampleRate)
        } else if holdSamplesRemaining > 0 {
            holdSamplesRemaining = max(0, holdSamplesRemaining - sampleCount)
        }

        let isOpen = holdSamplesRemaining > 0
        if isOpen {
            return (bytes, true)
        }
        if zeroScratch.count != bytes.count {
            zeroScratch = [UInt8](repeating: 0, count: bytes.count)
        }
        return (zeroScratch, false)
    }
}
