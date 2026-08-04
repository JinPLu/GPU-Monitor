import AppKit
import SwiftUI

enum DesignTokens {
    static let ink = Color(nsColor: .labelColor)
    static let mutedInk = Color(nsColor: .secondaryLabelColor)
    static let onInteraction = Color(nsColor: .selectedMenuItemTextColor)
    static let interaction = Color(nsColor: .controlAccentColor)
    static let success = Color(nsColor: .systemGreen)
    static let warning = Color(nsColor: .systemOrange)
    static let danger = Color(nsColor: .systemRed)
    static let selection = Color(nsColor: .unemphasizedSelectedContentBackgroundColor)
    static let surface = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.13, green: 0.13, blue: 0.14, alpha: 1)
            : NSColor(srgbRed: 0.99, green: 0.99, blue: 1.00, alpha: 1)
    })
    static let surfaceStroke = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(white: 1, alpha: 0.14)
            : NSColor(white: 0, alpha: 0.08)
    })
    static let ambientSmoke = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.10, green: 0.10, blue: 0.11, alpha: 1)
            : NSColor(srgbRed: 0.96, green: 0.96, blue: 0.97, alpha: 1)
    })
    static let glassSmoke = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.12, green: 0.12, blue: 0.13, alpha: 1)
            : NSColor(srgbRed: 0.98, green: 0.98, blue: 0.99, alpha: 1)
    })
    static let surfaceShadow = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return NSColor(white: 0, alpha: isDark ? 0.22 : 0.08)
    })
}

enum DesktopAssets {
    static let computeStudio: NSImage? = {
        guard let url = Bundle.main.url(forResource: "ai-compute-studio-v1", withExtension: "png") else {
            return nil
        }
        return NSImage(contentsOf: url)
    }()
}

enum DashboardSection: Hashable {
    case overview
    case serverPool
    case leases
    case settings
}

struct AmbientBackground: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        ZStack {
            DesignTokens.ambientSmoke.ignoresSafeArea()

            if let image = DesktopAssets.computeStudio {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .scaleEffect(1.01)
                    .saturation(colorScheme == .dark ? 0.70 : 0.82)
                    .contrast(colorScheme == .dark ? 0.88 : 0.90)
                    .brightness(colorScheme == .dark ? -0.17 : 0.02)
                    .blur(radius: 3)
                    .opacity(colorScheme == .dark ? 0.72 : 0.84)
                    .ignoresSafeArea()
            }

            DesignTokens.ambientSmoke
                .blendMode(.color)
                .opacity(colorScheme == .dark ? 0.28 : 0.10)
                .ignoresSafeArea()

            DesignTokens.ambientSmoke
                .opacity(colorScheme == .dark ? 0.16 : 0.05)
                .ignoresSafeArea()
        }
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
            .background(.thinMaterial, in: Circle())
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.30 : 0.42) : 0.18),
                in: Circle()
            )
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
            .background(.thinMaterial, in: Capsule())
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.24 : 0.36) : 0.16),
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
            .background(.thinMaterial, in: Circle())
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.24 : 0.36) : 0.16),
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
        if #available(macOS 26.0, *) {
            glassEffect(.regular, in: shape)
        } else {
            background(.regularMaterial, in: shape)
        }
    }

    func spatialContentSurface() -> some View {
        background(DesignTokens.glassSmoke.opacity(0.28))
    }
}
