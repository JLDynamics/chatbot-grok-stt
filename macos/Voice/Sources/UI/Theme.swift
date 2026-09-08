import SwiftUI

// Colour tokens for the floating voice window.
// Values match tokens.css and mockup.html exactly — change them in one place only.

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            .sRGB,
            red:   Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8)  & 0xFF) / 255,
            blue:  Double( hex        & 0xFF) / 255,
            opacity: opacity
        )
    }
}

struct Theme {
    let bg: Color
    let surface2: Color
    let border: Color
    let borderStrong: Color

    let text: Color
    let text2: Color
    let text3: Color

    let accent: Color
    let accentDeep: Color
    let accentSoft: Color
    let accentTint: Color
    let onTint: Color

    let danger: Color
    let dangerBorder: Color
    let dangerTint: Color

    // Orb fill, light-to-dark. Drawn as a radial gradient centred at (0.36, 0.30).
    let orbStops: [Color]
    let orbMutedStops: [Color]
    let orbRing: Color

    static let radius: CGFloat = 14
    static let radiusSmall: CGFloat = 4

    static let light = Theme(
        bg:           Color(hex: 0xFBFBF9),
        surface2:     Color(hex: 0xF1EFE8),
        border:       Color(hex: 0xDDDDD6),
        borderStrong: Color(hex: 0xC7C7BE),

        text:  Color(hex: 0x2C2C2A),
        text2: Color(hex: 0x5F5E5A),
        text3: Color(hex: 0x888780),

        accent:     Color(hex: 0x378ADD),
        accentDeep: Color(hex: 0x185FA5),
        accentSoft: Color(hex: 0x85B7EB),
        accentTint: Color(hex: 0xE6F1FB),
        onTint:     Color(hex: 0x0C447C),

        danger:       Color(hex: 0xA32D2D),
        dangerBorder: Color(hex: 0xF09595),
        dangerTint:   Color(hex: 0xFCEBEB),

        orbStops:      [Color(hex: 0x85B7EB), Color(hex: 0x378ADD), Color(hex: 0x185FA5)],
        orbMutedStops: [Color(hex: 0xB4B2A9), Color(hex: 0x888780), Color(hex: 0x5F5E5A)],
        orbRing:       Color(hex: 0x378ADD, opacity: 0.16)
    )

    static let dark = Theme(
        bg:           Color(hex: 0x12161D),
        surface2:     Color(hex: 0xFFFFFF, opacity: 0.07),
        border:       Color(hex: 0xFFFFFF, opacity: 0.12),
        borderStrong: Color(hex: 0xFFFFFF, opacity: 0.22),

        text:  Color(hex: 0xE3E8F2),
        text2: Color(hex: 0x9AA2B2),
        text3: Color(hex: 0x6F7787),

        accent:     Color(hex: 0x5A9BE8),
        accentDeep: Color(hex: 0x3184E2),
        accentSoft: Color(hex: 0x85B7EB),
        accentTint: Color(hex: 0x3184E2, opacity: 0.18),
        onTint:     Color(hex: 0xB5D4F4),

        danger:       Color(hex: 0xE2706F),
        dangerBorder: Color(hex: 0xE2706F, opacity: 0.45),
        dangerTint:   Color(hex: 0xE2706F, opacity: 0.12),

        orbStops:      [Color(hex: 0x8CC6FF), Color(hex: 0x3184E2), Color(hex: 0x134795)],
        orbMutedStops: [Color(hex: 0x7D8595), Color(hex: 0x5F6878), Color(hex: 0x3A4150)],
        orbRing:       Color(hex: 0x3184E2, opacity: 0.20)
    )
}

// Reach the theme from any view: @Environment(\.theme) private var theme
private struct ThemeKey: EnvironmentKey {
    static let defaultValue = Theme.light
}

extension EnvironmentValues {
    var theme: Theme {
        get { self[ThemeKey.self] }
        set { self[ThemeKey.self] = newValue }
    }
}

/// Instant press feedback — no animation delay so controls feel like they click.
struct PressableButtonStyle: ButtonStyle {
    var pressedScale: CGFloat = 0.94

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? pressedScale : 1)
            .opacity(configuration.isPressed ? 0.72 : 1)
    }
}

// The orb's fill. Diameter stays fixed; the ring around it carries the state.
struct OrbGradient: View {
    let stops: [Color]
    var body: some View {
        RadialGradient(
            gradient: Gradient(stops: [
                .init(color: stops[0], location: 0.0),
                .init(color: stops[1], location: 0.55),
                .init(color: stops[2], location: 1.0)
            ]),
            center: UnitPoint(x: 0.36, y: 0.30),
            startRadius: 0,
            endRadius: 46
        )
    }
}
