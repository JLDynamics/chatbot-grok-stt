import Foundation

/// Fast, stateful noise gate for 16 kHz PCM16 mono mic audio.
/// Zeroes out room noise, laptop fans, and breath during pauses so VAD
/// is never falsely triggered while waiting for the assistant to reply.
public final class PCM16NoiseGate: @unchecked Sendable {
    private let lock = NSLock()
    private let sampleRate: Double = 16_000.0
    private let bytesPerSample: Int = 2

    public var isEnabled: Bool = false
    public var thresholdDBFS: Double = -55.0

    private static let attackMs = 5.0
    private static let holdMs = 250.0
    private static let releaseMs = 80.0

    private var gain: Double = 1.0
    private var holdSamplesRemaining: Int = 0

    public init(thresholdDBFS: Double = -55.0, isEnabled: Bool = false) {
        self.thresholdDBFS = thresholdDBFS
        self.isEnabled = isEnabled
    }

    public func reset() {
        lock.lock()
        defer { lock.unlock() }
        gain = isEnabled ? 0.0 : 1.0
        holdSamplesRemaining = 0
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
        var samples = [Int16](repeating: 0, count: sampleCount)
        var sumSquares: Double = 0.0

        bytes.withUnsafeBytes { raw in
            let ptr = raw.bindMemory(to: Int16.self)
            for i in 0..<sampleCount {
                let s = ptr[i]
                samples[i] = s
                let norm = Double(s) / 32768.0
                sumSquares += norm * norm
            }
        }

        let rms = sqrt(sumSquares / Double(sampleCount))
        let threshold = pow(10.0, thresholdDBFS / 20.0)
        let signalAboveThreshold = rms >= threshold

        let targetGain: Double
        if signalAboveThreshold {
            holdSamplesRemaining = Int((Self.holdMs / 1000.0) * sampleRate)
            targetGain = 1.0
        } else if holdSamplesRemaining > 0 {
            holdSamplesRemaining = max(0, holdSamplesRemaining - sampleCount)
            targetGain = 1.0
        } else {
            targetGain = 0.0
        }

        let attackCoeff = exp(-1.0 / ((Self.attackMs / 1000.0) * sampleRate))
        let releaseCoeff = exp(-1.0 / ((Self.releaseMs / 1000.0) * sampleRate))

        var output = [UInt8]()
        output.reserveCapacity(bytes.count)

        for s in samples {
            let coeff = targetGain > gain ? attackCoeff : releaseCoeff
            gain = targetGain + (gain - targetGain) * coeff
            let effectiveGain = gain < 0.005 ? 0.0 : gain
            let scaled = Int((Double(s) * effectiveGain).rounded())
            let clamped = Int16(max(Int(Int16.min), min(Int(Int16.max), scaled)))
            let u16 = UInt16(bitPattern: clamped)
            output.append(UInt8(u16 & 0xff))
            output.append(UInt8((u16 >> 8) & 0xff))
        }

        let isOpen = signalAboveThreshold || holdSamplesRemaining > 0 || gain > 0.05
        return (output, isOpen)
    }
}
