import AppKit
import Foundation
import SwiftUI

private enum DesktopError: LocalizedError {
    case projectRootMissing
    case brokerExecutableMissing
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .projectRootMissing:
            return "应用内置运行资源不完整。请重新安装 GPU Broker.app，或为开发构建设置 GPU_BROKER_ROOT。"
        case .brokerExecutableMissing:
            return "应用内置后台服务不完整。请重新安装 GPU Broker.app，或为开发构建设置 GPU_BROKER_CLI。"
        case .commandFailed(let details):
            return details
        }
    }
}

// MARK: - Native desktop shell

@MainActor
final class DesktopAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let port = 8787
    private let brokerStore = BrokerStore()
    private var window: NSWindow?
    private var isStarting = false

    private lazy var projectRoot: URL? = {
        if let configured = ProcessInfo.processInfo.environment["GPU_BROKER_ROOT"], !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        if let bundledRoot = Bundle.main.resourceURL?
            .appendingPathComponent("BrokerRuntime", isDirectory: true),
           FileManager.default.fileExists(
               atPath: bundledRoot.appendingPathComponent("configs/inventory.yaml").path
           ) {
            return bundledRoot
        }
        let bundleParent = Bundle.main.bundleURL.deletingLastPathComponent()
        return findProjectRoot(startingAt: bundleParent)
    }()

    private var baseURL: URL {
        URL(string: "http://127.0.0.1:\(port)/")!
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        configureMainMenu()

        let visibleSize = NSScreen.main?.visibleFrame.size ?? NSSize(width: 1440, height: 820)
        let initialSize = NSSize(
            width: max(1024, min(1440, visibleSize.width - 48)),
            height: max(640, min(820, visibleSize.height - 48))
        )
        let contentRect = NSRect(origin: .zero, size: initialSize)
        let createdWindow = NSWindow(
            contentRect: contentRect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        createdWindow.title = "GPU Broker"
        createdWindow.titleVisibility = .hidden
        createdWindow.titlebarAppearsTransparent = true
        createdWindow.toolbarStyle = .unifiedCompact
        createdWindow.titlebarSeparatorStyle = .none
        createdWindow.backgroundColor = .clear
        createdWindow.isOpaque = false
        createdWindow.minSize = NSSize(width: 1024, height: 640)
        createdWindow.center()
        createdWindow.delegate = self

        let view = NSHostingView(rootView: NativeBrokerRoot(store: brokerStore))
        view.frame = contentRect
        view.autoresizingMask = [.width, .height]
        createdWindow.contentView = view
        window = createdWindow
        createdWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
#if DEBUG
        if configureFixtureModeIfRequested() {
            return
        }
#endif
        ensureDaemon()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func configureMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 GPU Broker", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    private func findProjectRoot(startingAt url: URL) -> URL? {
        var candidate = url.standardizedFileURL
        let fileManager = FileManager.default
        while candidate.path != "/" {
            let projectFile = candidate.appendingPathComponent("pyproject.toml")
            let inventory = candidate.appendingPathComponent("configs/inventory.yaml")
            if fileManager.fileExists(atPath: projectFile.path) && fileManager.fileExists(atPath: inventory.path) {
                return candidate
            }
            candidate.deleteLastPathComponent()
        }
        return nil
    }

    private func brokerExecutable() -> URL? {
        let environment = ProcessInfo.processInfo.environment
        let home = environment["HOME"] ?? NSHomeDirectory()
        let candidates = [
            environment["GPU_BROKER_CLI"],
            Bundle.main.resourceURL?
                .appendingPathComponent("BrokerRuntime/gpu-broker")
                .path,
            "\(home)/.local/share/uv/tools/gpu-broker/bin/gpu-broker",
            "/opt/homebrew/bin/gpu-broker",
            "/usr/local/bin/gpu-broker"
        ].compactMap { $0 }
        return candidates
            .map { URL(fileURLWithPath: $0) }
            .first(where: { FileManager.default.isExecutableFile(atPath: $0.path) })
    }

    private func processEnvironment(broker: URL) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if let root = projectRoot {
            environment["GPU_BROKER_PROJECT_ROOT"] = root.path
        }
        environment["GPU_BROKER_DAEMON_EXECUTABLE"] = broker.path
        return environment
    }

    private func connectOrStartServer(attempt: Int = 0) {
        healthCheck { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .compatible(let info):
                    self.brokerStore.connect(to: self.baseURL, serviceInfo: info)
                    return
                case .incompatible(let reason):
                    self.showFatalError(reason)
                    return
                case .unavailable:
                    break
                }
                if !self.isStarting {
                    self.ensureDaemon()
                    return
                }
                if attempt < 80 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                        self.connectOrStartServer(attempt: attempt + 1)
                    }
                } else {
                    self.showFatalError("本机 GPU Broker 服务未能在规定时间内启动。请检查项目依赖和 state 目录。")
                }
            }
        }
    }

    private func healthCheck(completion: @escaping (ServiceProbeResult) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("health/live"))
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [port] data, response, error in
            guard error == nil, let response = response as? HTTPURLResponse else {
                completion(.unavailable)
                return
            }
            guard response.statusCode == 200 else {
                completion(.unavailable)
                return
            }
            guard
                let data,
                let object = try? JSONSerialization.jsonObject(with: data),
                let payload = object as? [String: Any],
                let info = ServiceInfo(health: payload)
            else {
                completion(.incompatible("127.0.0.1:\(port) 上有服务响应，但它不是当前 GPU Broker 服务。桌面应用不会关闭或替换这个外部服务。"))
                return
            }
            guard info.schemaVersion == "v1", info.capabilities.contains("instant_claims") else {
                completion(.incompatible("127.0.0.1:\(port) 上的 GPU Broker 版本不兼容。请先退出旧服务，再重新打开桌面应用。"))
                return
            }
            completion(.compatible(info))
        }.resume()
    }

    private func ensureDaemon() {
        guard let root = projectRoot else {
            showFatalError(DesktopError.projectRootMissing.localizedDescription)
            return
        }
        guard let broker = brokerExecutable() else {
            showFatalError(DesktopError.brokerExecutableMissing.localizedDescription)
            return
        }
        isStarting = true
        let environment = processEnvironment(broker: broker)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try Self.runCommand(
                    executable: broker,
                    arguments: [
                        "daemon", "ensure", "--source-root", root.path
                    ],
                    root: root,
                    environment: environment
                )
                DispatchQueue.main.async {
                    self.isStarting = false
                    self.connectOrStartServer()
                }
            } catch {
                DispatchQueue.main.async {
                    self.isStarting = false
                    self.showFatalError(error.localizedDescription)
                }
            }
        }
    }

#if DEBUG
    private func configureFixtureModeIfRequested() -> Bool {
        let environment = ProcessInfo.processInfo.environment
        guard let fixture = environment["GPU_BROKER_DESKTOP_FIXTURE"], !fixture.isEmpty else {
            return false
        }
        do {
            let fixturesRoot = desktopFixturesRoot()
            let fixtureURL = try FixtureSnapshots.resolve(
                fixture,
                fixturesRoot: fixturesRoot,
                projectRoot: projectRoot
            )
            brokerStore.useFixture(snapshot: try FixtureSnapshots.load(from: fixtureURL))
            return true
        } catch {
            showFatalError(error.localizedDescription)
            return true
        }
    }

    private func desktopFixturesRoot() -> URL {
        if let resourceURL = Bundle.main.resourceURL?.appendingPathComponent("Fixtures", isDirectory: true),
           FileManager.default.fileExists(atPath: resourceURL.path) {
            return resourceURL
        }
        if let root = projectRoot {
            return root
                .appendingPathComponent("desktop", isDirectory: true)
                .appendingPathComponent("Fixtures", isDirectory: true)
        }
        return Bundle.main.bundleURL
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures", isDirectory: true)
    }
#endif

    nonisolated private static func runCommand(
        executable: URL,
        arguments: [String],
        root: URL,
        environment: [String: String]
    ) throws -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = root
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let details = String(data: data, encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            throw DesktopError.commandFailed("启动本机后台服务失败：\(details)")
        }
        return details
    }

    private func showFatalError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "无法启动 GPU Broker"
        alert.informativeText = message
        alert.addButton(withTitle: "退出")
        alert.runModal()
        NSApp.terminate(nil)
    }
}

// MARK: - Broker API model

private enum ServiceProbeResult {
    case compatible(ServiceInfo)
    case incompatible(String)
    case unavailable
}

@discardableResult
private func confirmEndpointRemoval(_ endpoint: EndpointRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "移除这台服务器？"
    alert.informativeText = "这会删除本机保存的服务器登记、GPU 状态和采集历史，但不会关闭远端机器或停止任务。存在租约、预约、排队请求、维护记录或其他受保护历史时，本机服务会拒绝移除。"
    alert.addButton(withTitle: "移除")
    alert.addButton(withTitle: "取消")
    guard alert.runModal() == .alertFirstButtonReturn else { return false }
    return true
}

