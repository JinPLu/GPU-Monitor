import AppKit
import SwiftUI

enum DesignTokens {
    static let ink = Color(red: 0.137, green: 0.224, blue: 0.263)
    static let mutedInk = Color(red: 0.282, green: 0.369, blue: 0.400)
    static let fogBlue = Color(red: 0.659, green: 0.753, blue: 0.804)
    static let harborSlate = Color(red: 0.231, green: 0.337, blue: 0.384)
    static let seaGlass = Color(red: 0.867, green: 0.941, blue: 0.937)
    static let cyan = Color(red: 0.0, green: 0.722, blue: 0.851)
    static let green = Color(red: 0.169, green: 0.647, blue: 0.424)
    static let amber = Color(red: 0.843, green: 0.580, blue: 0.133)
    static let coral = Color(red: 0.843, green: 0.392, blue: 0.357)
    static let card = Color.white.opacity(0.58)
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var drifting = false

    var body: some View {
        ZStack {
            if let image = DesktopAssets.computeStudio {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .scaleEffect(drifting && !reduceMotion ? 1.055 : 1.025)
                    .offset(
                        x: drifting && !reduceMotion ? 14 : -10,
                        y: drifting && !reduceMotion ? -8 : 8
                    )
                    .ignoresSafeArea()
                    .overlay(Color.black.opacity(0.08))
            } else {
                DesignTokens.fogBlue.opacity(0.88)
            }
            Circle()
                .fill(DesignTokens.seaGlass.opacity(0.26))
                .frame(width: 520, height: 520)
                .blur(radius: 104)
                .offset(x: -420, y: -250)
            Circle()
                .fill(DesignTokens.amber.opacity(0.20))
                .frame(width: 440, height: 440)
                .blur(radius: 118)
                .offset(x: 440, y: 360)
            Circle()
                .fill(DesignTokens.cyan.opacity(0.16))
                .frame(width: 380, height: 380)
                .blur(radius: 102)
                .offset(x: 470, y: -270)
        }
        .ignoresSafeArea()
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 26).repeatForever(autoreverses: true)) {
                drifting = true
            }
        }
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
            .foregroundStyle(DesignTokens.harborSlate)
            .background(Color.white.opacity(configuration.isPressed ? 0.72 : 0.58), in: Circle())
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
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
        view.appearance = NSAppearance(named: .aqua)
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
}
