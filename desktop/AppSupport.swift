import AppKit
import SwiftUI

enum DesignTokens {
    static let ink = Color(nsColor: .labelColor)
    static let mutedInk = Color(nsColor: .secondaryLabelColor)
    static let onInteraction = Color(nsColor: .selectedMenuItemTextColor)
    // Apple Home-style restraint: one system interaction/resource accent.
    // Green, orange, and red are reserved for semantic status only.
    static let interaction = Color(nsColor: .controlAccentColor)
    // Resource categories stay neutral; labels and SF Symbols carry meaning.
    static let cpu = mutedInk
    static let memory = mutedInk
    static let gpu = mutedInk
    static let network = mutedInk
    static let success = Color(nsColor: .systemGreen)
    static let warning = Color(nsColor: .systemOrange)
    static let danger = Color(nsColor: .systemRed)
    static let selection = Color(nsColor: .unemphasizedSelectedContentBackgroundColor)
    static let surface = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.145, green: 0.145, blue: 0.16, alpha: 1)
            : NSColor(srgbRed: 1.00, green: 1.00, blue: 1.00, alpha: 1)
    })
    static let surfaceStroke = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(white: 1, alpha: 0.11)
            : NSColor(white: 0, alpha: 0.07)
    })
    static let ambientSmoke = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.095, green: 0.095, blue: 0.105, alpha: 1)
            : NSColor(srgbRed: 0.945, green: 0.945, blue: 0.96, alpha: 1)
    })
    static let glassSmoke = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.125, green: 0.125, blue: 0.14, alpha: 1)
            : NSColor(srgbRed: 0.975, green: 0.975, blue: 0.985, alpha: 1)
    })
}

enum DashboardSection: Hashable {
    case resources
    case leases
    case settings
}

struct AmbientBackground: View {
    var body: some View {
        DesignTokens.ambientSmoke
            .ignoresSafeArea()
    }
}

struct SoftButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    let tint: Color
    let foreground: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(foreground.opacity(isEnabled ? 1 : 0.52))
            .padding(.horizontal, 13)
            .frame(height: 31)
            .background(
                tint.opacity(isEnabled ? (configuration.isPressed ? 0.72 : 0.94) : 0.22),
                in: Capsule()
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct SoftIconButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.74 : 1) : 0.46),
                in: Circle()
            )
            .overlay(Circle().stroke(DesignTokens.surfaceStroke, lineWidth: 1))
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
    }
}

struct PrimaryActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(DesignTokens.onInteraction.opacity(isEnabled ? 1 : 0.58))
            .padding(.horizontal, 15)
            .frame(height: 34)
            .background(
                DesignTokens.interaction.opacity(isEnabled ? (configuration.isPressed ? 0.78 : 1) : 0.26),
                in: Capsule()
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct SecondaryActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .padding(.horizontal, 14)
            .frame(height: 34)
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.74 : 1) : 0.46),
                in: Capsule()
            )
            .overlay(Capsule().stroke(DesignTokens.surfaceStroke.opacity(isEnabled ? 1 : 0.50), lineWidth: 1))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct IconActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .frame(width: 34, height: 34)
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.74 : 1) : 0.46),
                in: Circle()
            )
            .overlay(Circle().stroke(DesignTokens.surfaceStroke.opacity(isEnabled ? 1 : 0.50), lineWidth: 1))
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
    }
}

struct VisualEffect: NSViewRepresentable {
    let material: NSVisualEffectView.Material
    let blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}

extension View {
    func fieldLabel() -> some View {
        font(.system(size: 12, weight: .semibold))
            .foregroundStyle(DesignTokens.ink)
    }

    @ViewBuilder
    func spatialGlass<S: Shape>(in shape: S) -> some View {
        background(DesignTokens.surface, in: shape)
            .overlay(shape.stroke(DesignTokens.surfaceStroke, lineWidth: 1))
    }

    func spatialContentSurface() -> some View {
        background(DesignTokens.glassSmoke)
    }
}