@discardableResult
private func confirmLeaseRelease(_ lease: LeaseRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "归还这 \(lease.gpuIDs.count) 块 GPU？"
    alert.informativeText = "归还后，这些 GPU 可以再次分配。正在运行的远端任务不会停止，请先确认任务已经结束。"
    alert.addButton(withTitle: "归还")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

// MARK: - Apple Home inspired native interface

private struct NativeBrokerRoot: View {
    @ObservedObject var store: BrokerStore
    @State private var showAddServer = false
    @State private var showClaim = false
    @State private var selectedEndpointID = ""
    @State private var selectedGPU: GPURecord?
    @State private var selectedEndpoint: EndpointRecord?
    @State private var selectedDashboardSection: DashboardSection = .overview

    var body: some View {
        GeometryReader { proxy in
            let compactNavigation = proxy.size.width < 1180
            let sidebarWidth: CGFloat = compactNavigation ? 72 : 224

            ZStack {
                AmbientBackground()

                HStack(spacing: 0) {
                    AppSidebar(
                        store: store,
                        selectedSection: selectedDashboardSection,
                        compact: compactNavigation,
                        navigate: { selectedDashboardSection = $0 }
                    )
                    .frame(width: sidebarWidth)

                    Divider().opacity(0.34)

                    VStack(spacing: 0) {
                        AppToolbar(
                            store: store,
                            selectedSection: selectedDashboardSection,
                            addServer: { showAddServer = true },
                            claimGPU: {
                                selectedEndpointID = ""
                                showClaim = true
                            },
                            refresh: store.reload
                        )
                        DashboardView(
                            store: store,
                            addServer: { showAddServer = true },
                            claimGPU: {
                                selectedEndpointID = ""
                                showClaim = true
                            },
                            claimEndpoint: { endpointID in
                                selectedEndpointID = endpointID
                                showClaim = true
                            },
                            openEndpoint: { endpoint in
                                selectedEndpoint = endpoint
                            },
                            removeEndpoint: { endpoint in
                                guard store.allowsMutations else { return }
                                guard confirmEndpointRemoval(endpoint) else { return }
                                store.deleteEndpoint(endpoint) { _, _ in }
                            },
                            selectedSection: $selectedDashboardSection,
                            selectGPU: { gpu in
                                selectedGPU = gpu
                            }
                        )
                    }
                    .frame(
                        width: max(0, proxy.size.width - sidebarWidth - 1),
                        height: proxy.size.height
                    )
                    .clipped()
                    .background(Color.clear)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(DesignTokens.glassSmoke.opacity(0.16))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .tint(DesignTokens.interaction)
        .sheet(isPresented: $showAddServer) {
            AddServerSheet(store: store)
        }
        .sheet(isPresented: $showClaim) {
            ClaimSheet(store: store, initialEndpointID: selectedEndpointID)
        }
        .sheet(item: $selectedEndpoint) { endpoint in
            ServerDetailSheet(
                store: store,
                endpoint: endpoint,
                gpus: store.snapshot.gpus(for: endpoint)
            )
        }
        .sheet(item: $selectedGPU) { gpu in
            GPUDetailSheet(gpu: gpu)
        }
    }
}

private struct AppSidebar: View {
    @ObservedObject var store: BrokerStore
    let selectedSection: DashboardSection
    let compact: Bool
    let navigate: (DashboardSection) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .fill(DesignTokens.interaction)
                    Image(systemName: "square.3.layers.3d.top.filled")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(DesignTokens.onInteraction)
                }
                .frame(width: 36, height: 36)
                if !compact {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("GPU Broker")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(DesignTokens.ink)
                        Text("本机资源控制面")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: compact ? .center : .leading)
            .padding(.horizontal, compact ? 10 : 18)
            .padding(.top, 30)
            .padding(.bottom, 25)

            if !compact {
                Text("空间")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .padding(.horizontal, 18)
                    .padding(.bottom, 8)
            }

            SidebarSelection(title: "资源总览", systemImage: "square.grid.2x2.fill", selected: selectedSection == .overview, compact: compact) {
                navigate(.overview)
            }
            SidebarSelection(title: "服务器池", systemImage: "server.rack", selected: selectedSection == .serverPool, compact: compact) {
                navigate(.serverPool)
            }
            SidebarSelection(title: "租约", systemImage: "key.fill", selected: selectedSection == .leases, compact: compact) {
                navigate(.leases)
            }
            SidebarSelection(title: "设置", systemImage: "gearshape.fill", selected: selectedSection == .settings, compact: compact) {
                navigate(.settings)
            }

            Spacer(minLength: 22)

            VStack(alignment: compact ? .center : .leading, spacing: 6) {
                HStack(spacing: 7) {
                    Circle()
                        .fill(store.isConnected ? DesignTokens.success : DesignTokens.warning)
                        .frame(width: 7, height: 7)
                    if !compact {
                        Text(store.isConnected ? "本机服务已连接" : "正在连接本机服务")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(DesignTokens.ink)
                    }
                }
                if !compact {
                    Text("状态仅供查看；操作由协调客户端提交")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
            }
            .padding(.horizontal, compact ? 10 : 18)
            .padding(.vertical, 16)
            .overlay(alignment: .top) {
                Divider().padding(.horizontal, 18)
            }
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(DesignTokens.glassSmoke.opacity(0.18))
        .background(.regularMaterial)
    }
}

private struct SidebarSelection: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let title: String
    let systemImage: String
    let selected: Bool
    let compact: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 11) {
                Image(systemName: systemImage)
                    .font(.system(size: 14, weight: .semibold))
                    .frame(width: 18)
                if !compact {
                    Text(title)
                        .font(.system(size: 13, weight: selected ? .semibold : .medium))
                    Spacer()
                }
            }
            .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.ink)
            .padding(.horizontal, compact ? 0 : 15)
            .frame(height: 38)
            .frame(maxWidth: .infinity)
            .background(
                selected ? DesignTokens.interaction.opacity(0.16) : DesignTokens.ink.opacity(hovering ? 0.045 : 0),
                in: RoundedRectangle(cornerRadius: 9, style: .continuous)
            )
        }
        .buttonStyle(.plain)
        .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.ink)
        .padding(.horizontal, compact ? 12 : 10)
        .help(title)
        .accessibilityLabel(title)
        .accessibilityValue(selected ? "当前页面" : "未选中")
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
    }
}

private struct AppToolbar: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var store: BrokerStore
    let selectedSection: DashboardSection
    let addServer: () -> Void
    let claimGPU: () -> Void
    let refresh: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(statusText)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 14)
            VStack(alignment: .trailing, spacing: 6) {
                HStack(spacing: 8) {
                    FreshnessBadge(
                        icon: store.isConnected ? "checkmark.circle.fill" : "wifi.exclamationmark",
                        text: store.isConnected ? "已连接" : "未连接",
                        value: lastSuccessText,
                        color: store.isConnected ? DesignTokens.success : DesignTokens.warning
                    )
                    FreshnessBadge(
                        icon: "clock.arrow.circlepath",
                        text: apiFreshnessText,
                        value: revisionText,
                        color: freshnessColor
                    )
                    Button(action: addServer) {
                        Label("添加服务器", systemImage: "plus")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .buttonStyle(SecondaryActionButtonStyle())
                    .disabled(!store.allowsMutations)
                    .help(store.allowsMutations ? "添加服务器到本机资源池" : store.mutationUnavailableReason)
                    .accessibilityLabel("添加服务器")
                    Button(action: claimGPU) {
                        Label("认领 GPU", systemImage: "key.fill")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .buttonStyle(PrimaryActionButtonStyle())
                    .disabled(!store.allowsMutations || store.snapshot.endpoints.isEmpty)
                    .help(!store.allowsMutations ? store.mutationUnavailableReason : (store.snapshot.endpoints.isEmpty ? "请先添加服务器" : "认领可用 GPU"))
                    .accessibilityLabel("认领 GPU")
                    Button(action: refresh) {
                        Label(store.isRefreshing ? "刷新中" : "刷新", systemImage: "arrow.clockwise")
                            .font(.system(size: 12, weight: .semibold))
                            .rotationEffect(.degrees(store.isRefreshing && !reduceMotion ? 360 : 0))
                            .animation(
                                store.isRefreshing && !reduceMotion ? .linear(duration: 0.8).repeatForever(autoreverses: false) : .easeOut(duration: 0.15),
                                value: store.isRefreshing
                            )
                    }
                    .buttonStyle(SecondaryActionButtonStyle())
                    .disabled(store.isRefreshing || !store.canRefresh)
                    .keyboardShortcut("r", modifiers: [.command])
                    .help(store.canRefresh ? "刷新本机资源快照" : "测试夹具使用固定快照，不能刷新")
                    .accessibilityLabel("刷新资源快照")
                    .accessibilityValue(store.isRefreshing ? "正在刷新" : (store.canRefresh ? "可刷新" : "固定测试快照"))
                }
                if let error = store.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.danger)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .accessibilityLabel("当前刷新错误")
                        .accessibilityValue(error)
                }
            }
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 14)
        .background(.regularMaterial)
    }

    private var statusText: String {
        if store.errorMessage != nil, store.lastGoodSnapshot != nil {
            return "显示上次成功快照，等待恢复连接"
        }
        if store.freshness == .stale {
            return "快照已过期，资源操作暂时停用"
        }
        if let snapshotRevision = store.snapshot.snapshotRevision {
            return "快照修订 \(snapshotRevision) · \(serverTimeText)"
        }
        return store.isConnected ? "等待第一份快照" : "正在连接本机服务"
    }

    private var lastSuccessText: String {
        guard let lastUpdated = store.lastUpdated else { return "尚无成功刷新" }
        let elapsed = max(0, Int(Date().timeIntervalSince(lastUpdated)))
        return elapsed < 5 ? "刚刚成功" : "\(elapsed) 秒前成功"
    }

    private var apiFreshnessText: String {
        if store.freshness == .stale { return "数据已过期" }
        if store.freshness == .failed { return "刷新失败" }
        if let age = store.snapshot.dataAgeSeconds {
            return age < 5 ? "数据新鲜" : "数据 \(Int(age.rounded())) 秒"
        }
        return "等待数据"
    }

    private var revisionText: String {
        let revision = store.snapshot.snapshotRevision.map { "#\($0)" } ?? "无修订"
        if let freshness = store.snapshot.freshnessSeconds {
            return "\(revision) · 阈值 \(Int(freshness.rounded())) 秒"
        }
        return revision
    }

    private var freshnessColor: Color {
        switch store.freshness {
        case .fresh: return DesignTokens.success
        case .waiting: return DesignTokens.warning
        case .stale, .failed: return DesignTokens.danger
        }
    }

    private var serverTimeText: String {
        formattedTimestamp(store.snapshot.serverTime)
    }

    private var title: String {
        switch selectedSection {
        case .overview: return "资源总览"
        case .serverPool: return "服务器池"
        case .leases: return "租约与队列"
        case .settings: return "设置"
        }
    }
}

private struct FreshnessBadge: View {
    let icon: String
    let text: String
    let value: String
    let color: Color

    var body: some View {
        Label {
            VStack(alignment: .leading, spacing: 1) {
                Text(text)
                    .font(.system(size: 10, weight: .semibold))
                Text(value)
                    .font(.system(size: 8, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
        } icon: {
            Image(systemName: icon)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(color)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.regularMaterial, in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text)
        .accessibilityValue(value)
    }
}

private struct DashboardView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var store: BrokerStore
    let addServer: () -> Void
    let claimGPU: () -> Void
    let claimEndpoint: (String) -> Void
    let openEndpoint: (EndpointRecord) -> Void
    let removeEndpoint: (EndpointRecord) -> Void
    @Binding var selectedSection: DashboardSection
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        VStack(spacing: 0) {
            if let error = store.errorMessage {
                NoticeBanner(message: error, color: DesignTokens.danger, icon: "exclamationmark.triangle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if store.freshness == .stale {
                NoticeBanner(message: "快照已超过新鲜度阈值；当前仅显示最后可用数据，资源操作已停用。", color: DesignTokens.danger, icon: "clock.badge.exclamationmark.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if let notice = store.notice {
                NoticeBanner(message: notice, color: DesignTokens.success, icon: "checkmark.circle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            }

            Group {
                switch selectedSection {
                case .overview:
                    FleetOverview(
                        snapshot: store.snapshot,
                        openEndpoint: openEndpoint,
                        selectGPU: selectGPU,
                        openServerPool: { selectedSection = .serverPool }
                    )
                case .serverPool:
                    SpatialServerPool(
                        store: store,
                        claimEndpoint: claimEndpoint,
                        removeEndpoint: removeEndpoint,
                        selectGPU: selectGPU
                    )
                case .leases:
                    SpatialLeaseDesk(store: store)
                case .settings:
                    SettingsDashboard(store: store)
                }
            }
            .id(selectedSection)
            .transition(.opacity.combined(with: .offset(y: reduceMotion ? 0 : 6)))
            .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: selectedSection)
        }
        .background(Color.clear)
    }
}

private struct HomeSectionTitle: View {
    let title: String
    let subtitle: String?

    init(title: String, subtitle: String? = nil) {
        self.title = title
        self.subtitle = subtitle
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(title)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(DesignTokens.ink)
            if let subtitle {
                Text(subtitle)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .padding(.leading, 4)
            }
            Spacer()
        }
    }
}

private struct SettingsDashboard: View {
    @ObservedObject var store: BrokerStore
    @State private var actorID = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HomeSectionTitle(title: "设置", subtitle: "本机桌面行为与协调边界")

                VStack(alignment: .leading, spacing: 12) {
                    Text("操作者")
                        .font(.system(size: 13, weight: .semibold))
                    HStack(alignment: .bottom, spacing: 12) {
                        VStack(alignment: .leading, spacing: 7) {
                            Text("操作者标识")
                                .fieldLabel()
                            TextField("human", text: $actorID)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 13, weight: .medium, design: .monospaced))
                                .accessibilityLabel("操作者标识")
                        }
                        Button("保存") { store.setActor(actorID) }
                            .buttonStyle(SecondaryActionButtonStyle())
                            .keyboardShortcut(.defaultAction)
                            .accessibilityLabel("保存操作者标识")
                    }
                    Text("该标识只写入本机协调操作记录，不代表身份认证或远端权限。")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                .padding(16)
                .background(DesignTokens.surface.opacity(0.72), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))

                VStack(alignment: .leading, spacing: 12) {
                    Text("服务")
                        .font(.system(size: 13, weight: .semibold))
                    SettingsFact(label: "连接", value: store.isConnected ? "已连接" : "未连接", icon: store.isConnected ? "checkmark.circle.fill" : "wifi.exclamationmark")
                    SettingsFact(label: "Schema", value: store.serviceInfo?.schemaVersion ?? "未知", icon: "curlybraces")
                    SettingsFact(label: "版本", value: store.serviceInfo?.version ?? "未知", icon: "number")
                    SettingsFact(label: "端点移除", value: store.supportsEndpointDeletion ? "支持" : "当前服务不支持", icon: "trash")
                }
                .padding(16)
                .background(DesignTokens.surface.opacity(0.72), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))

