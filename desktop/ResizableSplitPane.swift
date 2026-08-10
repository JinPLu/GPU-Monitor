import AppKit
import SwiftUI

struct MasterDetailSplitConfiguration: Equatable {
    let persistenceKey: String
    let defaultMasterWidth: CGFloat
    let minimumMasterWidth: CGFloat
    let minimumDetailWidth: CGFloat
    let compactBackLabel: String

    static let resources = MasterDetailSplitConfiguration(
        persistenceKey: "serverpilot.resources.masterWidth.v1",
        defaultMasterWidth: 430,
        // A narrower list turns endpoint IDs and four telemetry columns into
        // unreadable fragments.  At this point the row switches to its
        // multi-line form instead of allowing the divider to compress it
        // further.
        minimumMasterWidth: 400,
        minimumDetailWidth: 560,
        compactBackLabel: "返回服务器列表"
    )

    static let ownership = MasterDetailSplitConfiguration(
        persistenceKey: "serverpilot.resourceUsage.masterWidth.v1",
        defaultMasterWidth: 292,
        minimumMasterWidth: 260,
        minimumDetailWidth: 520,
        compactBackLabel: "返回项目与 Agent 列表"
    )
}

struct PersistedMasterDetailSplit<Master: View, Detail: View>: View {

    private let configuration: MasterDetailSplitConfiguration
    @Binding private var showsCompactDetail: Bool
    private let master: Master
    private let detail: Detail
    @State private var masterWidth: CGFloat
    @State private var dragOriginWidth: CGFloat?
    @State private var isHoveringSplitHandle = false

    init(
        configuration: MasterDetailSplitConfiguration,
        showsCompactDetail: Binding<Bool>,
        @ViewBuilder master: () -> Master,
        @ViewBuilder detail: () -> Detail
    ) {
        self.configuration = configuration
        _showsCompactDetail = showsCompactDetail
        let storedWidth = UserDefaults.standard.double(forKey: configuration.persistenceKey)
        _masterWidth = State(initialValue: storedWidth > 0 ? storedWidth : configuration.defaultMasterWidth)
        self.master = master()
        self.detail = detail()
    }

    var body: some View {
        GeometryReader { proxy in
            let isCompact = proxy.size.width < configuration.minimumMasterWidth + configuration.minimumDetailWidth
            let maximumMasterWidth = max(configuration.minimumMasterWidth, proxy.size.width - configuration.minimumDetailWidth)
            let resolvedMasterWidth = min(max(masterWidth, configuration.minimumMasterWidth), maximumMasterWidth)

            Group {
                if isCompact {
                    if showsCompactDetail {
                        VStack(spacing: 0) {
                            HStack {
                                Button(action: { showsCompactDetail = false }) {
                                    Label(configuration.compactBackLabel, systemImage: "chevron.backward")
                                        .font(.system(size: 12, weight: .semibold))
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel(configuration.compactBackLabel)
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 16)
                            .frame(height: 42)
                            .background(DesignTokens.surface)

                            Divider().opacity(0.45)
                            detail
                        }
                    } else {
                        master
                    }
                } else {
                    HStack(spacing: 0) {
                        master
                            .frame(width: resolvedMasterWidth)

                        splitHandle(maximumMasterWidth: maximumMasterWidth)

                        detail
                            .frame(minWidth: configuration.minimumDetailWidth, maxWidth: .infinity)
                    }
                }
            }
            .onChange(of: isCompact) { _, compact in
                if compact {
                    showsCompactDetail = false
                }
            }
            .onChange(of: masterWidth) { _, width in
                UserDefaults.standard.set(width, forKey: configuration.persistenceKey)
            }
        }
    }

    private func splitHandle(maximumMasterWidth: CGFloat) -> some View {
        Rectangle()
            .fill(DesignTokens.surfaceStroke.opacity(0.55))
            .frame(width: 1)
            .frame(width: 10)
            .contentShape(Rectangle())
            .overlay {
                Image(systemName: "line.3.horizontal")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(DesignTokens.mutedInk.opacity(0.55))
                    .rotationEffect(.degrees(90))
                    .accessibilityHidden(true)
            }
            .gesture(
                DragGesture(minimumDistance: 1)
                    .onChanged { value in
                        let origin = dragOriginWidth ?? masterWidth
                        if dragOriginWidth == nil {
                            dragOriginWidth = origin
                        }
                        masterWidth = min(
                            max(origin + value.translation.width, configuration.minimumMasterWidth),
                            maximumMasterWidth
                        )
                    }
                    .onEnded { _ in
                        dragOriginWidth = nil
                    }
            )
            .onHover { hovering in
                guard isHoveringSplitHandle != hovering else { return }
                isHoveringSplitHandle = hovering
                if hovering {
                    NSCursor.resizeLeftRight.push()
                } else {
                    NSCursor.pop()
                }
            }
            .onDisappear {
                guard isHoveringSplitHandle else { return }
                NSCursor.pop()
                isHoveringSplitHandle = false
            }
            .help("拖动调整服务器列表与详情宽度")
            .accessibilityLabel("调整列表与详情宽度")
            .accessibilityHint("拖动可调整并保存列表宽度")
    }
}
