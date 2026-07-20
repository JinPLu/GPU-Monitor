import AppKit
import SwiftUI

enum DesignTokens {
    static let ink = Color(nsColor: .labelColor)
    static let mutedInk = Color(nsColor: .secondaryLabelColor)
    static let interaction = Color(nsColor: NSColor(name: nil) { appearance in
        let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return isDark
            ? NSColor(srgbRed: 0.78, green: 0.58, blue: 0.56, alpha: 1)
            : NSColor(srgbRed: 0.43, green: 0.31, blue: 0.31, alpha: 1)
    })
    static let success = Color(red: 0.20, green: 0.56, blue: 0.38)
    static let warning = Color(red: 0.80, green: 0.49, blue: 0.14)
    static let danger = Color(red: 0.75, green: 0.28, blue: 0.30)
    static let selection = Color(nsColor: .unemphasizedSelectedContentBackgroundColor)
    static let surface = Color(nsColor: .controlBackgroundColor).opacity(0.72)
    static let surfaceStroke = Color.white.opacity(0.22)
    static let ambientSmoke = Color(red: 0.30, green: 0.40, blue: 0.44)
    static let glassSmoke = Color(red: 0.67, green: 0.72, blue: 0.74)
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
                    .saturation(colorScheme == .dark ? 0.34 : 0.44)
                    .contrast(colorScheme == .dark ? 0.88 : 0.92)
                    .brightness(colorScheme == .dark ? -0.18 : -0.07)
                    .blur(radius: 1.8)
                    .opacity(colorScheme == .dark ? 0.72 : 0.86)
                    .ignoresSafeArea()
            }

            DesignTokens.ambientSmoke
                .blendMode(.color)
                .opacity(colorScheme == .dark ? 0.42 : 0.34)
                .ignoresSafeArea()

            DesignTokens.ambientSmoke
                .opacity(colorScheme == .dark ? 0.16 : 0.08)
                .ignoresSafeArea()
        }
        .ignoresSafeArea()
    }
}

struct SoftButtonStyle: ButtonStyle {
    let tint: Color
    let foreground: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(foreground)
            .padding(.horizontal, 13)
            .frame(height: 31)
            .background(tint.opacity(configuration.isPressed ? 0.72 : 0.94), in: Capsule())
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct SoftIconButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(DesignTokens.ink)
            .background(DesignTokens.surface.opacity(configuration.isPressed ? 0.82 : 0.96), in: Circle())
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
    }
}

struct PrimaryActionButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 15)
            .frame(height: 34)
            .background(
                DesignTokens.interaction.opacity(configuration.isPressed ? 0.78 : 1),
                in: Capsule()
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct SecondaryActionButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(DesignTokens.ink)
            .padding(.horizontal, 14)
            .frame(height: 34)
            .background(
                DesignTokens.surface.opacity(configuration.isPressed ? 0.74 : 0.94),
                in: Capsule()
            )
            .overlay(Capsule().stroke(DesignTokens.surfaceStroke, lineWidth: 1))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct IconActionButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(DesignTokens.ink)
            .frame(width: 34, height: 34)
            .background(
                DesignTokens.surface.opacity(configuration.isPressed ? 0.74 : 0.94),
                in: Circle()
            )
            .overlay(Circle().stroke(DesignTokens.surfaceStroke, lineWidth: 1))
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
        background(DesignTokens.glassSmoke.opacity(0.20))
    }
}