                Label("GPU Broker 只协调 GPU 归属，不授权启动或停止远端任务。", systemImage: "hand.raised.fill")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 18)
            .frame(maxWidth: 760, alignment: .leading)
        }
        .onAppear { actorID = store.actorID }
        .accessibilityLabel("设置")
    }
}

private struct SettingsFact: View {
    let label: String
    let value: String
    let icon: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DesignTokens.interaction)
                .frame(width: 28, height: 28)
                .background(DesignTokens.interaction.opacity(0.11), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            Text(label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
            Spacer()
            Text(value)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(DesignTokens.ink)
        }
        .accessibilityElement(children: .combine)
    }
}

private struct NoticeBanner: View {
    let message: String
    let color: Color
    let icon: String

    var body: some View {
        Label(message, systemImage: icon)
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(DesignTokens.ink)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(color.opacity(0.16), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(color.opacity(0.35), lineWidth: 1)
            )
    }
}

private struct SpatialServerPool: View {
    @ObservedObject var store: BrokerStore
    @State private var selectedEndpointID = ""
    let claimEndpoint: (String) -> Void
    let removeEndpoint: (EndpointRecord) -> Void
    let selectGPU: (GPURecord) -> Void

    private var endpoints: [EndpointRecord] { store.snapshot.endpoints }

    private var selectedEndpoint: EndpointRecord? {
        endpoints.first { $0.id == selectedEndpointID } ?? endpoints.first
    }

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("服务器")
                            .font(.system(size: 18, weight: .semibold))
                        Text("\(store.snapshot.summary.onlineServers) / \(store.snapshot.summary.totalServers) 在线")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                    Spacer()
                    Text("\(store.snapshot.summary.totalGPUs) GPU")
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                .padding(20)

                Divider()

                if endpoints.isEmpty {
                    ContentUnavailableView("暂无服务器", systemImage: "server.rack", description: Text("从命令岛添加一台服务器"))
                        .frame(maxHeight: .infinity)
                } else {
                    ScrollView {
                        LazyVStack(spacing: 6) {
                            ForEach(endpoints) { endpoint in
                                SpatialServerRow(
                                    endpoint: endpoint,
                                    gpus: store.snapshot.gpus(for: endpoint),
                                    selected: endpoint.id == selectedEndpoint?.id
                                ) {
                                    withAnimation(.easeOut(duration: 0.14)) {
                                        selectedEndpointID = endpoint.id
                                    }
                                }
                            }
                        }
                        .padding(10)
                    }
                }

                if store.serviceInfo != nil, !store.supportsEndpointDeletion {
                    Label("更新本机服务后可移除服务器", systemImage: "exclamationmark.triangle.fill")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.warning)
                        .padding(14)
                }
            }
            .frame(width: 304)
            .background(DesignTokens.glassSmoke.opacity(0.62))

            Divider().opacity(0.36)

            if let selectedEndpoint {
                SpatialServerDetail(
                    store: store,
                    endpoint: selectedEndpoint,
                    gpus: store.snapshot.gpus(for: selectedEndpoint),
                    claim: { claimEndpoint(selectedEndpoint.id) },
                    remove: { removeEndpoint(selectedEndpoint) },
                    selectGPU: selectGPU
                )
                .id(selectedEndpoint.id)
                .transition(.opacity.combined(with: .offset(x: 8)))
            } else {
                ContentUnavailableView("选择服务器", systemImage: "server.rack")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear { ensureSelection() }
        .onChange(of: endpoints.map(\.id)) { _, _ in ensureSelection() }
    }

    private func ensureSelection() {
        if !endpoints.contains(where: { $0.id == selectedEndpointID }) {
            selectedEndpointID = endpoints.first?.id ?? ""
        }
    }
}

private struct SpatialServerRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let selected: Bool
    let select: () -> Void

    private var availableCount: Int { gpus.filter { $0.state == "AVAILABLE" }.count }

    var body: some View {
        Button(action: select) {
            HStack(spacing: 11) {
                Image(systemName: endpoint.monitorStatus == "ONLINE" ? "server.rack" : "exclamationmark.triangle.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(statusColor)
                    .frame(width: 32, height: 32)
                    .background(statusColor.opacity(0.13), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text(endpoint.sshCommand)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(endpoint.monitorLabel)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Spacer(minLength: 0)
                Text(gpus.isEmpty ? "—" : "\(availableCount)/\(gpus.count)")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(.horizontal, 12)
            .frame(height: 54)
            .background(
                selected ? DesignTokens.interaction.opacity(0.14) : DesignTokens.ink.opacity(hovering ? 0.045 : 0),
                in: RoundedRectangle(cornerRadius: 8, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
    }

    private var statusColor: Color {
        switch endpoint.monitorStatus {
        case "ONLINE": return DesignTokens.success
        case "PENDING", "DRAINING": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }
}

private struct SpatialServerDetail: View {
    @ObservedObject var store: BrokerStore
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let claim: () -> Void
    let remove: () -> Void
    let selectGPU: (GPURecord) -> Void

    private var unavailable: Bool {
        ["ERROR", "STALE", "DISABLED", "DRAINING", "RETIRED"].contains(endpoint.monitorStatus)
    }

    private var availableCount: Int { gpus.filter { $0.state == "AVAILABLE" }.count }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 12) {
                    serverIdentity
                        .frame(maxWidth: .infinity, alignment: .leading)
                    serverAction
                }

                if unavailable {
                    DetailCallout(
                        icon: "exclamationmark.triangle.fill",
                        color: DesignTokens.danger,
                        message: endpoint.monitorDetail ?? "当前无法读取服务器状态"
                    )
                }

                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 12), count: 4), spacing: 12) {
                    SpatialMetric(label: "GPU 可用", value: gpus.isEmpty ? "—" : "\(availableCount) / \(gpus.count)", detail: gpus.isEmpty ? nil : "块", color: DesignTokens.success)
                    SpatialMetric(label: "GPU 显存", value: metricPercent(endpointAverageMemoryFraction(endpoint: endpoint, gpus: gpus)), detail: gpuMemoryDetail, color: DesignTokens.interaction)
                    SpatialMetric(label: "CPU 负载", value: metricPercent(endpoint.cpuLoadFraction), detail: cpuDetail, color: DesignTokens.warning)
                    SpatialMetric(label: "系统内存", value: metricPercent(endpoint.memoryFraction), detail: memoryDetail, color: DesignTokens.success)
                }

                Divider()

                VStack(alignment: .leading, spacing: 12) {
                    Text("GPU")
                        .font(.system(size: 16, weight: .semibold))
                    if gpus.isEmpty {
                        Text("没有可显示的 GPU 状态")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    } else {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 132, maximum: 160), spacing: 10)], spacing: 10) {
                            ForEach(gpus.sorted { $0.index < $1.index }) { gpu in
                                SpatialGPUCell(gpu: gpu) { selectGPU(gpu) }
                            }
                        }
                    }
                }

                if !gpus.isEmpty {
                    ServerLeaseSummary(gpus: gpus)
                }
            }
            .padding(26)
            .padding(.bottom, 70)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .spatialContentSurface()
    }

    private var serverIdentity: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 7) {
                StatusDot(status: endpoint.monitorStatus)
                Text(endpoint.monitorLabel)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Text(endpoint.sshCommand)
                .font(.system(size: 19, weight: .semibold, design: .monospaced))
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.middle)
                .help(endpoint.sshCommand)
                .textSelection(.enabled)
        }
    }

    @ViewBuilder
    private var serverAction: some View {
        if unavailable {
            Button(action: remove) {
                Label(store.deletingEndpointIDs.contains(endpoint.id) ? "移除中" : "移除", systemImage: "trash")
                    .font(.system(size: 11, weight: .semibold))
            }
            .buttonStyle(HomeClaimButtonStyle(tint: DesignTokens.danger, foreground: DesignTokens.onInteraction))
            .disabled(!store.allowsMutations || !store.supportsEndpointDeletion || store.deletingEndpointIDs.contains(endpoint.id))
            .help(!store.allowsMutations ? store.mutationUnavailableReason : (store.supportsEndpointDeletion ? "移除本机登记；不会停止远端任务" : "当前本机服务版本不支持移除"))
        } else {
            Button(action: claim) {
                Label("在此认领", systemImage: "key.fill")
                    .font(.system(size: 11, weight: .semibold))
            }
            .buttonStyle(HomeClaimButtonStyle(tint: DesignTokens.interaction, foreground: DesignTokens.onInteraction))
            .disabled(!store.allowsMutations)
            .help(store.allowsMutations ? "仅在此服务器上申请 GPU" : store.mutationUnavailableReason)
        }
    }

    private var gpuMemoryDetail: String? {
        guard !gpus.isEmpty, gpus.allSatisfy({ $0.memoryUsedMiB != nil }) else { return nil }
        let used = gpus.compactMap(\.memoryUsedMiB).reduce(0, +) / 1024
        let total = gpus.map(\.totalVRAMMiB).reduce(0, +) / 1024
        return "\(used) / \(total) GB"
    }

    private var cpuDetail: String? {
        guard let count = endpoint.cpuCount, let load = endpoint.load1m else { return nil }
        return "\(String(format: "%.1f", load)) / \(count) 核"
    }

    private var memoryDetail: String? {
        guard let total = endpoint.memoryTotalMiB, let available = endpoint.memoryAvailableMiB else { return nil }
        return "可用 \(available / 1024) / \(total / 1024) GB"
    }

    private func metricPercent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int((value * 100).rounded()))%"
    }
}

private struct SpatialMetric: View {
    let label: String
    let value: String
    let detail: String?
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
            Text(value)
                .font(.system(size: 19, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
            Text(detail ?? "暂无数据")
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(detail == nil ? DesignTokens.mutedInk.opacity(0.65) : color)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 4)
    }
}

private struct SpatialGPUCell: View {
    let gpu: GPURecord
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 9) {
                GPUUsageGlyph(gpu: gpu, diameter: 34)
                VStack(alignment: .leading, spacing: 3) {
                    Text("GPU \(gpu.index)")
                        .font(.system(size: 12, weight: .semibold, design: .rounded))
                    Text(gpuStateLabel(gpu.state))
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(10)
            .background(DesignTokens.glassSmoke.opacity(0.14), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .buttonStyle(.plain)
    }
}

private struct ServerPool: View {
    @ObservedObject var store: BrokerStore
    let snapshot: BrokerSnapshot
    let claimEndpoint: (String) -> Void
    let openEndpoint: (EndpointRecord) -> Void
    let removeEndpoint: (EndpointRecord) -> Void
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("服务器")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Text("在线情况和 GPU 可用数")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Spacer()
                Text("\(snapshot.summary.totalGPUs) 个 GPU")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(DesignTokens.selection.opacity(0.85), in: Capsule())
            }

            if snapshot.endpoints.isEmpty {
                EmptyServerPool()
                    .background(DesignTokens.surface.opacity(0.78), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 300, maximum: 430), spacing: 12)],
                    alignment: .leading,
                    spacing: 12
                ) {
                    ForEach(snapshot.endpoints) { endpoint in
                        ServerAccessoryCard(
                            store: store,
                            endpoint: endpoint,
                            gpus: snapshot.gpus(for: endpoint),
                            claim: { claimEndpoint(endpoint.id) },
                            open: { openEndpoint(endpoint) },
                            remove: { removeEndpoint(endpoint) },
                            selectGPU: selectGPU
                        )
                    }
                }
            }
        }
    }
}

private struct ServerAccessoryCard: View {
    @ObservedObject var store: BrokerStore
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let claim: () -> Void
    let open: () -> Void
    let remove: () -> Void
    let selectGPU: (GPURecord) -> Void
    @State private var hovering = false

    private var availableGPUCount: Int {
        gpus.filter { $0.state == "AVAILABLE" }.count
    }

    private var averageMemoryFraction: Double? {
        endpointAverageMemoryFraction(endpoint: endpoint, gpus: gpus)
    }

    private var averageUtilizationFraction: Double? {
        endpointAverageUtilizationFraction(endpoint: endpoint, gpus: gpus)
    }

    private var gpuMemoryDetail: String? {
        guard !gpus.isEmpty, gpus.allSatisfy({ $0.memoryUsedMiB != nil }) else { return nil }
        let used = gpus.compactMap(\.memoryUsedMiB).reduce(0, +) / 1024
        let total = gpus.map(\.totalVRAMMiB).reduce(0, +) / 1024
        return "\(used) / \(total) GB"
    }

    private var gpuUtilizationDetail: String? {
        let observed = gpus.compactMap(\.utilization).count
        return observed > 0 ? "\(observed) 块 GPU 平均" : nil
    }

    private var cpuLoadDetail: String? {
        guard let cpuCount = endpoint.cpuCount, let load1m = endpoint.load1m else { return nil }
        return "1 分钟负载 \(String(format: "%.1f", load1m)) / \(cpuCount) 核"
    }

    private var memoryDetail: String? {
        guard let total = endpoint.memoryTotalMiB, let available = endpoint.memoryAvailableMiB else { return nil }
        return "可用 \(available / 1024) / \(total / 1024) GB"
    }

    private var isUnavailable: Bool {
        ["ERROR", "STALE", "DISABLED", "DRAINING", "RETIRED"].contains(endpoint.monitorStatus)
    }

    private var isRemoving: Bool {
        store.deletingEndpointIDs.contains(endpoint.id)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                ZStack {
                    Circle().fill(statusColor.opacity(0.16))
                    Image(systemName: endpoint.monitorStatus == "ONLINE" ? "server.rack" : "exclamationmark.triangle.fill")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(statusColor)
                }
                .frame(width: 34, height: 34)

                VStack(alignment: .leading, spacing: 3) {
                    Text(endpoint.sshCommand)
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    HStack(spacing: 5) {
                        StatusDot(status: endpoint.monitorStatus)
                        Text(statusLine)
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 2) {
                    Text(gpus.isEmpty ? "—" : "\(availableGPUCount) / \(gpus.count)")
                        .font(.system(size: 17, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.ink)
                    Text(gpus.isEmpty ? "GPU 状态" : "GPU 可用")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Button(action: open) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(DesignTokens.ink)
                        .frame(width: 28, height: 28)
                        .background(DesignTokens.selection.opacity(0.86), in: Circle())
                }
                .buttonStyle(.plain)
                .help("查看服务器详情")
            }

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 11) {
                ServerMetric(label: "平均 GPU 显存", value: averageMemoryFraction, detail: gpuMemoryDetail, tint: DesignTokens.interaction)
                ServerMetric(label: "平均 GPU 利用率", value: averageUtilizationFraction, detail: gpuUtilizationDetail, tint: DesignTokens.warning)
                ServerMetric(label: "CPU 负载", value: endpoint.cpuLoadFraction, detail: cpuLoadDetail, tint: DesignTokens.ink, help: "1 分钟负载 ÷ CPU 核数，不等同于 CPU 利用率")
                ServerMetric(label: "系统内存", value: endpoint.memoryFraction, detail: memoryDetail, tint: DesignTokens.success)
            }

            if !gpus.isEmpty {
                ServerLeaseSummary(gpus: gpus)
            }

            VStack(alignment: .leading, spacing: 9) {
                if gpus.isEmpty {
                    Text(endpoint.monitorDetail ?? (isUnavailable ? "连接不可用" : "正在读取 GPU 状态"))
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(isUnavailable ? DesignTokens.danger : DesignTokens.mutedInk)
                        .lineLimit(2)
                } else {
                    LazyVGrid(columns: Array(repeating: GridItem(.fixed(28), spacing: 5), count: min(max(gpus.count, 1), 8)), spacing: 5) {
                        ForEach(gpus.sorted { $0.index < $1.index }) { gpu in
                            GPUUsageRing(gpu: gpu, diameter: 28, select: { selectGPU(gpu) })
                        }
                    }
                }
                HStack {
                    Text(footerHint)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    if isUnavailable {
                        Button(action: remove) {
                            Label(isRemoving ? "移除中" : "移除", systemImage: "trash")
                                .font(.system(size: 11, weight: .semibold))
                        }
                        .buttonStyle(HomeClaimButtonStyle(tint: DesignTokens.danger, foreground: DesignTokens.onInteraction))
                        .help(store.allowsMutations ? removeHelp : store.mutationUnavailableReason)
                        .disabled(!store.allowsMutations || isRemoving || !store.supportsEndpointDeletion)
                    } else {
                        Button(action: claim) {
                            Label("认领", systemImage: "key.fill")
                                .font(.system(size: 11, weight: .semibold))
                        }
                        .buttonStyle(HomeClaimButtonStyle())
                        .disabled(!store.allowsMutations)
                        .help(store.allowsMutations ? "仅在此服务器上申请 GPU" : store.mutationUnavailableReason)
                    }
                }
            }
        }
        .padding(16)
        .frame(minHeight: 218, alignment: .top)
        .background(cardBackground, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(DesignTokens.surfaceStroke.opacity(hovering ? 1 : 0.78), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(hovering ? 0.13 : 0.06), radius: hovering ? 13 : 7, y: 5)
        .scaleEffect(hovering ? 1.006 : 1)
        .animation(.easeOut(duration: 0.2), value: hovering)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .contain)
    }

    private var statusColor: Color {
        switch endpoint.monitorStatus {
        case "ONLINE": return DesignTokens.success
        case "PENDING", "DRAINING": return DesignTokens.warning
        case "ERROR", "STALE", "RETIRED", "DISABLED": return DesignTokens.danger
        default: return DesignTokens.mutedInk
        }
    }

    private var cardBackground: Color {
        if isUnavailable { return DesignTokens.surface.opacity(0.72) }
        if endpoint.monitorStatus == "PENDING" { return DesignTokens.selection.opacity(0.48) }
        return DesignTokens.surface.opacity(0.90)
    }

    private var statusLine: String {
        endpoint.monitorLabel
    }

    private var footerHint: String {
        if isUnavailable {
            return store.supportsEndpointDeletion ? "可刷新状态或移除服务器" : "旧版本服务暂不支持移除"
        }
        return gpus.isEmpty ? "读取完成后显示 GPU 明细" : "点击编号查看 GPU 详情"
    }

    private var removeHelp: String {
        guard store.supportsEndpointDeletion else {
            return "当前本机服务不支持移除服务器。请重启或升级服务。"
        }
        return "删除本机登记和采集历史；不会关闭远端机器或停止任务"
    }
}

private func endpointAverageMemoryFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { gpu -> Double? in
        guard gpu.totalVRAMMiB > 0, gpu.memoryUsedMiB != nil else { return nil }
        return gpu.memoryFraction
    }
    guard !values.isEmpty else { return nil }
    return values.reduce(0, +) / Double(values.count)
}

private func endpointAverageUtilizationFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { $0.utilization }.map { Double($0) / 100 }
    guard !values.isEmpty else { return nil }
    return min(max(values.reduce(0, +) / Double(values.count), 0), 1)
}

private func percentageLabel(_ value: Double?) -> String {
    value.map { "\(Int(($0 * 100).rounded()))%" } ?? "—"
}

private func isGPUClaimed(_ gpu: GPURecord) -> Bool {
    return ["HELD", "LEASED_IDLE", "RUNNING_MANAGED", "ORPHANED_BUSY", "CONFLICT", "RESERVED"].contains(gpu.state)
}

private func gpuStateColor(_ state: String) -> Color {
    switch state {
    case "AVAILABLE": return DesignTokens.success
    case "HELD", "LEASED_IDLE": return DesignTokens.interaction
    case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED", "DRAINING", "MAINTENANCE": return DesignTokens.warning
    default: return DesignTokens.danger
    }
}

private func gpuStateLabel(_ state: String) -> String {
    switch state {
    case "AVAILABLE": return "可用"
    case "HELD", "LEASED_IDLE": return "已认领"
    case "RUNNING_MANAGED": return "运行中"
    case "BUSY_UNMANAGED": return "非托管占用"
    case "ORPHANED_BUSY": return "释放后仍占用"
    case "RESERVED": return "已预约"
    case "UNKNOWN_RECOVERING": return "等待状态"
    case "UNKNOWN_STALE": return "状态过期"
    case "UNHEALTHY": return "状态异常"
    case "CONFLICT": return "需要处理"
    case "DISABLED": return "已停用"
    case "MAINTENANCE": return "维护中"
    case "DRAINING": return "排空中"
    case "RETIRED": return "已退役"
    default: return "需处理"
    }
}

private func endpointStateIcon(_ state: String) -> String {
    switch state {
    case "ONLINE": return "server.rack"
    case "PENDING": return "hourglass"
    case "STALE": return "clock.badge.exclamationmark"
    case "ERROR": return "exclamationmark.triangle.fill"
    case "DISABLED": return "pause.circle.fill"
    case "DRAINING": return "arrow.down.forward.and.arrow.up.backward"
    case "RETIRED": return "archivebox.fill"
    default: return "questionmark.diamond.fill"
    }
}

private func localizedStateReason(_ reason: String) -> String {
    if reason == "no fresh telemetry after service start" {
        return "服务启动后还没有读取到状态"
    }
    if reason == "endpoint or GPU is disabled" {
        return "服务器或 GPU 已停用"
    }
    if reason == "lease/process attribution conflict" {
        return "租约和进程归属不一致"
    }
    if reason == "lease expired while a compute process remains" {
        return "租约已过期，但仍检测到计算进程"
    }
    if reason == "bound workload process observed" {
        return "已检测到登记过的任务进程"
    }
    if reason == "compute process observed; admission blocked" {
        return "检测到未登记的计算进程，暂不能分配"
    }
    if reason == "exclusive lease active" {
        return "已有独占租约"
    }
    if reason.hasPrefix("telemetry age "), reason.contains("exceeds stale threshold") {
        return "状态数据已过期"
    }
    if reason.hasPrefix("reservation "), reason.contains(" is active") {
        return "预约正在生效"
    }
    return reason
}

private func formattedTimestamp(_ value: String?) -> String {
    guard let value else { return "未知" }
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    guard let date else { return "时间格式异常" }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "M 月 d 日 HH:mm"
    return formatter.string(from: date)
}

private struct LeaseSummaryGroup: Identifiable {
    let id: String
    let owner: String
    let task: String
    let count: Int
}

private func leaseSummaryGroups(gpus: [GPURecord]) -> [LeaseSummaryGroup] {
    var groups: [String: (owner: String, task: String, count: Int)] = [:]
    for gpu in gpus where isGPUClaimed(gpu) {
        let owner = gpu.owner ?? "未知操作者"
        let task = gpu.taskReference ?? "未标注任务"
        let key = "\(owner)|\(task)"
        if let existing = groups[key] {
            groups[key] = (existing.owner, existing.task, existing.count + 1)
        } else {
            groups[key] = (owner, task, 1)
        }
    }
    return groups.map { LeaseSummaryGroup(id: $0.key, owner: $0.value.owner, task: $0.value.task, count: $0.value.count) }
        .sorted { lhs, rhs in
            lhs.count == rhs.count ? lhs.owner < rhs.owner : lhs.count > rhs.count
        }
}

private struct ServerLeaseSummary: View {
    let gpus: [GPURecord]

    private var groups: [LeaseSummaryGroup] {
        leaseSummaryGroups(gpus: gpus)
    }

    private var claimedCount: Int {
        gpus.filter(isGPUClaimed).count
    }

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: "person.2.fill")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(groups.isEmpty ? DesignTokens.mutedInk : DesignTokens.interaction)
                .frame(width: 24, height: 24)
                .background(DesignTokens.surface.opacity(0.72), in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text("使用情况")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                Text(summaryText)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 0)
            if !gpus.isEmpty {
                Text("\(claimedCount) / \(gpus.count)")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(DesignTokens.surface.opacity(0.64), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var summaryText: String {
        guard !gpus.isEmpty else { return "等待 GPU 状态" }
        guard let first = groups.first else { return "无活跃租约" }
        let extra = groups.count > 1 ? "，+\(groups.count - 1)" : ""
        return "\(first.owner) · \(first.task) · \(first.count) GPU\(extra)"
    }
}

private enum SpatialLeaseMode: Hashable {
    case active
    case queued
}

private struct SpatialLeaseDesk: View {
    @ObservedObject var store: BrokerStore
    @State private var mode: SpatialLeaseMode = .active
    @State private var selectedLeaseID = ""
    @State private var selectedRequestID = ""
    @State private var inlineMessage: String?

    private var selectedLease: LeaseRecord? {
        store.snapshot.leases.first { $0.id == selectedLeaseID } ?? store.snapshot.leases.first
    }

    private var selectedRequest: AllocationRequestRecord? {
        store.snapshot.requests.first { $0.id == selectedRequestID } ?? store.snapshot.requests.first
    }

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("租约")
                            .font(.system(size: 18, weight: .semibold))
                        Text("\(store.snapshot.leases.count) 个使用中")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                    Spacer()
                    Text("\(store.snapshot.leases.reduce(0) { $0 + $1.gpuIDs.count }) GPU")
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                .padding(20)

                Picker("租约类型", selection: $mode) {
                    Text("使用中 \(store.snapshot.leases.count)").tag(SpatialLeaseMode.active)
                    Text("等待 \(store.snapshot.requests.count)").tag(SpatialLeaseMode.queued)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .padding(.horizontal, 14)
                .padding(.bottom, 12)

                Divider()

                ScrollView {
                    LazyVStack(spacing: 6) {
                        if mode == .active {
                            ForEach(store.snapshot.leases) { lease in
                                SpatialLeaseRow(lease: lease, selected: lease.id == selectedLease?.id) {
                                    withAnimation(.easeOut(duration: 0.14)) { selectedLeaseID = lease.id }
                                }
                            }
                        } else {
                            ForEach(store.snapshot.requests) { request in
                                SpatialRequestRow(request: request, selected: request.id == selectedRequest?.id) {
                                    withAnimation(.easeOut(duration: 0.14)) { selectedRequestID = request.id }
                                }
                            }
                        }
                    }
                    .padding(10)
                }

                Label("只管理资源归属", systemImage: "hand.raised.fill")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .padding(14)
            }
            .frame(width: 304)
            .background(DesignTokens.glassSmoke.opacity(0.62))

            Divider().opacity(0.36)

            Group {
                if mode == .active, let selectedLease {
                    SpatialLeaseDetail(
                        store: store,
                        lease: selectedLease,
                        gpus: store.snapshot.gpus,
                        inlineMessage: inlineMessage,
                        release: { release(selectedLease) }
                    )
                    .id(selectedLease.id)
                } else if mode == .queued, let selectedRequest {
                    SpatialRequestDetail(request: selectedRequest)
                        .id(selectedRequest.id)
                } else {
                    ContentUnavailableView(
                        mode == .active ? "没有使用中的租约" : "没有等待分配的请求",
                        systemImage: mode == .active ? "checkmark.circle" : "hourglass"
                    )
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .transition(.opacity.combined(with: .offset(x: 8)))
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .spatialContentSurface()
        }
        .onAppear { ensureSelection() }
        .onChange(of: mode) { _, _ in ensureSelection() }
        .onChange(of: store.snapshot.leases.map(\.id)) { _, _ in ensureSelection() }
        .onChange(of: store.snapshot.requests.map(\.id)) { _, _ in ensureSelection() }
    }

    private func ensureSelection() {
        if !store.snapshot.leases.contains(where: { $0.id == selectedLeaseID }) {
            selectedLeaseID = store.snapshot.leases.first?.id ?? ""
        }
        if !store.snapshot.requests.contains(where: { $0.id == selectedRequestID }) {
            selectedRequestID = store.snapshot.requests.first?.id ?? ""
        }
    }

    private func release(_ lease: LeaseRecord) {
        guard confirmLeaseRelease(lease) else { return }
        inlineMessage = nil
        store.releaseLease(lease) { success, error in
            if !success { inlineMessage = error ?? "没有归还成功，请稍后再试。" }
        }
    }
}

private struct SpatialLeaseRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let lease: LeaseRecord
    let selected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 11) {
                Image(systemName: "key.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(DesignTokens.interaction)
                    .frame(width: 32, height: 32)
                    .background(DesignTokens.interaction.opacity(0.13), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text(lease.projectID)
                        .font(.system(size: 11, weight: .semibold))
                        .lineLimit(1)
                    Text(lease.taskReference ?? lease.purpose ?? "未命名任务")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 5) {
                    StatusDot(status: lease.state == "ACTIVE" ? "ONLINE" : "PENDING")
                    Text("\(lease.gpuIDs.count) GPU")
                        .font(.system(size: 9, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
            }
            .padding(.horizontal, 12)
            .frame(height: 56)
            .background(
                selected ? DesignTokens.interaction.opacity(0.14) : DesignTokens.ink.opacity(hovering ? 0.045 : 0),
                in: RoundedRectangle(cornerRadius: 8, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
    }
}

private struct SpatialRequestRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let request: AllocationRequestRecord
    let selected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 11) {
                Image(systemName: "hourglass")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(DesignTokens.warning)
                    .frame(width: 32, height: 32)
                    .background(DesignTokens.warning.opacity(0.13), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text(request.projectID)
                        .font(.system(size: 11, weight: .semibold))
                        .lineLimit(1)
                    Text(request.taskReference)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                Text("\(request.gpuCount) GPU")
                    .font(.system(size: 9, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(.horizontal, 12)
            .frame(height: 56)
            .background(
                selected ? DesignTokens.warning.opacity(0.14) : DesignTokens.ink.opacity(hovering ? 0.045 : 0),
                in: RoundedRectangle(cornerRadius: 8, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
    }
}

private struct SpatialLeaseDetail: View {
    @ObservedObject var store: BrokerStore
    let lease: LeaseRecord
    let gpus: [GPURecord]
    let inlineMessage: String?
    let release: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .top, spacing: 16) {
                    VStack(alignment: .leading, spacing: 7) {
                        HStack(spacing: 7) {
                            Circle().fill(DesignTokens.success).frame(width: 7, height: 7)
                            Text(lease.stateLabel)
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(DesignTokens.mutedInk)
                        }
                        Text(lease.projectID)
                            .font(.system(size: 24, weight: .semibold))
                            .foregroundStyle(DesignTokens.ink)
                            .lineLimit(2)
                        Text(lease.taskReference ?? lease.purpose ?? "未命名任务")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .lineLimit(3)
                    }
                    Spacer(minLength: 0)
                    Button(action: release) {
                        Label(store.releasingLeaseIDs.contains(lease.id) ? "归还中" : "归还", systemImage: "arrow.uturn.backward")
                            .font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(HomeClaimButtonStyle(tint: DesignTokens.danger.opacity(0.16), foreground: DesignTokens.danger))
                    .disabled(!store.allowsMutations || store.releasingLeaseIDs.contains(lease.id))
                    .help(store.allowsMutations ? "归还 GPU；不会停止远端任务" : store.mutationUnavailableReason)
                }

                if let inlineMessage {
                    NoticeBanner(message: inlineMessage, color: DesignTokens.danger, icon: "exclamationmark.triangle.fill")
                }

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 180, maximum: 260), spacing: 18)], spacing: 18) {
                    SpatialFact(label: "操作者", value: lease.actorID, icon: "person.crop.circle")
                    SpatialFact(label: "GPU", value: "\(lease.gpuIDs.count) 块", icon: "square.grid.3x3.fill")
                    SpatialFact(label: "到期", value: formattedTimestamp(lease.expiresAt), icon: "clock.fill")
                }

                Divider()

                VStack(alignment: .leading, spacing: 12) {
                    Text("分配的 GPU")
                        .font(.system(size: 16, weight: .semibold))
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 170, maximum: 220), spacing: 10)], spacing: 10) {
                        ForEach(lease.gpuIDs, id: \.self) { gpuID in
                            SpatialLeaseGPU(gpuID: gpuID, gpu: gpus.first { $0.id == gpuID })
                        }
                    }
                }

                Label("归还只释放资源归属，不会停止远端任务", systemImage: "hand.raised.fill")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(30)
            .padding(.bottom, 70)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct SpatialRequestDetail: View {
    let request: AllocationRequestRecord

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(spacing: 7) {
                    Circle().fill(DesignTokens.warning).frame(width: 7, height: 7)
                    Text(request.stateLabel)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Text(request.projectID)
                    .font(.system(size: 24, weight: .semibold))
                Text(request.taskReference)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(3)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 180, maximum: 260), spacing: 18)], spacing: 18) {
                    SpatialFact(label: "操作者", value: request.actorID, icon: "person.crop.circle")
                    SpatialFact(label: "需要", value: "\(request.gpuCount) 块 GPU", icon: "square.grid.3x3.fill")
                    SpatialFact(label: "提交时间", value: formattedTimestamp(request.createdAt), icon: "clock.fill")
                }
                if let reason = request.blockedReason, !reason.isEmpty {
                    DetailCallout(icon: "hourglass", color: DesignTokens.warning, message: localizedStateReason(reason))
                }
                if !request.purpose.isEmpty {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("用途").font(.system(size: 12, weight: .semibold))
                        Text(request.purpose)
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                }
            }
            .padding(30)
            .padding(.bottom, 70)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct SpatialFact: View {
    let label: String
    let value: String
    let icon: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(DesignTokens.interaction)
                .frame(width: 30, height: 30)
                .background(DesignTokens.interaction.opacity(0.10), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                Text(label)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                Text(value)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(2)
            }
            Spacer(minLength: 0)
        }
    }
}

private struct SpatialLeaseGPU: View {
    let gpuID: String
    let gpu: GPURecord?

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "square.grid.3x3.fill")
                .foregroundStyle(DesignTokens.interaction)
            VStack(alignment: .leading, spacing: 2) {
                Text(gpu.map { "GPU \($0.index)" } ?? "GPU")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                Text(gpu?.name ?? gpuID)
                    .font(.system(size: 9, weight: .medium, design: .monospaced))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(DesignTokens.glassSmoke.opacity(0.14), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct LeaseStatusSection: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var store: BrokerStore
    @State private var inlineMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            LeaseOverviewBar(
                leaseCount: store.snapshot.leases.count,
                gpuCount: store.snapshot.leases.reduce(0) { $0 + $1.gpuIDs.count },
                requestCount: store.snapshot.requests.count
            )

            if let inlineMessage {
                NoticeBanner(message: inlineMessage, color: DesignTokens.danger, icon: "exclamationmark.triangle.fill")
            }

            VStack(alignment: .leading, spacing: 14) {
                HomeSectionTitle(title: "正在使用", subtitle: leaseSectionSubtitle)
                if store.snapshot.leases.isEmpty {
                    EmptyLeasePanel(message: "目前没有分配中的 GPU")
                } else {
                    LazyVGrid(
                        columns: [GridItem(.adaptive(minimum: 320, maximum: 520), spacing: 14)],
                        alignment: .leading,
                        spacing: 14
                    ) {
                        ForEach(store.snapshot.leases) { lease in
                            LeaseHomeCard(
                                lease: lease,
                                isReleasing: store.releasingLeaseIDs.contains(lease.id),
                                mutationsAllowed: store.allowsMutations,
                                mutationUnavailableReason: store.mutationUnavailableReason,
                                release: { release(lease) }
                            )
                            .transition(.opacity.combined(with: .scale(scale: 0.98)))
                        }
                    }
                    .animation(
                        reduceMotion ? nil : .easeInOut(duration: 0.2),
                        value: store.snapshot.leases.map(\.id)
                    )
                }
            }

            if !store.snapshot.requests.isEmpty {
                VStack(alignment: .leading, spacing: 14) {
                    HomeSectionTitle(title: "等待分配", subtitle: "分配完成后再启动任务")
                    LazyVStack(spacing: 10) {
                        ForEach(store.snapshot.requests) { request in
                            RequestRow(request: request)
                        }
                    }
                }
            }

            Label("GPU Broker 只管理资源归属，不会操作远端任务", systemImage: "hand.raised.fill")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .padding(.top, 2)
        }
    }

    private var leaseSectionSubtitle: String {
        let count = store.snapshot.leases.count
        return count == 0 ? "所有 GPU 都已归还" : "\(count) 个租约"
    }

    private func release(_ lease: LeaseRecord) {
        guard confirmLeaseRelease(lease) else { return }
        inlineMessage = nil
        store.releaseLease(lease) { success, error in
            if !success {
                inlineMessage = error ?? "没有归还成功，请稍后再试。"
            }
        }
    }
}

private struct LeaseOverviewBar: View {
    let leaseCount: Int
    let gpuCount: Int
    let requestCount: Int

    var body: some View {
        HStack(spacing: 22) {
            LeaseOverviewItem(value: "\(leaseCount)", label: "租约", icon: "key.fill", color: DesignTokens.interaction)
            Divider().frame(height: 30)
            LeaseOverviewItem(value: "\(gpuCount)", label: "块 GPU", icon: "square.grid.3x3.fill", color: DesignTokens.success)
            Divider().frame(height: 30)
            LeaseOverviewItem(
                value: "\(requestCount)",
                label: requestCount == 0 ? "无需等待" : "等待分配",
                icon: requestCount == 0 ? "checkmark.circle.fill" : "hourglass",
                color: requestCount == 0 ? DesignTokens.success : DesignTokens.warning
            )
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 4)
        .frame(minHeight: 48)
    }
}

private struct LeaseOverviewItem: View {
    let value: String
    let label: String
    let icon: String
    let color: Color

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: icon)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 32, height: 32)
                .background(color.opacity(0.14), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 1) {
                Text(value)
                    .font(.system(size: 18, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
                Text(label)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
    }
}

private struct EmptyLeasePanel: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(DesignTokens.mutedInk)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(DesignTokens.surface.opacity(0.72), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

private struct LeaseHomeCard: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let lease: LeaseRecord
    let isReleasing: Bool
    let mutationsAllowed: Bool
    let mutationUnavailableReason: String
    let release: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "key.fill")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(DesignTokens.onInteraction)
                    .frame(width: 42, height: 42)
                    .background(DesignTokens.interaction, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text(lease.projectID)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                    Label(lease.stateLabel, systemImage: "circle.fill")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(DesignTokens.success)
                }
                Spacer(minLength: 0)
                Button(action: release) {
                    Label(isReleasing ? "归还中" : "归还", systemImage: "arrow.uturn.backward")
                        .font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(HomeClaimButtonStyle(tint: DesignTokens.danger.opacity(0.16), foreground: DesignTokens.danger))
                .disabled(!mutationsAllowed || isReleasing)
                .help(mutationsAllowed ? "归还 GPU；不会停止远端任务" : mutationUnavailableReason)
            }

            Text(lease.taskReference ?? lease.purpose ?? "未命名任务")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(2)
                .frame(minHeight: 38, alignment: .topLeading)

            Label(lease.actorID, systemImage: "person.crop.circle")
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
                .truncationMode(.middle)

            HStack(spacing: 18) {
                Label("\(lease.gpuIDs.count) 块 GPU", systemImage: "square.grid.3x3.fill")
                Label("\(formattedTimestamp(lease.expiresAt)) 到期", systemImage: "clock.fill")
            }
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(DesignTokens.mutedInk)
        }
        .padding(16)
        .frame(maxWidth: .infinity, minHeight: 184, alignment: .topLeading)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(DesignTokens.surfaceStroke.opacity(hovering ? 1 : 0.74), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(hovering ? 0.12 : 0.055), radius: hovering ? 14 : 7, y: 5)
        .scaleEffect(hovering && !reduceMotion ? 1.006 : 1)
        .animation(.easeOut(duration: 0.18), value: hovering)
        .onHover { hovering = $0 }
    }
}

private struct RequestRow: View {
    let request: AllocationRequestRecord

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: "hourglass")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(DesignTokens.warning)
                .frame(width: 34, height: 34)
                .background(DesignTokens.warning.opacity(0.14), in: Circle())
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(request.projectID)
                        .font(.system(size: 13, weight: .semibold, design: .monospaced))
                        .foregroundStyle(DesignTokens.ink)
                    Text(request.stateLabel)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(DesignTokens.warning)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(DesignTokens.warning.opacity(0.14), in: Capsule())
                }
                Text(request.taskReference)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(DesignTokens.ink.opacity(0.78))
                    .lineLimit(1)
                Text(requestDetail)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private var requestDetail: String {
        if let blockedReason = request.blockedReason, !blockedReason.isEmpty {
            return "\(request.gpuCount) 个 GPU · \(localizedStateReason(blockedReason))"
        }
        return "\(request.gpuCount) 块 GPU · \(formattedTimestamp(request.createdAt)) 提交"
    }
}

private struct GPUUsageRing: View {
    let gpu: GPURecord
    let diameter: CGFloat
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            GPUUsageGlyph(gpu: gpu, diameter: diameter)
        }
        .buttonStyle(.plain)
        .help(gpuTooltip)
        .accessibilityLabel("GPU \(gpu.index)，\(gpuStateLabel(gpu.state))，使用率 \(Int((usageFraction * 100).rounded()))%")
    }

    private var usageFraction: Double {
        if let utilization = gpu.utilization {
            return min(max(Double(utilization) / 100, 0), 1)
        }
        guard gpu.memoryUsedMiB != nil else { return 0 }
        return gpu.memoryFraction
    }

    private var gpuTooltip: String {
        var details = "\(gpu.name) · \(gpuStateLabel(gpu.state)) · 显存 \(gpu.memoryLabel)"
        if let utilization = gpu.utilization { details += " · 利用率 \(utilization)%" }
        if let owner = gpu.owner { details += " · \(owner)" }
        if let task = gpu.taskReference { details += " · \(task)" }
        return details
    }
}

private struct GPUUsageGlyph: View {
    let gpu: GPURecord
    let diameter: CGFloat

    var body: some View {
        ZStack {
            Circle()
                .stroke(DesignTokens.ink.opacity(0.10), lineWidth: 4)
            Circle()
                .trim(from: 0, to: usageFraction)
                .stroke(gpuStateColor(gpu.state), style: StrokeStyle(lineWidth: 4, lineCap: .round))
                .rotationEffect(.degrees(-90))
            Circle()
                .fill(gpuStateColor(gpu.state).opacity(0.18))
                .frame(width: diameter * 0.58, height: diameter * 0.58)
            Text("\(gpu.index)")
                .font(.system(size: diameter > 32 ? 10 : 9, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
        }
        .frame(width: diameter, height: diameter)
    }

    private var usageFraction: Double {
        if let utilization = gpu.utilization {
            return min(max(Double(utilization) / 100, 0), 1)
        }
        guard gpu.memoryUsedMiB != nil else { return 0 }
        return gpu.memoryFraction
    }
}

private struct ServerMetric: View {
    let label: String
    let value: Double?
    let detail: String?
    let tint: Color
    var help: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 4) {
                Text(label)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                if let help {
                    Image(systemName: "info.circle")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(DesignTokens.mutedInk.opacity(0.75))
                        .help(help)
                }
                Spacer(minLength: 0)
                Text(value.map { "\(Int(($0 * 100).rounded()))%" } ?? "—")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
            }
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(DesignTokens.ink.opacity(0.10))
                    if let value {
                        Capsule()
                            .fill(tint)
                            .frame(width: max(3, proxy.size.width * value))
                    } else {
                        Capsule()
                            .stroke(DesignTokens.mutedInk.opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [2, 3]))
                    }
                }
            }
            .frame(height: 5)
            if let detail {
                Text(detail)
                    .font(.system(size: 9, weight: .medium, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label) \(value.map { "\(Int(($0 * 100).rounded()))%" } ?? "无数据") \(detail ?? "")")
    }
}

private struct GPUAccessoryChip: View {
    let gpu: GPURecord
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 5) {
                Image(systemName: stateIcon)
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(stateColor)
                Text("GPU \(gpu.index)")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(gpu.memoryUsedMiB == nil ? "—" : "\(Int((gpu.memoryFraction * 100).rounded()))%")
                    .font(.system(size: 9, weight: .medium, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(.horizontal, 8)
            .frame(height: 28)
            .background(DesignTokens.surface.opacity(0.74), in: Capsule())
        }
        .buttonStyle(.plain)
        .help(gpuTooltip)
    }

    private var stateIcon: String {
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.success
        case "HELD", "LEASED_IDLE": return DesignTokens.interaction
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }

    private var gpuTooltip: String {
        var details = "\(gpu.name) · \(gpu.vramLabel) · \(gpuStateLabel(gpu.state))"
        if let owner = gpu.owner { details += " · \(owner)" }
        if let task = gpu.taskReference { details += " · \(task)" }
        return details
    }
}

private struct HomeClaimButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var tint = DesignTokens.selection
    var foreground = DesignTokens.ink

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(isEnabled ? foreground : Color(nsColor: .disabledControlTextColor))
            .padding(.horizontal, 11)
            .frame(height: 28)
            .background(
                (isEnabled ? tint : Color(nsColor: .disabledControlTextColor))
                    .opacity(isEnabled ? (configuration.isPressed ? 0.62 : 0.90) : 0.12),
                in: Capsule()
            )
            .scaleEffect(configuration.isPressed && isEnabled ? 0.97 : 1)
            .opacity(isEnabled ? 1 : 0.72)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

private struct ServerDetailSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var selectedGPU: GPURecord?
    let endpoint: EndpointRecord
    let gpus: [GPURecord]

    private var availableGPUCount: Int {
        guard endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter { $0.state == "AVAILABLE" }.count
    }

    private var claimedGPUCount: Int {
        gpus.filter(isGPUClaimed).count
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                SheetTitle(
                    icon: endpoint.monitorStatus == "ONLINE" ? "server.rack" : endpointStateIcon(endpoint.monitorStatus),
                    title: "服务器详情",
                    subtitle: endpoint.sshCommand
                )

                LazyVGrid(columns: [GridItem(.adaptive(minimum: 128, maximum: 190), spacing: 12)], spacing: 12) {
                    GPUDetailMetric(label: "GPU 可分配", value: capacityLabel, accent: DesignTokens.success)
                    GPUDetailMetric(label: "已认领", value: gpus.isEmpty ? "等待状态" : "\(claimedGPUCount) / \(gpus.count)", accent: DesignTokens.interaction)
                    GPUDetailMetric(label: "平均利用率", value: percentageLabel(endpointAverageUtilizationFraction(endpoint: endpoint, gpus: gpus)), accent: DesignTokens.warning)
                    GPUDetailMetric(label: "平均显存", value: percentageLabel(endpointAverageMemoryFraction(endpoint: endpoint, gpus: gpus)), accent: DesignTokens.ink)
                }

                if !gpus.isEmpty {
                    ServerLeaseSummary(gpus: gpus)
                }

                if let error = store.errorMessage {
                    InlineValidation(message: error)
                }

                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("GPU 明细")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(DesignTokens.ink)
                        Spacer()
                        Text("\(gpus.count) 块")
                            .font(.system(size: 10, weight: .semibold, design: .rounded))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }

                    if gpus.isEmpty {
                        DetailCallout(icon: "waveform.path.ecg", color: DesignTokens.warning, message: "正在读取 GPU 状态。")
                    } else {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: gpus.count > 16 ? 142 : 220, maximum: 280), spacing: 10)], spacing: 10) {
                            ForEach(gpus.sorted { $0.index < $1.index }) { gpu in
                                ServerGPUDetailCard(gpu: gpu, compact: gpus.count > 16) {
                                    selectedGPU = gpu
                                }
                            }
                        }
                    }
                }

                HStack {
                    Label("此处显示上次采集到的协调状态", systemImage: "eye.fill")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                    Spacer()
                    Button("关闭") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                }
            }
            .padding(28)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minWidth: 560, idealWidth: 760, maxWidth: 920, minHeight: 420, idealHeight: 640, maxHeight: 780)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .accessibilityLabel("服务器详情")
        .accessibilityValue("\(endpoint.displayName)，\(endpoint.monitorLabel)，\(gpus.count) 块 GPU")
        .sheet(item: $selectedGPU) { gpu in
            GPUDetailSheet(gpu: gpu)
        }
    }

    private var capacityLabel: String {
        guard endpoint.monitorStatus == "ONLINE" else { return "状态未在线" }
        guard !gpus.isEmpty else { return "等待状态" }
        return "\(availableGPUCount) / \(gpus.count)"
    }
}

private struct ServerGPUDetailCard: View {
    let gpu: GPURecord
    var compact = false
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 10) {
                GPUUsageGlyph(gpu: gpu, diameter: compact ? 26 : 34)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text("GPU \(gpu.index)")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(DesignTokens.ink)
                        Text(gpuStateLabel(gpu.state))
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(gpuStateColor(gpu.state))
                    }
                    Text(gpu.name)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                    if !compact {
                        Text(detailLine)
                            .font(.system(size: 10, weight: .medium, design: .rounded))
                            .foregroundStyle(DesignTokens.ink.opacity(0.78))
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(10)
            .frame(maxWidth: .infinity, minHeight: compact ? 48 : 74, alignment: .leading)
            .background(DesignTokens.surface.opacity(0.76), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help("查看 GPU \(gpu.index) 详情")
        .accessibilityLabel("GPU \(gpu.index)")
        .accessibilityValue("\(gpuStateLabel(gpu.state))，UUID \(gpu.uuidLabel)")
    }

    private var detailLine: String {
        let utilization = gpu.utilization.map { "\($0)%" } ?? "等待状态"
        if let owner = gpu.owner {
            return "\(gpu.memoryLabel) · \(utilization) · \(owner)"
        }
        return "\(gpu.memoryLabel) · \(utilization)"
    }
}

private struct ServerPoolHeader: View {
    var body: some View {
        HStack(spacing: 16) {
            Text("连接")
                .frame(width: 260, alignment: .leading)
            Text("GPU 可用性")
                .frame(width: 112, alignment: .leading)
            Text("资源")
                .frame(maxWidth: .infinity, alignment: .leading)
            Text("操作")
                .frame(width: 46, alignment: .center)
        }
        .font(.system(size: 12, weight: .semibold))
        .foregroundStyle(DesignTokens.onInteraction)
        .padding(.horizontal, 18)
        .frame(height: 42)
        .background(DesignTokens.ink.opacity(0.96))
        .clipShape(RoundedRectangle(cornerRadius: 15, style: .continuous))
        .padding(4)
    }
}

private struct EmptyServerPool: View {
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "server.rack")
                .font(.system(size: 22, weight: .medium))
                .foregroundStyle(DesignTokens.interaction)
            VStack(alignment: .leading, spacing: 3) {
                Text("尚未接入服务器")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text("使用上方“添加服务器”，粘贴标准 SSH 指令即可登记。")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Spacer()
        }
        .padding(24)
    }
}

private struct EndpointRow: View {
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let claim: () -> Void
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 8) {
                Text(endpoint.sshCommand)
                    .font(.system(size: 13, weight: .semibold, design: .monospaced))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                HStack(spacing: 6) {
                    StatusDot(status: endpoint.monitorStatus)
                    Text(endpoint.monitorLabel)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
            }
            .frame(width: 260, alignment: .leading)

            AvailabilityIndicator(gpus: gpus)
                .frame(width: 112, alignment: .leading)

            HStack(spacing: 8) {
                if gpus.isEmpty {
                    Text("正在读取 GPU 状态")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                } else {
                    ForEach(Array(gpus.prefix(4))) { gpu in
                        GPUAccessoryTile(gpu: gpu, select: { selectGPU(gpu) })
                    }
                    if gpus.count > 4 {
                        Text("+\(gpus.count - 4)")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .padding(9)
                            .background(DesignTokens.surface.opacity(0.72), in: Circle())
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: claim) {
                Image(systemName: "checkmark.seal.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .frame(width: 32, height: 32)
            }
            .buttonStyle(.plain)
            .background(DesignTokens.selection.opacity(0.8), in: Circle())
            .help("认领此服务器的 GPU")
            .frame(width: 46)
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
    }
}

private struct StatusDot: View {
    let status: String

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 7, height: 7)
    }

    private var color: Color {
        switch status {
        case "ONLINE": return DesignTokens.success
        case "PENDING", "DRAINING": return DesignTokens.warning
        case "ERROR", "STALE", "DISABLED", "RETIRED": return DesignTokens.danger
        default: return DesignTokens.mutedInk
        }
    }
}

private struct AvailabilityIndicator: View {
    let gpus: [GPURecord]

    var body: some View {
        let available = gpus.filter { $0.state == "AVAILABLE" }.count
        let title = gpus.isEmpty ? "—" : "\(available) / \(gpus.count)"
        VStack(alignment: .leading, spacing: 5) {
            Text(title)
                .font(.system(size: 15, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
            Text(gpus.isEmpty ? "等待状态" : "GPU 可用")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
        }
    }
}

private struct GPUAccessoryTile: View {
    let gpu: GPURecord
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 5) {
                    Image(systemName: stateIcon)
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(stateColor)
                    Text("GPU \(gpu.index)")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Spacer(minLength: 0)
                }
                Text(shortName)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                ProgressView(value: gpu.memoryFraction)
                    .tint(stateColor)
                    .frame(width: 94)
                Text(gpu.memoryLabel)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(10)
            .frame(width: 116, alignment: .leading)
            .background(DesignTokens.surface.opacity(0.78), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(DesignTokens.surfaceStroke, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .help(gpuTooltip)
    }

    private var shortName: String {
        let words = gpu.name.split(separator: " ")
        return words.suffix(2).joined(separator: " ")
    }

    private var stateIcon: String {
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.success
        case "HELD", "LEASED_IDLE": return DesignTokens.interaction
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }

    private var gpuTooltip: String {
        var details = "\(gpu.name) · \(gpu.vramLabel) · \(gpuStateLabel(gpu.state))"
        if let owner = gpu.owner { details += " · \(owner)" }
        if let task = gpu.taskReference { details += " · \(task)" }
        return details
    }
}

private struct GPUDetailSheet: View {
    @Environment(\.dismiss) private var dismiss
    let gpu: GPURecord

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SheetTitle(
                icon: stateIcon,
                title: "GPU \(gpu.index) · \(stateLabel)",
                subtitle: gpu.name
            )
            HStack(spacing: 12) {
                GPUDetailMetric(label: "显存", value: gpu.vramLabel, accent: stateColor)
                GPUDetailMetric(label: "已用显存", value: gpu.memoryLabel, accent: DesignTokens.interaction)
                GPUDetailMetric(label: "计算利用率", value: utilizationLabel, accent: DesignTokens.warning)
                GPUDetailMetric(label: "温度", value: temperatureLabel, accent: DesignTokens.danger)
            }
            if let reason = gpu.stateReason {
                DetailCallout(icon: "info.circle.fill", color: stateColor, message: localizedStateReason(reason))
            }
            if let owner = gpu.owner {
                VStack(alignment: .leading, spacing: 5) {
                    Text("当前租约")
                        .fieldLabel()
                    Text(owner)
                        .font(.system(size: 13, weight: .semibold, design: .monospaced))
                        .foregroundStyle(DesignTokens.ink)
                    if let task = gpu.taskReference {
                        Text(task)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .lineLimit(2)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(DesignTokens.selection.opacity(0.64), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            HStack {
                Spacer()
                Button("关闭") { dismiss() }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(width: 560)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private var utilizationLabel: String {
        guard let value = gpu.utilization else { return "等待状态" }
        return "\(value)%"
    }

    private var temperatureLabel: String {
        guard let value = gpu.temperature else { return "等待状态" }
        return "\(value)°C"
    }

    private var stateLabel: String { gpuStateLabel(gpu.state) }

    private var stateIcon: String {
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.success
        case "HELD", "LEASED_IDLE": return DesignTokens.interaction
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }
}

private struct GPUDetailMetric: View {
    let label: String
    let value: String
    let accent: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Circle()
                .fill(accent)
                .frame(width: 7, height: 7)
            Text(value)
                .font(.system(size: 14, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DesignTokens.surface.opacity(0.76), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

private struct DetailCallout: View {
    let icon: String
    let color: Color
    let message: String

    var body: some View {
        Label(message, systemImage: icon)
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(DesignTokens.ink)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(13)
            .background(color.opacity(0.14), in: RoundedRectangle(cornerRadius: 13, style: .continuous))
    }
}

private struct DataFreshnessCard: View {
    let snapshot: BrokerSnapshot

    var body: some View {
        HStack(spacing: 11) {
            Image(systemName: "waveform.path.ecg")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(DesignTokens.interaction)
                .frame(width: 36, height: 36)
                .background(DesignTokens.interaction.opacity(0.12), in: Circle())
            VStack(alignment: .leading, spacing: 3) {
                Text("在线 GPU 数据")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(freshnessLabel)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private var freshnessLabel: String {
        guard let age = snapshot.dataAgeSeconds else { return "尚无可用的 GPU 状态数据" }
        return "最旧一条约 \(Int(age.rounded())) 秒前更新；连接异常的服务器不计入"
    }
}

private struct CoordinationBoundaryCard: View {
    let message: String

    var body: some View {
        HStack(spacing: 11) {
            Image(systemName: "hand.raised.fill")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(DesignTokens.warning)
                .frame(width: 36, height: 36)
                .background(DesignTokens.warning.opacity(0.14), in: Circle())
            Text(displayMessage)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private var displayMessage: String {
        if message == "A lease coordinates GPUs only; it does not authorize workload launch." {
            return "这里只负责分配 GPU，不代表可以启动或停止远端任务。"
        }
        return message
    }
}

private struct AddServerSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var sshCommand = ""
    @State private var endpointID = ""
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SheetTitle(icon: "server.rack", title: "添加服务器", subtitle: "粘贴 SSH 指令，把机器加入本机资源池。")
            VStack(alignment: .leading, spacing: 8) {
                Text("SSH 指令")
                    .fieldLabel()
                TextField("ssh -p 2201 gpu@server.example.com", text: $sshCommand)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, weight: .medium, design: .monospaced))
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("服务器标识（可选）")
                    .fieldLabel()
                TextField("留空则由主机名自动生成", text: $endpointID)
                    .textFieldStyle(.roundedBorder)
            }
            Text("连接成功后，这台机器可参与 GPU 分配。这里只读取远端状态，不会启动、停止或修改任务。")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
            if let validationMessage {
                InlineValidation(message: validationMessage)
            }
            HStack {
                Spacer()
                Button("取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("添加服务器") { submit() }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                    .keyboardShortcut(.defaultAction)
                    .disabled(!store.allowsMutations || isSubmitting)
                    .help(store.allowsMutations ? "添加服务器" : store.mutationUnavailableReason)
            }
        }
        .padding(28)
        .frame(width: 520)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private func submit() {
        do {
            let draft = try EndpointDraft(command: sshCommand, suppliedID: endpointID)
            validationMessage = nil
            isSubmitting = true
            store.addEndpoint(draft) { success, error in
                isSubmitting = false
                if success {
                    dismiss()
                } else {
                    validationMessage = error
                }
            }
        } catch {
            validationMessage = error.localizedDescription
        }
    }
}

private struct ClaimSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let initialEndpointID: String
    @State private var projectID = ""
    @State private var taskReference = ""
    @State private var purpose = ""
    @State private var gpuCountText = "1"
    @State private var minimumCPUCoresText = ""
    @State private var minimumMemoryMiBText = ""
    @State private var minimumTotalVRAMMiBText = ""
    @State private var minimumFreeVRAMMiBText = ""
    @State private var endpointID: String
    @State private var validationMessage: String?
    @State private var submissionResult: ClaimSubmissionResult?
    @State private var isSubmitting = false

    init(store: BrokerStore, initialEndpointID: String) {
        self.store = store
        self.initialEndpointID = initialEndpointID
        _endpointID = State(initialValue: initialEndpointID)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(icon: "checkmark.seal.fill", title: "认领 GPU", subtitle: "提交后由本机服务分配可用资源；排队时请先不要启动任务。")
            HStack(spacing: 14) {
                LabeledField(label: "项目", placeholder: "project-a", text: $projectID)
                LabeledField(label: "任务", placeholder: "training-042", text: $taskReference)
            }
            LabeledField(label: "用途", placeholder: "说明这次要做什么", text: $purpose)
            HStack(alignment: .bottom, spacing: 14) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("GPU 数量")
                        .fieldLabel()
                    TextField("1", text: $gpuCountText)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 110)
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("服务器范围")
                        .fieldLabel()
                    Picker("服务器范围", selection: $endpointID) {
                        Text("自动选择服务器").tag("")
                        ForEach(store.snapshot.endpoints) { endpoint in
                            Text(endpoint.sshCommand).tag(endpoint.id)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: .infinity)
                }
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("资源下限（可选）")
                    .fieldLabel()
                Text("填写后只会分配满足这些下限的资源；留空表示不限制。")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                    LabeledField(label: "CPU 可用核数", placeholder: "例如 8", text: $minimumCPUCoresText)
                    LabeledField(label: "系统内存 MiB", placeholder: "例如 65536", text: $minimumMemoryMiBText)
                    LabeledField(label: "单卡总显存 MiB", placeholder: "例如 81920", text: $minimumTotalVRAMMiBText)
                    LabeledField(label: "单卡可用显存 MiB", placeholder: "例如 40960", text: $minimumFreeVRAMMiBText)
                }
            }
            if let validationMessage {
                InlineValidation(message: validationMessage)
            }
            if let submissionResult {
                InlineResult(message: submissionResult.message, allocated: submissionResult.allocated)
            }
            HStack {
                Spacer()
                if submissionResult == nil {
                    Button("取消") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Button("提交认领") { submit() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                        .disabled(!store.allowsMutations || isSubmitting)
                    .help(store.allowsMutations ? "提交 GPU 认领" : store.mutationUnavailableReason)
                } else {
                    Button("完成") { dismiss() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(28)
        .frame(width: 620)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .onAppear {
            endpointID = initialEndpointID
        }
    }

    private func submit() {
        guard let gpuCount = Int(gpuCountText), gpuCount > 0 else {
            validationMessage = "GPU 数量必须是大于 0 的整数。"
            return
        }
        let project = projectID.trimmingCharacters(in: .whitespacesAndNewlines)
        let task = taskReference.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedPurpose = purpose.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !project.isEmpty, !task.isEmpty, !trimmedPurpose.isEmpty else {
            validationMessage = "项目、任务和用途都必须填写。"
            return
        }
        validationMessage = nil
        let minimumCPUCores = optionalDouble(minimumCPUCoresText, label: "CPU 可用核数")
        let minimumMemoryMiB = optionalInt(minimumMemoryMiBText, label: "系统内存 MiB")
        let minimumTotalVRAMMiB = optionalInt(minimumTotalVRAMMiBText, label: "单卡总显存 MiB")
        let minimumFreeVRAMMiB = optionalInt(minimumFreeVRAMMiBText, label: "单卡可用显存 MiB")
        if validationMessage != nil { return }
        submissionResult = nil
        isSubmitting = true
        store.submitClaim(
            ClaimDraft(
                projectID: project,
                taskReference: task,
                purpose: trimmedPurpose,
                gpuCount: gpuCount,
                endpointID: endpointID,
                minimumCPUCores: minimumCPUCores,
                minimumMemoryMiB: minimumMemoryMiB,
                minimumTotalVRAMMiB: minimumTotalVRAMMiB,
                minimumFreeVRAMMiB: minimumFreeVRAMMiB
            )
        ) { result, error in
            isSubmitting = false
            if let error {
                validationMessage = error
                return
            }
            submissionResult = result
        }
    }

    private func optionalInt(_ value: String, label: String) -> Int? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        guard let parsed = Int(trimmed), parsed >= 0 else {
            validationMessage = "\(label) 必须是 0 或更大的整数。"
            return nil
        }
        return parsed
    }

    private func optionalDouble(_ value: String, label: String) -> Double? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        guard let parsed = Double(trimmed), parsed >= 0 else {
            validationMessage = "\(label) 必须是 0 或更大的数字。"
            return nil
        }
        return parsed
    }
}

private struct ActorSettingsSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var actorID: String

    init(store: BrokerStore) {
        self.store = store
        _actorID = State(initialValue: store.actorID)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SheetTitle(icon: "person.crop.circle", title: "桌面设置", subtitle: "这个名称只用于记录本机操作，不代表身份认证或权限。")
            VStack(alignment: .leading, spacing: 8) {
                Text("操作者标识")
                    .fieldLabel()
                TextField("human", text: $actorID)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, weight: .medium, design: .monospaced))
            }
            HStack {
                Spacer()
                Button("取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("保存") {
                    store.setActor(actorID)
                    if store.errorMessage == nil { dismiss() }
                }
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(width: 430)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }
}

private struct SheetTitle: View {
    let icon: String
    let title: String
    let subtitle: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(DesignTokens.ink)
                .frame(width: 42, height: 42)
                .background(DesignTokens.selection, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(subtitle)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
    }
}

private struct LabeledField: View {
    let label: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label)
                .fieldLabel()
            TextField(placeholder, text: $text)
                .textFieldStyle(.roundedBorder)
        }
    }
}

private struct InlineValidation: View {
    let message: String

    var body: some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(DesignTokens.danger)
    }
}

private struct InlineResult: View {
    let message: String
    let allocated: Bool

    var body: some View {
        Label(message, systemImage: allocated ? "checkmark.circle.fill" : "hourglass")
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(allocated ? DesignTokens.success : DesignTokens.warning)
            .fixedSize(horizontal: false, vertical: true)
    }
}
