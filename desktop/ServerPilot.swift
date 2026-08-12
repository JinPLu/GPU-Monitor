import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

private enum DesktopError: LocalizedError {
    case projectRootMissing
    case brokerExecutableMissing
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .projectRootMissing:
            return "应用内置运行资源不完整。请重新安装 ServerPilot.app，或为开发构建设置 SERVERPILOT_ROOT。"
        case .brokerExecutableMissing:
            return "应用内置后台服务不完整。请重新安装 ServerPilot.app，或为开发构建设置 SERVERPILOT_CLI。"
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
        if let configured = ProcessInfo.processInfo.environment["SERVERPILOT_ROOT"], !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        if let bundledRoot = Bundle.main.resourceURL?
            .appendingPathComponent("ServerPilotRuntime", isDirectory: true),
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
        NSApp.appearance = NSAppearance(named: .aqua)
        configureMainMenu()

        let visibleSize = NSScreen.main?.visibleFrame.size ?? NSSize(width: 1440, height: 820)
        var initialSize = NSSize(
            width: max(1024, min(1440, visibleSize.width - 48)),
            height: max(640, min(820, visibleSize.height - 48))
        )
#if DEBUG || DESKTOP_FIXTURES
        if let fixtureViewport = fixtureViewportIfRequested() {
            initialSize = fixtureViewport
        }
#endif
        let contentRect = NSRect(origin: .zero, size: initialSize)
        let createdWindow = NSWindow(
            contentRect: contentRect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        createdWindow.title = "ServerPilot"
        createdWindow.titleVisibility = .hidden
        createdWindow.titlebarAppearsTransparent = true
        createdWindow.toolbarStyle = .unifiedCompact
        createdWindow.titlebarSeparatorStyle = .none
        createdWindow.backgroundColor = .windowBackgroundColor
        createdWindow.isOpaque = true
        createdWindow.minSize = NSSize(width: 900, height: 640)
        createdWindow.center()
        createdWindow.delegate = self

        let view = NSHostingView(rootView: NativeBrokerRoot(store: brokerStore))
        view.frame = contentRect
        view.autoresizingMask = [.width, .height]
        createdWindow.contentView = view
        window = createdWindow
        createdWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
#if DEBUG || DESKTOP_FIXTURES
        if configureFixtureModeIfRequested() {
            captureFixtureScreenshotIfRequested(from: view)
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
        appMenu.addItem(withTitle: "退出 ServerPilot", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
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
            environment["SERVERPILOT_CLI"],
            Bundle.main.resourceURL?
                .appendingPathComponent("ServerPilotRuntime/serverpilot")
                .path,
            "\(home)/.local/share/uv/tools/serverpilot/bin/serverpilot",
            "/opt/homebrew/bin/serverpilot",
            "/usr/local/bin/serverpilot"
        ].compactMap { $0 }
        return candidates
            .map { URL(fileURLWithPath: $0) }
            .first(where: { FileManager.default.isExecutableFile(atPath: $0.path) })
    }

    private func processEnvironment(broker: URL) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if let root = projectRoot {
            environment["SERVERPILOT_PROJECT_ROOT"] = root.path
        }
        environment["SERVERPILOT_DAEMON_EXECUTABLE"] = broker.path
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
                    self.showFatalError("本机 ServerPilot 服务未能在规定时间内启动。请检查项目依赖和 state 目录。")
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
                completion(.incompatible("127.0.0.1:\(port) 上有服务响应，但它不是当前 ServerPilot 服务。桌面应用不会关闭或替换这个外部服务。"))
                return
            }
            // A healthy but stale daemon must go through ensureDaemon so the
            // owned LaunchAgent can be restarted onto this app's runtime.
            // Do not surface a false "incompatible service" dialog for a
            // ServerPilot process that simply predates the current runtime
            // capability floor.
            guard info.schemaVersion == "v1", info.capabilities.contains("instant_claims") else {
                completion(.incompatible("127.0.0.1:\(port) 上有服务响应，但它不是当前 ServerPilot 服务。桌面应用不会关闭或替换这个外部服务。"))
                return
            }
            guard info.capabilities.contains("endpoint_conflict_cleanup") else {
                completion(.unavailable)
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

#if DEBUG || DESKTOP_FIXTURES
    private func fixtureViewportIfRequested() -> NSSize? {
        guard let rawValue = ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_VIEWPORT"] else {
            return nil
        }
        let components = rawValue.lowercased().split(separator: "x", maxSplits: 1)
        guard
            components.count == 2,
            let width = Double(components[0]),
            let height = Double(components[1]),
            width >= 900,
            height >= 640,
            width <= 1440,
            height <= 820
        else {
            return nil
        }
        return NSSize(width: width, height: height)
    }

    private func configureFixtureModeIfRequested() -> Bool {
        let environment = ProcessInfo.processInfo.environment
        guard let fixture = environment["SERVERPILOT_DESKTOP_FIXTURE"], !fixture.isEmpty else {
            return false
        }
        do {
            let fixturesRoot = desktopFixturesRoot()
            let fixtureURL = try FixtureSnapshots.resolve(
                fixture,
                fixturesRoot: fixturesRoot,
                projectRoot: projectRoot
            )
            let snapshot = try FixtureSnapshots.load(from: fixtureURL)
            if let historyFixture = environment["SERVERPILOT_DESKTOP_HISTORY_FIXTURE"], !historyFixture.isEmpty {
                let historyURL = try FixtureSnapshots.resolve(
                    historyFixture,
                    fixturesRoot: fixturesRoot,
                    projectRoot: projectRoot
                )
                let history = try FixtureSnapshots.loadEndpointTelemetryHistory(from: historyURL)
                guard snapshot.endpoints.contains(where: { $0.id == history.endpointID }) else {
                    throw FixtureSnapshotError.invalid(historyURL)
                }
                let serviceInfo = ServiceInfo(
                    schemaVersion: ServiceInfo.fixture.schemaVersion,
                    version: ServiceInfo.fixture.version,
                    capabilities: ServiceInfo.fixture.capabilities.union(["endpoint_telemetry_history"])
                )
                brokerStore.useFixture(
                    snapshot: snapshot,
                    serviceInfo: serviceInfo,
                    endpointTelemetryHistoryClient: FixtureEndpointTelemetryHistoryClient(history: history)
                )
            } else {
                brokerStore.useFixture(snapshot: snapshot)
            }
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

    private func captureFixtureScreenshotIfRequested(from view: NSView) {
        let environment = ProcessInfo.processInfo.environment
        guard let outputPath = environment["SERVERPILOT_DESKTOP_SCREENSHOT"], !outputPath.isEmpty else {
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            let bounds = view.bounds
            guard
                let representation = NSBitmapImageRep(
                    bitmapDataPlanes: nil,
                    pixelsWide: Int(bounds.width),
                    pixelsHigh: Int(bounds.height),
                    bitsPerSample: 8,
                    samplesPerPixel: 4,
                    hasAlpha: true,
                    isPlanar: false,
                    colorSpaceName: .deviceRGB,
                    bytesPerRow: 0,
                    bitsPerPixel: 0
                )
            else {
                return
            }
            representation.size = bounds.size
            view.cacheDisplay(in: bounds, to: representation)
            guard let data = representation.representation(using: .png, properties: [:]) else {
                return
            }
            do {
                try data.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
                if environment["SERVERPILOT_DESKTOP_EXIT_AFTER_SCREENSHOT"] == "1" {
                    NSApp.terminate(nil)
                }
            } catch {
                fputs("Unable to write fixture screenshot: \(error)\n", stderr)
            }
        }
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
        alert.messageText = "无法启动 ServerPilot"
        alert.informativeText = message
        alert.addButton(withTitle: "退出")
        alert.runModal()
        NSApp.terminate(nil)
    }
}

// MARK: - ServerPilot API model

private enum ServiceProbeResult {
    case compatible(ServiceInfo)
    case incompatible(String)
    case unavailable
}

@discardableResult
func confirmLeaseRelease(_ lease: LeaseRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "释放 \(lease.gpuIDs.count) 张 GPU？"
    alert.informativeText = "请先确认任务已结束。释放不会停止任务。"
    alert.addButton(withTitle: "释放")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult
private func confirmKeepaliveEnd() -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "结束占卡？"
    alert.informativeText = "将让出这台服务器上正在空闲占卡的 GPU；正在运行的任务不会被停止。"
    alert.addButton(withTitle: "结束占卡")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult
private func confirmEmptyLeaseCleanup(_ lease: LeaseRecord, conflict: Bool) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = conflict ? "清理这笔遗留归属？" : "释放这笔空闲占用？"
    alert.informativeText = "ServerPilot 会先重新采集这台服务器；只有确认这笔租约覆盖的 GPU 都没有运行中的进程时才会释放。"
    alert.addButton(withTitle: conflict ? "清理遗留归属" : "释放空闲占用")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult
private func confirmEmptyKeepaliveCleanup(gpuCount: Int) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "释放遗留占卡？"
    alert.informativeText = "ServerPilot 会先重新采集这台服务器；只有确认 \(gpuCount) 张占卡 GPU 都没有运行中的进程时才会释放。"
    alert.addButton(withTitle: "释放遗留占卡")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

// MARK: - Apple Home inspired native interface

private struct StableRecordSelection: Identifiable, Equatable {
    let id: String
}

private struct NativeBrokerRoot: View {
    @ObservedObject var store: BrokerStore
    @State private var showAddServer = false
    @State private var showClaim = false
    @State private var claimInitialEndpointID = ""
    @State private var selectedGPUID: String?
    @State private var selectedEndpointDetailID: String?
    @State private var editingEndpointID: String?
    @State private var selectedDashboardSection: DashboardSection

    init(store: BrokerStore) {
        self.store = store
#if DEBUG || DESKTOP_FIXTURES
        let requested = ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_SECTION"]
        let initialSection: DashboardSection = switch requested {
        case "server-pool": .resources
        case "resource-usage", "leases": .leases
        case "settings": .settings
        default: .resources
        }
        _selectedDashboardSection = State(initialValue: initialSection)
#else
        _selectedDashboardSection = State(initialValue: .resources)
#endif
    }

    private var selectedGPUSelection: Binding<StableRecordSelection?> {
        Binding(
            get: { selectedGPUID.map(StableRecordSelection.init(id:)) },
            set: { selectedGPUID = $0?.id }
        )
    }

    private var selectedEndpointSelection: Binding<StableRecordSelection?> {
        Binding(
            get: { selectedEndpointDetailID.map(StableRecordSelection.init(id:)) },
            set: { selectedEndpointDetailID = $0?.id }
        )
    }

    private var editingEndpointSelection: Binding<StableRecordSelection?> {
        Binding(
            get: { editingEndpointID.map(StableRecordSelection.init(id:)) },
            set: { editingEndpointID = $0?.id }
        )
    }

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
                                claimInitialEndpointID = ""
                                showClaim = true
                            },
                            refresh: store.reload
                        )
                        .fixedSize(horizontal: false, vertical: true)
                        DashboardView(
                            store: store,
                            addServer: { showAddServer = true },
                            claimGPU: {
                                claimInitialEndpointID = ""
                                showClaim = true
                            },
                            claimEndpoint: { endpointID in
                                claimInitialEndpointID = endpointID
                                showClaim = true
                            },
                            openEndpoint: { endpoint in
                                selectedEndpointDetailID = endpoint.id
                            },
                            editEndpoint: { endpoint in
                                editingEndpointID = endpoint.id
                            },
                            pauseEndpoint: { endpoint in
                                store.pauseEndpoint(endpoint) { _, _ in }
                            },
                            resumeEndpoint: { endpoint in
                                store.resumeEndpoint(endpoint) { _, _ in }
                            },
                            setKeepalive: { endpoint, enabled in
                                store.setEndpointKeepalive(endpoint, enabled: enabled) { _, _ in }
                            },
                            selectedSection: $selectedDashboardSection,
                            selectGPU: { gpu in
                                selectedGPUID = gpu.id
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
                .background(DesignTokens.ambientSmoke)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .tint(DesignTokens.interaction)
        .sheet(isPresented: $showAddServer) {
            AddServerSheet(store: store)
        }
        .sheet(isPresented: $showClaim) {
            ClaimSheet(store: store, initialEndpointID: claimInitialEndpointID)
        }
        .sheet(item: selectedEndpointSelection) { selection in
            ServerDetailSheet(
                store: store,
                endpointID: selection.id,
                claim: {
                    selectedEndpointDetailID = nil
                    claimInitialEndpointID = selection.id
                    showClaim = true
                },
                edit: {
                    selectedEndpointDetailID = nil
                    DispatchQueue.main.async {
                        editingEndpointID = selection.id
                    }
                }
            )
        }
        .sheet(item: editingEndpointSelection) { selection in
            if let endpoint = store.snapshot.endpoint(id: selection.id) {
                EditServerSheet(store: store, endpoint: endpoint)
            }
        }
        .sheet(item: selectedGPUSelection) { selection in
            if let gpu = store.snapshot.gpu(id: selection.id) {
                GPUDetailSheet(gpu: gpu)
            }
        }
        .onChange(of: store.snapshot.endpoints.map(\.id)) { _, endpointIDs in
            if let selectedEndpointDetailID, !endpointIDs.contains(selectedEndpointDetailID) {
                self.selectedEndpointDetailID = nil
            }
        }
        .onChange(of: store.snapshot.gpus.map(\.id)) { _, gpuIDs in
            if let selectedGPUID, !gpuIDs.contains(selectedGPUID) {
                self.selectedGPUID = nil
            }
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
                    Image(systemName: "server.rack")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(DesignTokens.onInteraction)
                }
                .frame(width: 36, height: 36)
                if !compact {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("ServerPilot")
                            .font(.system(size: 16, weight: .bold))
                            .foregroundStyle(DesignTokens.ink)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: compact ? .center : .leading)
            .padding(.horizontal, compact ? 10 : 18)
            .padding(.top, 30)
            .padding(.bottom, 25)

            SidebarSelection(title: "服务器", systemImage: "server.rack", color: DesignTokens.interaction, selected: selectedSection == .resources, compact: compact) {
                navigate(.resources)
            }
            SidebarSelection(title: "使用情况", systemImage: "chart.bar.xaxis", color: DesignTokens.interaction, selected: selectedSection == .leases, compact: compact) {
                navigate(.leases)
            }
            SidebarSelection(title: "设置", systemImage: "gearshape.fill", color: DesignTokens.interaction, selected: selectedSection == .settings, compact: compact) {
                navigate(.settings)
            }

            Spacer(minLength: 22)

            if !store.isConnected, let error = store.errorMessage {
                VStack(alignment: compact ? .center : .leading, spacing: 6) {
                HStack(spacing: 7) {
                    Circle()
                        .fill(DesignTokens.danger)
                        .frame(width: 7, height: 7)
                    if !compact {
                        Text(error)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(DesignTokens.ink)
                            .lineLimit(2)
                    }
                }
                }
                .padding(.horizontal, compact ? 10 : 18)
                .padding(.vertical, 16)
                .overlay(alignment: .top) {
                    Divider().padding(.horizontal, 18)
                }
            }
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(DesignTokens.surface)
    }
}

private struct SidebarSelection: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let title: String
    let systemImage: String
    let color: Color
    let selected: Bool
    let compact: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 11) {
                Image(systemName: systemImage)
                    .font(.system(size: 14, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(selected ? color : DesignTokens.mutedInk)
                    .frame(width: 18)
                if !compact {
                    Text(title)
                        .font(.system(size: 13, weight: selected ? .semibold : .medium))
                        .foregroundStyle(DesignTokens.ink)
                    Spacer()
                }
            }
            .padding(.horizontal, compact ? 0 : 15)
            .frame(height: 38)
            .frame(maxWidth: .infinity)
            .background(
                selected ? color.opacity(0.14) : DesignTokens.ink.opacity(hovering ? 0.045 : 0),
                in: RoundedRectangle(cornerRadius: 9, style: .continuous)
            )
        }
        .buttonStyle(.plain)
        .focusable()
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
                    Button(action: addServer) {
                        Label("添加服务器", systemImage: "plus")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .buttonStyle(SecondaryActionButtonStyle())
                    .focusable()
                    .disabled(!store.allowsMutations)
                    .help(store.allowsMutations ? "添加服务器到本机资源池" : store.mutationUnavailableReason)
                    .accessibilityLabel("添加服务器")
                    Button(action: claimGPU) {
                        Label("申请 GPU", systemImage: "key.fill")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .buttonStyle(PrimaryActionButtonStyle())
                    .focusable()
                    .disabled(!store.allowsMutations || store.snapshot.operationalEndpoints.isEmpty)
                    .help(!store.allowsMutations ? store.mutationUnavailableReason : (store.snapshot.operationalEndpoints.isEmpty ? "请先添加服务器" : "申请空闲 GPU"))
                    .accessibilityLabel("申请 GPU")
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
                    .focusable()
                    .disabled(store.isRefreshing || !store.canRefresh)
                    .keyboardShortcut("r", modifiers: [.command])
                    .help(store.canRefresh ? "更新资源数据" : "测试数据不能刷新")
                    .accessibilityLabel("更新资源数据")
                    .accessibilityValue(store.isRefreshing ? "正在更新" : (store.canRefresh ? "可以更新" : "测试数据"))
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
        .background(DesignTokens.surface)
    }

    private var statusText: String {
        if store.errorMessage != nil, store.lastGoodSnapshot != nil {
            return "连接已中断 · 显示上次数据"
        }
        if store.snapshot.snapshotRevision != nil {
            return "更新于 \(lastUpdatedText)"
        }
        return store.isConnected ? "正在读取资源" : "正在连接"
    }

    private var lastSuccessText: String {
        guard let lastUpdated = store.lastUpdated else { return "等待首次更新" }
        let elapsed = max(0, Int(Date().timeIntervalSince(lastUpdated)))
        return elapsed < 5 ? "刚刚" : "\(elapsed) 秒前"
    }

    private var lastUpdatedText: String {
        guard let lastUpdated = store.lastUpdated else { return "—" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M 月 d 日 HH:mm"
        return formatter.string(from: lastUpdated)
    }

    private var title: String {
        switch selectedSection {
        case .resources: return "服务器"
        case .leases: return "使用情况"
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
        .background(DesignTokens.glassSmoke, in: Capsule())
        .overlay(Capsule().stroke(DesignTokens.surfaceStroke, lineWidth: 1))
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
    let editEndpoint: (EndpointRecord) -> Void
    let pauseEndpoint: (EndpointRecord) -> Void
    let resumeEndpoint: (EndpointRecord) -> Void
    let setKeepalive: (EndpointRecord, Bool) -> Void
    @Binding var selectedSection: DashboardSection
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        VStack(spacing: 0) {
            if let error = store.errorMessage {
                NoticeBanner(message: error, color: DesignTokens.danger, icon: "exclamationmark.triangle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if store.freshness == .stale {
                NoticeBanner(message: "连接已中断，显示上次数据。", color: DesignTokens.danger, icon: "wifi.exclamationmark")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if let notice = displayedNotice {
                NoticeBanner(message: notice, color: DesignTokens.success, icon: "checkmark.circle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            }

            Group {
                switch selectedSection {
                case .resources:
                    ResourcesDashboard(
                        store: store,
                        claimEndpoint: claimEndpoint,
                        openEndpoint: openEndpoint,
                        editEndpoint: editEndpoint,
                        pauseEndpoint: pauseEndpoint,
                        resumeEndpoint: resumeEndpoint,
                        setKeepalive: setKeepalive,
                        selectGPU: selectGPU
                    )
                case .leases:
                    ResourceUsageDashboard(store: store, claimGPU: claimGPU)
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

    private var displayedNotice: String? {
        guard let notice = store.notice,
              notice.hasPrefix("已申领，待使用：")
        else { return store.notice }

        guard let runningLease = store.snapshot.leases.first(where: { lease in
            guard notice.contains(lease.id) else { return false }
            if lease.runtimeState == "RUNNING" { return true }
            return store.snapshot.resourceClaims.contains { claim in
                claim.nativeLeaseIDs.contains(lease.id)
                    && (claim.runtimeState == "RUNNING" || claim.state == "RUNNING")
            }
        }) else { return notice }

        let task = runningLease.taskReference ?? runningLease.purpose ?? "未命名任务"
        return "任务使用中：\(runningLease.projectID) · \(task) · \(runningLease.gpuIDs.count) GPU。"
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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HomeSectionTitle(title: "设置")
                VStack(alignment: .leading, spacing: 12) {
                    SettingsFact(label: "本机服务地址", value: store.serviceAddress, icon: "network")
                    if store.supportsCollectorSettings {
                        HStack(spacing: 10) {
                            Image(systemName: "clock.arrow.circlepath")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(DesignTokens.interaction)
                                .frame(width: 28, height: 28)
                                .background(DesignTokens.interaction.opacity(0.11), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                            Text("数据更新间隔")
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(DesignTokens.mutedInk)
                            Spacer()
                            Picker(
                                "数据更新间隔",
                                selection: Binding(
                                    get: { store.collectorSettings?.intervalSeconds ?? 10 },
                                    set: { store.updateCollectorInterval($0) { _, _ in } }
                                )
                            ) {
                                ForEach(store.collectorSettings?.allowedIntervals ?? [5, 10, 30], id: \.self) { seconds in
                                    Text("\(seconds) 秒").tag(seconds)
                                }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 210)
                            .accessibilityLabel("数据更新间隔")
                            .accessibilityValue("\(store.collectorSettings?.intervalSeconds ?? 10) 秒")
                            .disabled(
                                store.collectorSettingsLoading
                                    || store.collectorSettings == nil
                                    || !store.canUpdateCollectorSettings
                            )
                        }
                    }
                    SettingsFact(label: "版本", value: store.serviceInfo?.version ?? "未知", icon: "number")
                }
                .padding(16)
                .background(DesignTokens.surface.opacity(0.72), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
            }
            .padding(.horizontal, 24)
            .padding(.top, 8)
            .padding(.bottom, 18)
            .frame(maxWidth: 640, alignment: .leading)
        }
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

private enum EndpointFilter: String, CaseIterable, Identifiable {
    case all
    case available
    case taskOccupied
    case keepalive
    case connectionFailed

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "全部"
        case .available: return "有空闲 GPU"
        case .taskOccupied: return "任务占用"
        case .keepalive: return "占卡"
        case .connectionFailed: return "连接失败"
        }
    }
}

private enum EndpointSort: String, CaseIterable, Identifiable {
    case attention
    case id
    case assignment
    case availableGPU
    case gpuModel
    case gpuUtilization
    case gpuMemory
    case cpuLoad
    case memory

    var id: String { rawValue }

    var label: String {
        switch self {
        case .attention: return "连接状态"
        case .id: return "SSH 连接"
        case .assignment: return "项目 / 任务"
        case .availableGPU: return "空闲 GPU"
        case .gpuModel: return "GPU 配置"
        case .gpuUtilization: return "GPU 利用"
        case .gpuMemory: return "显存占用"
        case .cpuLoad: return "CPU 负载"
        case .memory: return "内存占用"
        }
    }

    var defaultDirection: EndpointSortDirection {
        switch self {
        case .id, .assignment, .gpuModel: return .ascending
        default: return .descending
        }
    }
}

private enum EndpointSortDirection: Equatable {
    case ascending
    case descending
}

private struct ResourcesDashboard: View {
    @ObservedObject var store: BrokerStore
    @State private var searchText = ""
    @State private var filter: EndpointFilter = .all
    @State private var sort: EndpointSort = .id
    @State private var sortDirection: EndpointSortDirection = .ascending
    @State private var endpointTableWidth: CGFloat = 1_200
    let claimEndpoint: (String) -> Void
    let openEndpoint: (EndpointRecord) -> Void
    let editEndpoint: (EndpointRecord) -> Void
    let pauseEndpoint: (EndpointRecord) -> Void
    let resumeEndpoint: (EndpointRecord) -> Void
    let setKeepalive: (EndpointRecord, Bool) -> Void
    let selectGPU: (GPURecord) -> Void

    private var endpoints: [EndpointRecord] { store.snapshot.operationalEndpoints }

    private var onlineEndpointCount: Int {
        endpoints.filter { $0.monitorStatus == "ONLINE" }.count
    }

    private var allocatableGPUCount: Int {
        guard store.freshness == .fresh else { return 0 }
        return endpoints
            .filter { $0.monitorStatus == "ONLINE" }
            .flatMap { store.snapshot.gpus(for: $0) }
            .filter(\.isPubliclyAvailable)
            .count
    }

    private var attentionEndpoints: [EndpointRecord] {
        endpoints.filter { endpointRequiresAttention(endpoint: $0, gpus: store.snapshot.gpus(for: $0)) }
    }

    private var attentionGPUCount: Int {
        store.snapshot.operationalGPUs.filter { gpuNeedsAttention($0) }.count
    }

    private var filteredEndpoints: [EndpointRecord] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return endpoints
            .filter { endpoint in
                switch filter {
                case .all:
                    true
                case .available:
                    store.freshness == .fresh
                        && endpoint.monitorStatus == "ONLINE"
                        && store.snapshot.gpus(for: endpoint).contains(where: \.isPubliclyAvailable)
                case .taskOccupied:
                    store.snapshot.gpus(for: endpoint).contains {
                        ["BUSY_UNMANAGED", "ORPHANED_BUSY", "CONFLICT"].contains($0.state)
                    }
                case .keepalive:
                    endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning
                case .connectionFailed:
                    endpoint.monitorStatus != "ONLINE"
                }
            }
            .filter { endpoint in
                guard !query.isEmpty else { return true }
                let endpointLeases = leases(for: endpoint)
                return endpoint.id.lowercased().contains(query)
                    || endpoint.displayName.lowercased().contains(query)
                    || endpoint.host.lowercased().contains(query)
                    || endpoint.sshCommand.lowercased().contains(query)
                    || (endpoint.workspacePath?.lowercased().contains(query) ?? false)
                    || store.snapshot.gpus(for: endpoint).contains { $0.name.lowercased().contains(query) }
                    || endpointLeases.contains {
                        $0.projectID.lowercased().contains(query)
                            || ($0.taskReference ?? "").lowercased().contains(query)
                            || ($0.purpose ?? "").lowercased().contains(query)
                    }
            }
            .sorted(by: endpointSort)
    }

    var body: some View {
        GeometryReader { proxy in
            VStack(spacing: 0) {
                resourceSummary
                Divider().opacity(0.45)
                endpointTable
                    .background(DesignTokens.surface)
            }
            .onAppear { endpointTableWidth = proxy.size.width }
            .onChange(of: proxy.size.width) { _, width in endpointTableWidth = width }
        }
        .onChange(of: sort) { _, newSort in
            sortDirection = newSort.defaultDirection
        }
        .accessibilityLabel("服务器")
    }

    private var resourceSummary: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 16) {
                summaryStatus
                Divider().frame(height: 24)
                gpuInventorySummary
                Spacer(minLength: 12)
                if store.freshness != .fresh { snapshotTrustSummary }
            }
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    summaryStatus
                    Spacer(minLength: 8)
                    if store.freshness != .fresh { snapshotTrustSummary }
                }
                gpuInventorySummary
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(DesignTokens.surface)
    }

    private var summaryStatus: some View {
        HStack(spacing: 14) {
            ResourceInlineStat(value: "\(endpoints.count)", label: "台服务器", color: DesignTokens.ink)
            ResourceInlineStat(value: "\(store.snapshot.operationalGPUs.count)", label: "张 GPU", color: DesignTokens.ink)
            ResourceInlineStat(value: "\(allocatableGPUCount)", label: "张空闲", color: DesignTokens.success)
        }
    }

    private var gpuInventorySummary: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("GPU 型号")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(DesignTokens.mutedInk)
            Text(fleetGPUModelSummary)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.tail)
                .help(fleetGPUModelSummary)
        }
    }

    private var snapshotTrustSummary: some View {
        Label(snapshotTrustLabel, systemImage: store.freshness == .fresh ? "checkmark.circle.fill" : "hand.raised.fill")
            .font(.system(size: 10, weight: .medium, design: .rounded))
            .foregroundStyle(store.freshness == .fresh ? DesignTokens.mutedInk : DesignTokens.danger)
            .lineLimit(1)
            .help(attentionSummary)
    }

    private var endpointTable: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                TextField("搜索 SSH、GPU、项目或任务", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12, weight: .medium))
                    .frame(maxWidth: 320)
                    .accessibilityLabel("搜索端点")
                Picker("过滤", selection: $filter) {
                    ForEach(EndpointFilter.allCases) { item in
                        Text(item.label).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 430)
                .accessibilityLabel("端点过滤")

                Spacer(minLength: 0)

                Menu {
                    Picker("排序", selection: $sort) {
                        ForEach(EndpointSort.allCases) { item in
                            Text(item.label).tag(item)
                        }
                    }
                } label: {
                    Label("排序", systemImage: "arrow.up.arrow.down")
                        .font(.system(size: 11, weight: .semibold))
                }
                .menuStyle(.borderlessButton)
                .help("资源排序")
                .accessibilityLabel("资源排序")
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 11)

            VStack(spacing: 0) {
                EndpointTableHeader(
                    sort: sort,
                    direction: sortDirection,
                    compactLayout: endpointTableWidth < 1_040,
                    selectSort: selectSort
                )
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(DesignTokens.glassSmoke)
                    .overlay(alignment: .bottom) {
                        Rectangle()
                            .fill(DesignTokens.surfaceStroke)
                            .frame(height: 1.5)
                    }

                if filteredEndpoints.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: endpoints.isEmpty ? "server.rack" : "magnifyingglass")
                            .font(.system(size: 24, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                        Text(endpoints.isEmpty ? "暂无端点" : "没有匹配端点")
                            .font(.system(size: 14, weight: .semibold))
                        Text(endpoints.isEmpty ? "添加服务器后会显示资源。" : "调整搜索或过滤条件。")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityElement(children: .combine)
                } else {
                    ScrollView {
                        VStack(spacing: 0) {
                            ForEach(filteredEndpoints) { endpoint in
                                EndpointTableRow(
                                    endpoint: endpoint,
                                    gpus: store.snapshot.gpus(for: endpoint),
                                    leases: leases(for: endpoint),
                                    isSnapshotFresh: store.freshness == .fresh,
                                    compactLayout: endpointTableWidth < 1_040,
                                    selected: false
                                ) {
                                    openEndpoint(endpoint)
                                }
                                Divider()
                            }
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            .background(DesignTokens.surface)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(DesignTokens.surfaceStroke, lineWidth: 1)
            )
            .accessibilityElement(children: .contain)
            .accessibilityLabel("服务器列表")
            .accessibilityValue(endpointTableAccessibilityValue)
            .padding(.horizontal, 20)
            .padding(.bottom, 12)

        }
    }

    private var endpointTableAccessibilityValue: String {
        if filteredEndpoints.isEmpty {
            return endpoints.isEmpty ? "暂无端点。添加服务器后会显示资源。" : "没有匹配端点。"
        }
        return filteredEndpoints.map { endpoint in
            "\(endpoint.sshCommand)：\(endpoint.monitorDetail ?? endpoint.monitorLabel)"
        }.joined(separator: "；")
    }

    private func selectSort(_ newSort: EndpointSort) {
        if sort == newSort {
            sortDirection = sortDirection == .ascending ? .descending : .ascending
        } else {
            sortDirection = newSort.defaultDirection
            sort = newSort
        }
    }

    private func leases(for endpoint: EndpointRecord) -> [LeaseRecord] {
        let gpuIDs = Set(store.snapshot.gpus(for: endpoint).map(\.id))
        guard !gpuIDs.isEmpty else { return [] }
        return store.snapshot.leases.filter { lease in
            !gpuIDs.isDisjoint(with: lease.gpuIDs)
                && !["RELEASED", "EXPIRED", "CANCELLED"].contains(lease.state)
        }
    }

    private func endpointSort(_ lhs: EndpointRecord, _ rhs: EndpointRecord) -> Bool {
        let comparison: ComparisonResult
        switch sort {
        case .attention:
            comparison = compare(endpointAttentionRank(lhs), endpointAttentionRank(rhs))
        case .id:
            comparison = lhs.sshCommand.localizedStandardCompare(rhs.sshCommand)
        case .assignment:
            comparison = assignmentSortLabel(lhs).localizedStandardCompare(assignmentSortLabel(rhs))
        case .availableGPU:
            comparison = compare(availableGPUCount(lhs), availableGPUCount(rhs))
        case .gpuModel:
            let left = endpointGPUModelSummary(store.snapshot.gpus(for: lhs))
            let right = endpointGPUModelSummary(store.snapshot.gpus(for: rhs))
            comparison = left.localizedStandardCompare(right)
        case .gpuUtilization:
            let left = endpointAverageUtilizationFraction(endpoint: lhs, gpus: store.snapshot.gpus(for: lhs)) ?? -1
            let right = endpointAverageUtilizationFraction(endpoint: rhs, gpus: store.snapshot.gpus(for: rhs)) ?? -1
            comparison = compare(left, right)
        case .gpuMemory:
            let left = endpointAverageMemoryFraction(endpoint: lhs, gpus: store.snapshot.gpus(for: lhs)) ?? -1
            let right = endpointAverageMemoryFraction(endpoint: rhs, gpus: store.snapshot.gpus(for: rhs)) ?? -1
            comparison = compare(left, right)
        case .cpuLoad:
            comparison = compare(lhs.cpuLoadFraction ?? -1, rhs.cpuLoadFraction ?? -1)
        case .memory:
            comparison = compare(lhs.memoryFraction ?? -1, rhs.memoryFraction ?? -1)
        }
        if comparison == .orderedSame {
            return lhs.id.localizedStandardCompare(rhs.id) == .orderedAscending
        }
        return sortDirection == .ascending ? comparison == .orderedAscending : comparison == .orderedDescending
    }

    private func compare<T: Comparable>(_ lhs: T, _ rhs: T) -> ComparisonResult {
        if lhs < rhs { return .orderedAscending }
        if lhs > rhs { return .orderedDescending }
        return .orderedSame
    }

    private func assignmentSortLabel(_ endpoint: EndpointRecord) -> String {
        let endpointLeases = leases(for: endpoint)
        guard let lease = endpointLeases.first(where: { $0.runtimeState == "RUNNING" }) ?? endpointLeases.first else {
            return ""
        }
        return "\(lease.projectID) \(lease.taskReference ?? lease.purpose ?? "")"
    }

    private func endpointAttentionRank(_ endpoint: EndpointRecord) -> Int {
        let endpointGPUs = store.snapshot.gpus(for: endpoint)
        let gpuRank = endpointGPUs.contains { gpuNeedsAttention($0) } ? 2 : 0
        let pressureRank = endpointHighPressure(endpoint: endpoint, gpus: endpointGPUs) ? 1 : 0
        return (endpointNeedsAttention(endpoint) ? 3 : 0) + gpuRank + pressureRank
    }

    private func availableGPUCount(_ endpoint: EndpointRecord) -> Int {
        guard store.freshness == .fresh, endpoint.monitorStatus == "ONLINE" else { return 0 }
        return store.snapshot.gpus(for: endpoint).filter(\.isPubliclyAvailable).count
    }

    private var allocatableGPUSummary: String {
        guard store.freshness == .fresh else { return "未确认" }
        return "\(allocatableGPUCount)/\(store.snapshot.operationalGPUs.count)"
    }

    private var fleetGPUModelSummary: String {
        let groups = Dictionary(grouping: store.snapshot.operationalGPUs, by: \.name)
        guard !groups.isEmpty else { return "未检测到 GPU" }
        let labels = groups.keys.sorted().map { name in
            "\(name) × \(groups[name]?.count ?? 0)"
        }
        if labels.count <= 3 { return labels.joined(separator: " · ") }
        return labels.prefix(3).joined(separator: " · ") + " · 另 \(labels.count - 3) 类"
    }

    private var snapshotTrustLabel: String {
        if store.freshness == .stale { return "连接已中断" }
        if store.freshness == .failed { return "暂无数据" }
        if store.snapshot.snapshotRevision != nil { return "数据已同步" }
        return "正在连接"
    }

    private var attentionSummary: String {
        if store.freshness != .fresh {
            return "当前显示上次数据。"
        }
        let attentionPrefix: String
        switch (attentionEndpoints.count, attentionGPUCount) {
        case (0, 0):
            attentionPrefix = "当前没有需要处理的资源"
        case (0, let gpuCount):
            attentionPrefix = "\(gpuCount) 块 GPU 需要处理"
        case (let endpointCount, 0):
            attentionPrefix = "\(endpointCount) 个端点需要处理"
        case (let endpointCount, let gpuCount):
            attentionPrefix = "\(endpointCount) 个端点、\(gpuCount) 块 GPU 需要处理"
        }
        return attentionPrefix
    }
}

private struct ResourceInlineStat: View {
    let value: String
    let label: String
    let color: Color

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
            Text(value)
                .font(.system(size: 14, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }
}

private struct EndpointTableHeader: View {
    let sort: EndpointSort
    let direction: EndpointSortDirection
    let compactLayout: Bool
    let selectSort: (EndpointSort) -> Void

    var body: some View {
        Group {
            if compactLayout {
                compactHeader
            } else {
                standardHeader
            }
        }
        .font(.system(size: 12, weight: .semibold))
    }

    private var standardHeader: some View {
        HStack(spacing: 10) {
                header("服务器", icon: "terminal", column: .id, alignment: .leading)
                    .frame(width: 190, alignment: .leading)
                header("项目 · 任务", icon: "folder", column: .assignment, alignment: .leading)
                    .frame(minWidth: 180, maxWidth: .infinity, alignment: .leading)
                header("GPU 配置", icon: "square.stack.3d.up", column: .gpuModel, alignment: .leading)
                    .frame(width: 140, alignment: .leading)
                header("空闲", accessibilityTitle: "空闲 GPU", icon: "checkmark.circle", column: .availableGPU, alignment: .trailing)
                    .frame(width: 90)
                header("GPU", accessibilityTitle: "GPU 利用", icon: "chart.bar.fill", column: .gpuUtilization, alignment: .trailing)
                    .frame(width: 68)
                header("显存", accessibilityTitle: "显存占用", icon: "memorychip", column: .gpuMemory, alignment: .trailing)
                    .frame(width: 68)
                header("CPU", accessibilityTitle: "CPU 负载", icon: "cpu", column: .cpuLoad, alignment: .trailing)
                    .frame(width: 68)
                header("内存", accessibilityTitle: "内存占用", icon: "memorychip.fill", column: .memory, alignment: .trailing)
                    .frame(width: 68)
                Color.clear.frame(width: 10, height: 10)
        }
        .frame(minWidth: 934, alignment: .leading)
    }

    private var compactHeader: some View {
        VStack(alignment: .leading, spacing: 7) {
            header("服务器", icon: "terminal", column: .id, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 8) {
                header("项目 · 任务", icon: "folder", column: .assignment, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("GPU 配置", icon: "square.stack.3d.up", column: .gpuModel, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("空闲", accessibilityTitle: "空闲 GPU", icon: "checkmark.circle", column: .availableGPU, alignment: .trailing)
                    .frame(width: 76, alignment: .trailing)
            }
            HStack(spacing: 12) {
                header("GPU 利用", icon: "chart.bar.fill", column: .gpuUtilization, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("显存", accessibilityTitle: "显存占用", icon: "memorychip", column: .gpuMemory, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("CPU", accessibilityTitle: "CPU 负载", icon: "cpu", column: .cpuLoad, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("内存", accessibilityTitle: "内存占用", icon: "memorychip.fill", column: .memory, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func header(
        _ title: String,
        accessibilityTitle: String? = nil,
        icon: String? = nil,
        column: EndpointSort,
        alignment: Alignment
    ) -> some View {
        TableColumnHeader(
            title: title,
            accessibilityTitle: accessibilityTitle ?? title,
            systemImage: icon,
            alignment: alignment,
            active: sort == column,
            direction: direction,
            action: { selectSort(column) }
        )
    }
}

private struct TableColumnHeader: View {
    let title: String
    let accessibilityTitle: String
    let systemImage: String?
    let alignment: Alignment
    let active: Bool
    let direction: EndpointSortDirection
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                if let systemImage {
                    Image(systemName: systemImage)
                        .font(.system(size: 12.5, weight: .medium))
                        .frame(width: 14, height: 14)
                }
                Text(title)
                    .lineLimit(1)
            }
            .padding(.leading, 6)
            .padding(.trailing, 14)
            .frame(height: 36)
            .frame(maxWidth: .infinity, alignment: alignment)
            .background {
                if active {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(DesignTokens.selection.opacity(0.78))
                }
            }
            .overlay(alignment: .trailing) {
                if active {
                    Image(systemName: direction == .ascending ? "arrow.up" : "arrow.down")
                        .font(.system(size: 9.5, weight: .bold))
                        .padding(.trailing, 4)
                }
            }
        }
        .buttonStyle(.plain)
        .focusable()
        .foregroundStyle(active ? DesignTokens.ink : DesignTokens.mutedInk)
        .help("按\(accessibilityTitle)排序")
        .accessibilityLabel("按\(accessibilityTitle)排序")
        .accessibilityValue(active ? (direction == .ascending ? "升序" : "降序") : "未选中")
    }
}

private struct EndpointTableRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let leases: [LeaseRecord]
    let isSnapshotFresh: Bool
    let compactLayout: Bool
    let selected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            Group {
                if compactLayout {
                    compactRow
                } else {
                    standardRow
                        .frame(minWidth: 934, alignment: .leading)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 11)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? DesignTokens.interaction.opacity(0.10) : DesignTokens.ink.opacity(hovering ? 0.035 : 0))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focusable()
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
        .help("\(endpoint.sshCommand)\n\(endpoint.workspacePath ?? "工作区未设置")\n\(assignmentHelp)")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("服务器 \(endpoint.sshCommand)")
        .accessibilityValue("\(assignmentHelp)，GPU 配置 \(gpuModelSummary)，\(gpuCaption)，\(attentionLabel)，CPU 负载 \(percentageLabel(endpoint.cpuLoadFraction))，内存占用率 \(percentageLabel(endpoint.memoryFraction))，GPU 利用率 \(percentageLabel(gpuPressure))")
    }

    private var standardRow: some View {
        HStack(alignment: .center, spacing: 10) {
            endpointTitle
                .frame(width: 190, alignment: .leading)
            assignmentCell.frame(minWidth: 180, maxWidth: .infinity, alignment: .leading)
            gpuModelCell.frame(width: 140, alignment: .leading)
            availabilitySummary
                .frame(width: 90, alignment: .trailing)
            TablePressureCell(label: "GPU 利用率", fraction: gpuPressure).frame(width: 68)
            TablePressureCell(label: "显存占用率", fraction: gpuMemoryPressure).frame(width: 68)
            TablePressureCell(label: "CPU 负载", fraction: endpoint.cpuLoadFraction).frame(width: 68)
            TablePressureCell(label: "内存占用率", fraction: endpoint.memoryFraction).frame(width: 68)
            Image(systemName: "chevron.right")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(DesignTokens.mutedInk)
                .frame(width: 10)
                .help("查看详情与历史")
        }
    }

    private var compactRow: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                endpointTitle
                Spacer(minLength: 0)
                Label("详情与历史", systemImage: "chart.xyaxis.line")
                    .font(.system(size: 8, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            HStack(spacing: 8) {
                assignmentCell.frame(maxWidth: .infinity, alignment: .leading)
                gpuModelCell.frame(maxWidth: .infinity, alignment: .leading)
                availabilitySummary
                    .frame(width: 90, alignment: .trailing)
            }
            HStack(spacing: 12) {
                CompactPressureLabel(label: "GPU", fraction: gpuPressure)
                    .frame(maxWidth: .infinity, alignment: .leading)
                CompactPressureLabel(label: "显存", fraction: gpuMemoryPressure)
                    .frame(maxWidth: .infinity, alignment: .leading)
                CompactPressureLabel(label: "CPU", fraction: endpoint.cpuLoadFraction)
                    .frame(maxWidth: .infinity, alignment: .leading)
                CompactPressureLabel(label: "内存", fraction: endpoint.memoryFraction)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var endpointTitle: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 9) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 7, height: 7)
                Text(endpoint.sshCommand)
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }
            Text(endpoint.workspacePath ?? "工作区未设置")
                .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                .foregroundStyle(endpoint.workspacePath == nil ? DesignTokens.warning : DesignTokens.mutedInk)
                .lineLimit(1)
                .truncationMode(.middle)
            if endpoint.monitorStatus != "ONLINE" {
                Text(endpoint.monitorDetail ?? endpoint.monitorLabel)
                    .font(.system(size: 9.5, weight: .medium))
                    .foregroundStyle(statusColor)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
        }
    }

    private var assignmentCell: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(assignmentTitle)
                .font(.system(size: 11.5, weight: .semibold))
                .foregroundStyle(assignmentIsUnassigned ? DesignTokens.mutedInk : DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.tail)
            Text(assignmentDetail)
                .font(.system(size: 9.5, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .help(assignmentHelp)
    }

    private var gpuModelCell: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(gpuModelSummary)
                .font(.system(size: 11.5, weight: .semibold))
                .foregroundStyle(gpus.isEmpty ? DesignTokens.mutedInk : DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.tail)
            Text(gpuCapacitySummary)
                .font(.system(size: 9.5, weight: .medium, design: .rounded))
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
        }
        .help(gpuModelDetail)
    }

    private var statusSummary: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(statusColor)
                .frame(width: 6, height: 6)
            Text(attentionLabel)
                .font(.system(size: 10, weight: .semibold))
                .lineLimit(1)
        }
        .foregroundStyle(statusColor)
        .help(freshnessLabel)
    }

    private var primaryLease: LeaseRecord? {
        leases.first(where: { $0.runtimeState == "RUNNING" }) ?? leases.first
    }

    private var hasUnattributedWorkload: Bool {
        gpus.contains { ["BUSY_UNMANAGED", "CONFLICT", "ORPHANED_BUSY"].contains($0.state) }
    }

    private var assignmentIsUnassigned: Bool {
        primaryLease == nil && !endpoint.keepalive.isActive && !endpoint.keepalive.isTransitioning
    }

    private var assignmentTitle: String {
        if let primaryLease { return primaryLease.projectID }
        if endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning { return "可用 · 空闲占卡" }
        if hasUnattributedWorkload { return "任务占用" }
        if !isSnapshotFresh { return "任务未确认" }
        return "—"
    }

    private var assignmentDetail: String {
        if let primaryLease {
            let task = primaryLease.taskReference ?? primaryLease.purpose ?? "未命名任务"
            let extra = leases.count > 1 ? " · 另 \(leases.count - 1) 项" : ""
            return "\(task)\(extra)"
        }
        if endpoint.keepalive.isActive {
            return "\(gpus.filter { $0.state == "KEEPALIVE" }.count) 张 GPU"
        }
        if hasUnattributedWorkload { return "服务器上检测到任务" }
        if !isSnapshotFresh { return "显示上次数据" }
        return gpus.isEmpty ? "无 GPU 任务" : "暂无运行任务"
    }

    private var assignmentHelp: String {
        if endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning {
            return "\(assignmentTitle) · \(assignmentDetail)"
        }
        guard !leases.isEmpty else { return "\(assignmentTitle) · \(assignmentDetail)" }
        return leases.map { lease in
            let task = lease.taskReference ?? lease.purpose ?? "未命名任务"
            return "\(lease.projectID) · \(task) · \(lease.gpuIDs.count) GPU"
        }.joined(separator: "\n")
    }

    private var availableGPUCount: Int {
        guard isSnapshotFresh, endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter(\.isPubliclyAvailable).count
    }

    private var gpuPressure: Double? {
        endpointAverageUtilizationFraction(endpoint: endpoint, gpus: gpus)
    }

    private var gpuMemoryPressure: Double? {
        endpointAverageMemoryFraction(endpoint: endpoint, gpus: gpus)
    }

    private var gpuCaption: String {
        guard !gpus.isEmpty else { return "无 GPU" }
        return isSnapshotFresh ? "\(availableGPUCount)/\(gpus.count) 空闲" : "状态未确认"
    }

    private var availabilityLabel: String {
        guard !gpus.isEmpty else { return "—" }
        guard isSnapshotFresh, endpoint.monitorStatus == "ONLINE" else { return "未确认" }
        return "\(availableGPUCount)/\(gpus.count)"
    }

    private var conflictedGPUCount: Int {
        gpus.filter { $0.state == "CONFLICT" }.count
    }

    private var availabilitySummary: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(availabilityLabel)
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(availabilityLabel == "—" || availabilityLabel == "未确认" ? DesignTokens.mutedInk : DesignTokens.ink)
            if conflictedGPUCount > 0 {
                Text("\(conflictedGPUCount) 待确认")
                    .font(.system(size: 8.5, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.warning)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("空闲 GPU")
        .accessibilityValue(
            conflictedGPUCount > 0
                ? "\(availabilityLabel)，\(conflictedGPUCount) 张归属待确认"
                : availabilityLabel
        )
    }

    private var gpuModelSummary: String {
        endpointGPUModelSummary(gpus)
    }

    private var gpuModelDetail: String {
        guard !gpus.isEmpty else { return "未检测到 GPU" }
        let groups = Dictionary(grouping: gpus, by: \.name)
        return groups.keys.sorted().map { "\($0) × \(groups[$0]?.count ?? 0)" }.joined(separator: "\n")
    }

    private var gpuCapacitySummary: String {
        guard !gpus.isEmpty else { return "CPU 节点" }
        let capacities = Set(gpus.map { max($0.totalVRAMMiB / 1024, 1) }).sorted()
        let capacity: String
        if capacities.count == 1, let first = capacities.first {
            capacity = "\(first) GB/卡"
        } else if let first = capacities.first, let last = capacities.last {
            capacity = "\(first)–\(last) GB/卡"
        } else {
            capacity = "显存未知"
        }
        return "\(gpus.count) 张 · \(capacity)"
    }

    private var freshnessLabel: String {
        guard isSnapshotFresh else { return "显示上次数据" }
        guard endpoint.monitorStatus == "ONLINE" else { return endpoint.monitorLabel }
        guard let lastSuccess = endpoint.monitorLastSuccessAt else { return "等待监控数据" }
        return "更新 \(formattedTimestamp(lastSuccess))"
    }

    private var attentionLabel: String {
        if !isSnapshotFresh { return "不可分配" }
        if endpointNeedsAttention(endpoint) { return endpoint.monitorLabel }
        let conflictedGPUCount = gpus.filter { $0.state == "CONFLICT" }.count
        if availableGPUCount > 0, conflictedGPUCount > 0 {
            return "\(availableGPUCount) 张可申请 · \(conflictedGPUCount) 张待确认"
        }
        if gpus.contains(where: { ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains($0.state) }) { return "任务占用" }
        if conflictedGPUCount > 0 { return "归属待确认" }
        if gpus.contains(where: gpuNeedsAttention) { return "不可分配" }
        if endpointHighPressure(endpoint: endpoint, gpus: gpus) { return "压力较高" }
        return endpoint.monitorLabel
    }

    private var statusColor: Color {
        if !isSnapshotFresh { return DesignTokens.danger }
        switch endpoint.monitorStatus {
        case "ONLINE":
            return endpointRequiresAttention(endpoint: endpoint, gpus: gpus) ? DesignTokens.warning : DesignTokens.success
        case "PENDING", "DRAINING":
            return DesignTokens.warning
        default:
            return DesignTokens.danger
        }
    }
}

private struct TablePressureCell: View {
    let label: String
    let fraction: Double?

    var body: some View {
        HStack(spacing: 6) {
            Text(percentageLabel(fraction))
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(fraction == nil ? DesignTokens.mutedInk : DesignTokens.ink)
                .lineLimit(1)
                .frame(width: 30, alignment: .trailing)
            PressureMeter(fraction: fraction, color: pressureColor(fraction))
                .frame(width: 40)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(percentageLabel(fraction))
    }
}

private struct CompactPressureLabel: View {
    let label: String
    let fraction: Double?

    var body: some View {
        HStack(spacing: 4) {
            Text(label)
                .foregroundStyle(DesignTokens.mutedInk)
            Text(percentageLabel(fraction))
                .foregroundStyle(fraction == nil ? DesignTokens.mutedInk : DesignTokens.ink)
        }
        .font(.system(size: 9, weight: .semibold, design: .rounded))
    }
}

private struct PressureMeter: View {
    let fraction: Double?
    let color: Color

    private var normalizedFraction: CGFloat {
        CGFloat(min(max(fraction ?? 0, 0), 1))
    }

    var body: some View {
        GeometryReader { proxy in
            Capsule()
                .fill(DesignTokens.ink.opacity(0.075))
                .overlay(alignment: .leading) {
                    Capsule()
                        .fill(color)
                        .frame(width: normalizedFraction > 0 ? max(proxy.size.width * normalizedFraction, 3) : 0)
                }
        }
        .frame(height: 8)
        .accessibilityHidden(true)
    }
}

private struct SpatialServerRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let selected: Bool
    let select: () -> Void

    private var availableCount: Int { gpus.filter(\.isPubliclyAvailable).count }

    var body: some View {
        Button(action: select) {
            HStack(alignment: .top, spacing: 11) {
                Image(systemName: endpoint.monitorStatus == "ONLINE" ? "server.rack" : "exclamationmark.triangle.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(statusColor)
                    .frame(width: 38, height: 38)
                    .background(statusColor.opacity(0.14), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                VStack(alignment: .leading, spacing: 7) {
                    Text(endpoint.sshCommand)
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    HStack(spacing: 6) {
                        ServerResourcePill(icon: "cpu", value: cpuLabel, color: DesignTokens.cpu)
                        ServerResourcePill(icon: "memorychip", value: memoryLabel, color: DesignTokens.memory)
                        ServerResourcePill(
                            icon: "square.stack.3d.up.fill",
                            value: gpus.isEmpty ? "无 GPU" : "\(availableCount)/\(gpus.count)",
                            color: DesignTokens.gpu
                        )
                    }
                }
                Spacer(minLength: 0)
                Text(endpoint.monitorLabel)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(statusColor)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .frame(minHeight: 74)
            .background(
                selected ? DesignTokens.interaction.opacity(0.15) : DesignTokens.ink.opacity(hovering ? 0.045 : 0),
                in: RoundedRectangle(cornerRadius: 11, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(endpoint.displayName)
        .accessibilityValue("\(endpoint.monitorLabel)，CPU \(cpuLabel)，内存 \(memoryLabel)，\(gpus.isEmpty ? "无 GPU" : "空闲 GPU \(availableCount) / \(gpus.count)")")
    }

    private var cpuLabel: String {
        endpoint.cpuCount.map { "\($0) 核" } ?? "—"
    }

    private var memoryLabel: String {
        endpoint.memoryTotalMiB.map { "\($0 / 1024) GB" } ?? "—"
    }

    private var statusColor: Color {
        switch endpoint.monitorStatus {
        case "ONLINE": return DesignTokens.success
        case "PENDING", "DRAINING": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }
}

private struct ServerResourcePill: View {
    let icon: String
    let value: String
    let color: Color

    var body: some View {
        Label(value, systemImage: icon)
            .font(.system(size: 9, weight: .semibold, design: .rounded))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 4)
            .background(color.opacity(0.12), in: Capsule())
    }
}

private struct SpatialMetric: View {
    let icon: String
    let label: String
    let value: String
    let detail: String?
    let fraction: Double?
    let color: Color

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 12, weight: .semibold))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(color)
                .frame(width: 26, height: 26)
                .background(color.opacity(0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(label)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(DesignTokens.mutedInk)
                    Text(value)
                        .font(.system(size: 14, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.ink)
                    Spacer(minLength: 0)
                    Text(detail ?? "—")
                        .font(.system(size: 9, weight: .medium, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                PressureMeter(fraction: fraction, color: pressureColor(fraction))
                    .frame(height: 5)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 46, alignment: .leading)
        .padding(.horizontal, 9)
        .padding(.vertical, 6)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue("\(value)，\(detail ?? "暂无数据")")
    }
}

private struct EndpointTelemetryHistoryPanel: View {
    @ObservedObject var store: BrokerStore
    let endpoint: EndpointRecord
    @State private var range: EndpointTelemetryRange = .oneHour

    private var history: EndpointTelemetryHistory? {
        store.endpointTelemetryHistory(endpointID: endpoint.id, range: range)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text("历史")
                    .font(.system(size: 13, weight: .semibold))
                Spacer()
                Picker("时间范围", selection: $range) {
                    Text("1h").tag(EndpointTelemetryRange.oneHour)
                    Text("6h").tag(EndpointTelemetryRange.sixHours)
                    Text("24h").tag(EndpointTelemetryRange.twentyFourHours)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 126)
                .accessibilityLabel("资源历史时间范围")
            }

            if !store.supportsEndpointTelemetryHistory {
                DetailCallout(
                    icon: "chart.line.uptrend.xyaxis",
                    color: DesignTokens.warning,
                    message: "当前服务不支持历史数据。"
                )
            } else if store.endpointTelemetryHistoryLoading.contains(endpoint.id) {
                Label("正在载入 \(range.rawValue)", systemImage: "arrow.clockwise")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
                    .padding(.horizontal, 12)
                    .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
            } else if let error = store.endpointTelemetryHistoryErrors[endpoint.id] {
                DetailCallout(icon: "exclamationmark.triangle.fill", color: DesignTokens.danger, message: error)
            } else if let history, !history.samples.isEmpty {
                if history.endpointID == endpoint.id {
                    EndpointTelemetryHistoryChart(history: history).equatable()
                } else {
                    DetailCallout(
                        icon: "exclamationmark.shield.fill",
                        color: DesignTokens.danger,
                        message: "历史数据校验失败。"
                    )
                }
            } else {
                DetailCallout(icon: "clock", color: DesignTokens.mutedInk, message: "所选时段暂无数据。")
            }
        }
        // History persists at a 60-second cadence. Refresh only while this
        // selected detail is visible, so the overview never fans out into one
        // request per endpoint and hidden charts cannot become a render driver.
        .task(id: "\(endpoint.id):\(range.rawValue)") {
            requestIfSupported()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                guard !Task.isCancelled else { return }
                requestIfSupported()
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("资源历史")
    }

    private func requestIfSupported() {
        guard store.supportsEndpointTelemetryHistory else { return }
        store.requestEndpointTelemetryHistory(endpointID: endpoint.id, range: range)
    }
}

private struct EndpointTelemetryHistoryChart: View, Equatable {
    let history: EndpointTelemetryHistory

    static func == (lhs: Self, rhs: Self) -> Bool { lhs.history == rhs.history }

    var body: some View {
        let prepared = EndpointTelemetryPreparedHistory(history: history)
        VStack(alignment: .leading, spacing: 8) {
            EndpointTelemetryHistoryContext(prepared: prepared)
            if prepared.hostSamples.isEmpty {
                Label(
                    "所选时段暂无数据。",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(DesignTokens.warning)
                .frame(maxWidth: .infinity, minHeight: 112, alignment: .leading)
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 300), spacing: 8)],
                    alignment: .leading,
                    spacing: 8
                ) {
                    EndpointTelemetryMetricChart(
                        title: "CPU 使用率",
                        subtitle: "",
                        series: prepared.cpuSeries,
                        hoverItems: prepared.cpuHoverItems,
                        emptyMessage: "无 CPU 历史数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "内存占用率",
                        subtitle: "",
                        series: prepared.memorySeries,
                        hoverItems: prepared.memoryHoverItems,
                        emptyMessage: "无内存历史数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "GPU 利用率",
                        subtitle: prepared.gpuSeries.isEmpty ? "无 GPU" : "",
                        series: prepared.gpuUtilizationSeries,
                        hoverItems: prepared.gpuUtilizationHoverItems,
                        emptyMessage: prepared.gpuSeries.isEmpty ? "无 GPU" : "无 GPU 利用率数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "显存占用率",
                        subtitle: prepared.gpuSeries.isEmpty ? "无 GPU" : "",
                        series: prepared.gpuMemorySeries,
                        hoverItems: prepared.gpuMemoryHoverItems,
                        emptyMessage: prepared.gpuSeries.isEmpty ? "无 GPU" : "无显存历史数据"
                    )
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("端点资源历史")
        .accessibilityValue(prepared.accessibilityValue)
    }
}

private struct EndpointTelemetryHistoryContext: View {
    let prepared: EndpointTelemetryPreparedHistory

    var body: some View {
        HStack(spacing: 8) {
            Label(prepared.lastObservationLabel, systemImage: "clock")
            if let warning = prepared.visualWarningLabel {
                Label(warning, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(DesignTokens.warning)
            }
            Spacer(minLength: 0)
        }
        .font(.system(size: 9, weight: .semibold, design: .rounded))
        .foregroundStyle(DesignTokens.mutedInk)
        .lineLimit(1)
    }
}

private struct EndpointTelemetryMetricChart: View, Equatable {
    let title: String
    let subtitle: String
    let series: [EndpointTelemetryLineSeries]
    let hoverItems: [EndpointTelemetryHoverItem]
    let emptyMessage: String

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.title == rhs.title
            && lhs.subtitle == rhs.subtitle
            && lhs.series == rhs.series
            && lhs.hoverItems == rhs.hoverItems
            && lhs.emptyMessage == rhs.emptyMessage
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(title).font(.system(size: 12, weight: .semibold))
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }

            if series.allSatisfy({ $0.points.isEmpty }) {
                Label(emptyMessage, systemImage: "chart.line.flattrend.xyaxis")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 154, alignment: .leading)
            } else {
#if canImport(Charts)
                chart
#else
                Text("当前系统没有 Swift Charts，使用文本降级。")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 154, alignment: .leading)
#endif
            }

            if series.count > 1 { legend }
        }
        .padding(7)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 0.8))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(accessibilitySummary)
    }

    private var accessibilitySummary: String {
        let summaries = series.compactMap { line -> String? in
            guard
                let latest = line.points.max(by: { $0.timestamp < $1.timestamp }),
                let minimum = line.points.map(\.value).min(),
                let maximum = line.points.map(\.value).max()
            else { return nil }
            return "\(line.label)：最新 \(historyPercent(latest.value))，最低 \(historyPercent(minimum))，最高 \(historyPercent(maximum))"
        }
        return summaries.isEmpty ? emptyMessage : summaries.joined(separator: "；")
    }

    private var legend: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 70), spacing: 7)], alignment: .leading, spacing: 4) {
            ForEach(series) { line in
                HStack(spacing: 4) {
                    Capsule().fill(line.color).frame(width: 12, height: 3)
                    Text(line.label).lineLimit(1)
                }
                .font(.system(size: 8, weight: .semibold))
                .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        .accessibilityHidden(true)
    }

#if canImport(Charts)
    private var chart: some View {
        Chart {
            ForEach(series) { line in
                ForEach(line.points) { point in
                    LineMark(
                        x: .value("观测时间", point.timestamp),
                        y: .value("使用率", point.value),
                        series: .value("连续片段", point.segmentID)
                    )
                    .foregroundStyle(line.color)
                    .interpolationMethod(.linear)
                    .lineStyle(StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
                }
            }
        }
        .chartYScale(domain: 0...1)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(DesignTokens.surfaceStroke)
                AxisTick()
                AxisValueLabel(format: .dateTime.hour().minute())
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: [0, 0.5, 1]) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(DesignTokens.surfaceStroke)
                AxisTick()
                AxisValueLabel {
                    if let fraction = value.as(Double.self) { Text(historyPercent(fraction)) }
                }
            }
        }
        .chartOverlay { proxy in
            EndpointTelemetryChartHoverOverlay(proxy: proxy, items: hoverItems)
        }
        .frame(height: 126)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(accessibilitySummary)
        .accessibilityHint("将指针悬停在图表上可检查最近的观测样本。")
    }
#endif
}

#if canImport(Charts)
private struct EndpointTelemetryChartHoverOverlay: View {
    let proxy: ChartProxy
    let items: [EndpointTelemetryHoverItem]
    @State private var selectedIndex: Int?

    var body: some View {
        GeometryReader { geometry in
            if let anchor = proxy.plotFrame {
                let frame = geometry[anchor]
                Rectangle()
                    .fill(.clear)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let location):
                            let x = location.x - frame.origin.x
                            guard x >= 0, x <= frame.width, let date = proxy.value(atX: x, as: Date.self) else { return }
                            updateSelection(for: date)
                        case .ended:
                            updateSelection(to: nil)
                        }
                    }
                    .overlay {
                        if let selected = selectedItem, let position = proxy.position(forX: selected.timestamp) {
                            let x = frame.minX + position
                            let cardWidth = min(
                                EndpointTelemetryHoverCard.preferredWidth(for: selected.entries.count),
                                max(184, geometry.size.width - 8)
                            )
                            let halfCardWidth = cardWidth / 2
                            Path { path in
                                path.move(to: CGPoint(x: x, y: frame.minY))
                                path.addLine(to: CGPoint(x: x, y: frame.maxY))
                            }
                            .stroke(DesignTokens.ink.opacity(0.55), style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                            .allowsHitTesting(false)
                            EndpointTelemetryHoverCard(item: selected, width: cardWidth)
                                .position(
                                    x: min(max(4 + halfCardWidth, x), geometry.size.width - 4 - halfCardWidth),
                                    y: frame.midY
                                )
                                .allowsHitTesting(false)
                        }
                    }
            } else {
                Color.clear
            }
        }
    }

    private var selectedItem: EndpointTelemetryHoverItem? {
        guard let selectedIndex, items.indices.contains(selectedIndex) else { return nil }
        return items[selectedIndex]
    }

    private func updateSelection(for date: Date) {
        guard !items.isEmpty else { return }
        var lowerBound = 0
        var upperBound = items.count
        while lowerBound < upperBound {
            let middle = (lowerBound + upperBound) / 2
            if items[middle].timestamp < date { lowerBound = middle + 1 } else { upperBound = middle }
        }
        let candidate: Int
        if lowerBound == 0 {
            candidate = 0
        } else if lowerBound == items.count {
            candidate = items.count - 1
        } else {
            let earlier = items[lowerBound - 1]
            let later = items[lowerBound]
            candidate = abs(earlier.timestamp.timeIntervalSince(date)) <= abs(later.timestamp.timeIntervalSince(date))
                ? lowerBound - 1 : lowerBound
        }
        updateSelection(to: candidate)
    }

    private func updateSelection(to index: Int?) {
        guard selectedIndex != index else { return }
        var transaction = Transaction()
        transaction.animation = nil
        withTransaction(transaction) { selectedIndex = index }
    }
}

private struct EndpointTelemetryHoverCard: View {
    let item: EndpointTelemetryHoverItem
    let width: CGFloat

    static func preferredWidth(for entryCount: Int) -> CGFloat {
        entryCount > 4 ? 316 : 184
    }

    private var columns: [GridItem] {
        Array(
            repeating: GridItem(.flexible(minimum: 116), spacing: 8, alignment: .leading),
            count: item.entries.count > 4 ? 2 : 1
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(historyDateTime(item.timestamp))
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(DesignTokens.ink)
            ScrollView(.vertical) {
                LazyVGrid(columns: columns, alignment: .leading, spacing: 4) {
                    ForEach(item.entries) { entry in
                        HStack(spacing: 5) {
                            Capsule()
                                .fill(entry.color)
                                .frame(width: 12, height: 3)
                            Text(entry.label)
                                .lineLimit(1)
                            Spacer(minLength: 4)
                            Text(entry.value)
                                .fontWeight(.bold)
                        }
                        .font(.system(size: 8, weight: .medium, design: .monospaced))
                        .foregroundStyle(DesignTokens.ink)
                    }
                }
            }
            .scrollIndicators(.hidden)
            .frame(maxHeight: 146)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .frame(width: width, alignment: .leading)
        .frame(maxHeight: 176)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .shadow(color: .black.opacity(0.10), radius: 5, y: 2)
    }
}
#endif

private struct EndpointTelemetryPreparedHistory: Equatable {
    let range: EndpointTelemetryRange
    let hostSamples: [EndpointTelemetryPreparedHostSample]
    let gpuSeries: [EndpointTelemetryPreparedGPUSeries]
    let cpuSeries: [EndpointTelemetryLineSeries]
    let memorySeries: [EndpointTelemetryLineSeries]
    let gpuUtilizationSeries: [EndpointTelemetryLineSeries]
    let gpuMemorySeries: [EndpointTelemetryLineSeries]
    let cpuHoverItems: [EndpointTelemetryHoverItem]
    let memoryHoverItems: [EndpointTelemetryHoverItem]
    let gpuUtilizationHoverItems: [EndpointTelemetryHoverItem]
    let gpuMemoryHoverItems: [EndpointTelemetryHoverItem]
    let rejectedHostSampleCount: Int
    let generatedAt: Date?
    let hostSamplingGapCount: Int

    init(history: EndpointTelemetryHistory) {
        range = history.range
        generatedAt = history.generatedAt.flatMap(endpointTelemetryHistoryDate)
        var seenHostIDs = Set<String>()
        let decodedHost = history.samples.compactMap(EndpointTelemetryPreparedHostSample.init).sorted { $0.timestamp < $1.timestamp }
        hostSamples = decodedHost.filter { seenHostIDs.insert($0.id).inserted }
        rejectedHostSampleCount = history.samples.count - hostSamples.count
        hostSamplingGapCount = EndpointTelemetryPreparedHistory.gapCount(in: hostSamples.map(\.timestamp))

        cpuSeries = [EndpointTelemetryPreparedHistory.lineSeries(
            id: "cpu", label: "CPU", color: DesignTokens.chartSeries[0],
            samples: hostSamples.map { ($0.timestamp, $0.cpuFraction) }
        )]
        memorySeries = [EndpointTelemetryPreparedHistory.lineSeries(
            id: "memory", label: "内存", color: DesignTokens.chartSeries[2],
            samples: hostSamples.map { ($0.timestamp, $0.memoryFraction) }
        )]
        cpuHoverItems = hostSamples.map {
            EndpointTelemetryHoverItem(
                timestamp: $0.timestamp,
                title: "CPU 占用",
                entries: [EndpointTelemetryHoverEntry(id: "cpu", label: "CPU", value: historyPercent($0.cpuFraction), color: DesignTokens.chartSeries[0])]
            )
        }
        memoryHoverItems = hostSamples.map {
            EndpointTelemetryHoverItem(
                timestamp: $0.timestamp,
                title: "内存占用",
                entries: [EndpointTelemetryHoverEntry(id: "memory", label: "内存", value: historyPercent($0.memoryFraction), color: DesignTokens.chartSeries[2])]
            )
        }

        gpuSeries = history.gpuSeries.map(EndpointTelemetryPreparedGPUSeries.init).sorted { $0.index < $1.index }
        gpuUtilizationSeries = gpuSeries.enumerated().map { offset, gpu in
            EndpointTelemetryPreparedHistory.lineSeries(
                id: gpu.id, label: gpu.label, color: EndpointTelemetryPreparedHistory.gpuColor(offset),
                samples: gpu.samples.map { ($0.timestamp, $0.gpuUtilizationFraction) }
            )
        }
        gpuMemorySeries = gpuSeries.enumerated().map { offset, gpu in
            EndpointTelemetryPreparedHistory.lineSeries(
                id: "\(gpu.id)-memory", label: gpu.label, color: EndpointTelemetryPreparedHistory.gpuColor(offset),
                samples: gpu.samples.map { ($0.timestamp, $0.memoryFraction) }
            )
        }
        gpuUtilizationHoverItems = EndpointTelemetryPreparedHistory.hoverItems(
            series: gpuUtilizationSeries,
            prefix: "GPU 利用率"
        )
        gpuMemoryHoverItems = EndpointTelemetryPreparedHistory.hoverItems(
            series: gpuMemorySeries,
            prefix: "GPU 显存"
        )
    }

    var lastObservationLabel: String {
        guard let latest = hostSamples.last else { return "时间未知" }
        return "更新于 \(historyShortTime(latest.timestamp))"
    }

    var visualWarningLabel: String? {
        if rejectedHostSampleCount > 0, hostSamplingGapCount > 0 {
            return "\(rejectedHostSampleCount) 异常 · \(hostSamplingGapCount) 断点"
        }
        if rejectedHostSampleCount > 0 { return "\(rejectedHostSampleCount) 异常" }
        if hostSamplingGapCount > 0 { return "\(hostSamplingGapCount) 断点" }
        if hostSamples.count > 1, hostSamples.count < 3 { return "样本不足" }
        return nil
    }

    var freshnessAndTrustLabel: String {
        var contexts = [freshnessLabel]
        if rejectedHostSampleCount > 0 { contexts.append("已省略 \(rejectedHostSampleCount) 个无法验证的主机样本。") }
        if hostSamples.count < 3, hostSamples.count > 1 {
            contexts.append("样本不足 3 个，未假设连续采样。")
        } else if hostSamplingGapCount > 0 {
            contexts.append("发现 \(hostSamplingGapCount) 段采样间隔，趋势已在间隔处断开。")
        }
        contexts.append("历史趋势只供检查；资源申请仍以当前快照和端点状态为准。")
        return contexts.joined(separator: " ")
    }

    var accessibilityValue: String {
        "范围 \(range.rawValue)，已验证主机样本 \(hostSamples.count) 个，GPU 序列 \(gpuSeries.count) 条。\(freshnessAndTrustLabel)"
    }

    private var freshnessLabel: String {
        guard let generatedAt else { return "服务未提供历史响应生成时间，无法判定历史数据新鲜度。" }
        guard let latest = hostSamples.last else { return "响应生成于 \(historyDateTime(generatedAt))，但没有可验证的观测样本。" }
        let lag = generatedAt.timeIntervalSince(latest.timestamp)
        guard lag >= 0 else { return "响应生成时间早于最后观测时间，无法判定历史数据新鲜度。" }
        return "响应生成于 \(historyDateTime(generatedAt))；最后观测落后 \(historyElapsedDescription(lag))。"
    }

    private static func lineSeries(
        id: String, label: String, color: Color, samples: [(Date, Double?)]
    ) -> EndpointTelemetryLineSeries {
        let threshold = gapThreshold(samples.map(\.0))
        var points: [EndpointTelemetryChartPoint] = []
        var segment = 0
        var previousTimestamp: Date?
        for (timestamp, value) in samples {
            if let previousTimestamp, let threshold, timestamp.timeIntervalSince(previousTimestamp) > threshold { segment += 1 }
            guard let value else {
                segment += 1
                previousTimestamp = timestamp
                continue
            }
            points.append(EndpointTelemetryChartPoint(
                id: "\(id)-\(timestamp.timeIntervalSince1970)-\(segment)", timestamp: timestamp, value: value, segmentID: "\(id)-\(segment)"
            ))
            previousTimestamp = timestamp
        }
        return EndpointTelemetryLineSeries(id: id, label: label, color: color, points: points)
    }

    private static func hoverItems(series: [EndpointTelemetryLineSeries], prefix: String) -> [EndpointTelemetryHoverItem] {
        var values = [Date: [EndpointTelemetryHoverEntry]]()
        for line in series {
            for point in line.points {
                values[point.timestamp, default: []].append(
                    EndpointTelemetryHoverEntry(
                        id: line.id,
                        label: line.label,
                        value: historyPercent(point.value),
                        color: line.color
                    )
                )
            }
        }
        return values.map { timestamp, entries in
            EndpointTelemetryHoverItem(timestamp: timestamp, title: prefix, entries: entries)
        }.sorted { $0.timestamp < $1.timestamp }
    }

    private static func gapCount(in timestamps: [Date]) -> Int {
        guard let threshold = gapThreshold(timestamps) else { return 0 }
        return zip(timestamps, timestamps.dropFirst()).filter { $1.timeIntervalSince($0) > threshold }.count
    }

    private static func gapThreshold(_ timestamps: [Date]) -> TimeInterval? {
        let intervals = zip(timestamps, timestamps.dropFirst()).map { $1.timeIntervalSince($0) }.filter { $0 > 0 }
        guard intervals.count >= 2 else { return nil }
        let sorted = intervals.sorted()
        let middle = sorted.count / 2
        let median = sorted.count.isMultiple(of: 2) ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle]
        return median * 2.5
    }

    private static func gpuColor(_ index: Int) -> Color {
        DesignTokens.chartSeries[index % DesignTokens.chartSeries.count]
    }
}

private struct EndpointTelemetryPreparedHostSample: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let cpuFraction: Double?
    let memoryFraction: Double?

    init?(_ sample: EndpointTelemetrySample) {
        guard let timestamp = endpointTelemetryHistoryDate(sample.timestamp), sample.status.map({ ["ONLINE", "OK"].contains($0) }) ?? true else { return nil }
        let cpu = sample.cpuLoadFraction.flatMap(endpointTelemetryHistoryFraction)
        let memory = sample.memoryFraction.flatMap(endpointTelemetryHistoryFraction)
        guard cpu != nil || memory != nil else { return nil }
        id = sample.timestamp
        self.timestamp = timestamp
        cpuFraction = cpu
        memoryFraction = memory
    }
}

private struct EndpointTelemetryPreparedGPUSeries: Identifiable, Equatable {
    let id: String
    let index: Int
    let label: String
    let samples: [EndpointTelemetryPreparedGPUSample]

    init(_ series: EndpointGPUHistorySeries) {
        let samples = series.samples.compactMap(EndpointTelemetryPreparedGPUSample.init).sorted { $0.timestamp < $1.timestamp }
        id = series.id
        index = series.index
        label = series.label
        self.samples = samples
    }
}

private struct EndpointTelemetryPreparedGPUSample: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let gpuUtilizationFraction: Double?
    let memoryFraction: Double?

    init?(_ sample: EndpointGPUHistorySample) {
        guard let timestamp = endpointTelemetryHistoryDate(sample.timestamp) else { return nil }
        let gpu = sample.gpuUtilizationFraction.flatMap(endpointTelemetryHistoryFraction)
        let memory = sample.memoryFraction.flatMap(endpointTelemetryHistoryFraction)
        guard gpu != nil || memory != nil else { return nil }
        id = sample.timestamp
        self.timestamp = timestamp
        gpuUtilizationFraction = gpu
        memoryFraction = memory
    }
}

private struct EndpointTelemetryLineSeries: Identifiable, Equatable {
    let id: String
    let label: String
    let color: Color
    let points: [EndpointTelemetryChartPoint]

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.points == rhs.points
    }
}

private struct EndpointTelemetryChartPoint: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let value: Double
    let segmentID: String
}

private struct EndpointTelemetryHoverItem: Identifiable, Equatable {
    var id: Date { timestamp }
    let timestamp: Date
    let title: String
    let entries: [EndpointTelemetryHoverEntry]

    var summary: String {
        "\(title)：\(entries.map { "\($0.label) \($0.value)" }.joined(separator: " · "))"
    }
}

private struct EndpointTelemetryHoverEntry: Identifiable, Equatable {
    let id: String
    let label: String
    let value: String
    let color: Color

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.value == rhs.value
    }
}

private func endpointTelemetryHistoryDate(_ value: String) -> Date? {
    EndpointTelemetryHistoryDateParser.fractional.date(from: value)
        ?? EndpointTelemetryHistoryDateParser.standard.date(from: value)
}

private enum EndpointTelemetryHistoryDateParser {
    static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    static let standard = ISO8601DateFormatter()
}

private func endpointTelemetryHistoryFraction(_ value: Double) -> Double? {
    guard value.isFinite, (0...1).contains(value) else { return nil }
    return value
}

private func historyPercent(_ value: Double?) -> String {
    guard let value else { return "—" }
    return "\(Int((value * 100).rounded()))%"
}

private func historyDateTime(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "M 月 d 日 HH:mm:ss"
    return formatter.string(from: value)
}

private func historyShortTime(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: value)
}

private func historyElapsedDescription(_ value: TimeInterval) -> String {
    let seconds = max(0, Int(value.rounded()))
    if seconds < 60 { return "\(seconds) 秒" }
    if seconds < 3_600 { return "\(seconds / 60) 分钟" }
    return "\(seconds / 3_600) 小时 \((seconds % 3_600) / 60) 分钟"
}

private struct SpatialGPUCell: View {
    let gpu: GPURecord
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 7) {
                GPUUsageGlyph(gpu: gpu, diameter: 26)
                VStack(alignment: .leading, spacing: 2) {
                    Text("GPU \(gpu.index) · \(gpu.name)")
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Text("\(gpu.vramLabel) · \(gpuPresentationLabel(gpu))")
                        .font(.system(size: 10, weight: .medium, design: .rounded))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU \(gpu.index) · \(gpu.name)")
        .accessibilityValue("\(gpu.vramLabel) · \(gpuPresentationLabel(gpu))")
    }
}

private struct ServerPool: View {
    @ObservedObject var store: BrokerStore
    let snapshot: BrokerSnapshot
    let claimEndpoint: (String) -> Void
    let openEndpoint: (EndpointRecord) -> Void
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("服务器")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Text("在线情况和空闲 GPU 数")
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

            if snapshot.operationalEndpoints.isEmpty {
                EmptyServerPool()
                    .background(DesignTokens.surface.opacity(0.78), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 300, maximum: 430), spacing: 12)],
                    alignment: .leading,
                    spacing: 12
                ) {
                    ForEach(snapshot.operationalEndpoints) { endpoint in
                        ServerAccessoryCard(
                            store: store,
                            endpoint: endpoint,
                            gpus: snapshot.gpus(for: endpoint),
                            claim: { claimEndpoint(endpoint.id) },
                            open: { openEndpoint(endpoint) },
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
    let selectGPU: (GPURecord) -> Void
    @State private var hovering = false

    private var availableGPUCount: Int {
        gpus.filter(\.isPubliclyAvailable).count
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
        ["ERROR", "STALE", "DISABLED", "DRAINING"].contains(endpoint.monitorStatus)
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
                    Text(gpus.isEmpty ? "GPU 状态" : "空闲 GPU")
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
                        Button(action: open) {
                            Label("查看详情", systemImage: "chevron.right")
                                .font(.system(size: 11, weight: .semibold))
                        }
                        .buttonStyle(SecondaryActionButtonStyle())
                        .help("查看服务器详情并管理生命周期")
                    } else {
                        Button(action: claim) {
                            Label("申请", systemImage: "key.fill")
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
        .scaleEffect(hovering ? 1.006 : 1)
        .animation(.easeOut(duration: 0.2), value: hovering)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .contain)
    }

    private var statusColor: Color {
        switch endpoint.monitorStatus {
        case "ONLINE": return DesignTokens.success
        case "PENDING", "DRAINING": return DesignTokens.warning
        case "ERROR", "STALE", "DISABLED": return DesignTokens.danger
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
            return "可查看详情并管理服务器生命周期"
        }
        return gpus.isEmpty ? "读取完成后显示 GPU 明细" : "点击编号查看 GPU 详情"
    }
}

private func endpointGPUModelSummary(_ gpus: [GPURecord]) -> String {
    guard !gpus.isEmpty else { return "无 GPU" }
    let names = Array(Set(gpus.map(\.name))).sorted()
    guard let first = names.first else { return "无 GPU" }
    return names.count == 1 ? first : "\(first) +\(names.count - 1) 类"
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

private let endpointHighPressureThreshold = 0.85

private func endpointPressureFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    [
        endpoint.cpuLoadFraction,
        endpoint.memoryFraction,
        endpointAverageUtilizationFraction(endpoint: endpoint, gpus: gpus)
    ]
    .compactMap { $0 }
    .max()
}

private func endpointHighPressure(endpoint: EndpointRecord, gpus: [GPURecord]) -> Bool {
    guard let pressure = endpointPressureFraction(endpoint: endpoint, gpus: gpus) else { return false }
    return pressure >= endpointHighPressureThreshold
}

private func endpointRequiresAttention(endpoint: EndpointRecord, gpus: [GPURecord]) -> Bool {
    endpointNeedsAttention(endpoint)
        || gpus.contains(where: gpuNeedsAttention)
        || endpointHighPressure(endpoint: endpoint, gpus: gpus)
}

private func pressureColor(_ fraction: Double?) -> Color {
    guard let fraction else { return DesignTokens.mutedInk }
    switch fraction {
    case ..<0.70: return DesignTokens.success
    case ..<0.90: return DesignTokens.warning
    default: return DesignTokens.danger
    }
}

private func isGPUClaimed(_ gpu: GPURecord) -> Bool {
    if gpu.isPubliclyAvailable { return false }
    return ["HELD", "LEASED_IDLE", "RUNNING_MANAGED", "ORPHANED_BUSY", "CONFLICT", "RESERVED"].contains(gpu.state)
}

private func endpointNeedsAttention(_ endpoint: EndpointRecord) -> Bool {
    ["ERROR", "STALE", "DISABLED", "DRAINING"].contains(endpoint.monitorStatus)
        || !endpoint.enabled
}

private func gpuNeedsAttention(_ gpu: GPURecord) -> Bool {
    [
        "BUSY_UNMANAGED",
        "UNKNOWN_RECOVERING",
        "UNKNOWN_STALE",
        "UNHEALTHY",
        "CONFLICT",
        "ORPHANED_BUSY",
        "DISABLED",
        "DRAINING",
        "MAINTENANCE"
    ].contains(gpu.state)
}

private func gpuStateColor(_ state: String) -> Color {
    switch state {
    case "AVAILABLE": return DesignTokens.success
    case "HELD", "LEASED_IDLE", "KEEPALIVE": return DesignTokens.interaction
    case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED", "DRAINING", "MAINTENANCE": return DesignTokens.warning
    default: return DesignTokens.danger
    }
}

private func gpuStateLabel(_ state: String) -> String {
    switch state {
    case "AVAILABLE": return "可用 · 未开启占卡"
    case "HELD", "LEASED_IDLE", "RUNNING_MANAGED": return "使用中"
    case "KEEPALIVE": return "可用 · 空闲占卡"
    case "BUSY_UNMANAGED", "ORPHANED_BUSY": return "任务占用"
    case "RESERVED": return "不可分配"
    case "UNKNOWN_RECOVERING": return "正在连接"
    case "UNKNOWN_STALE": return "采集延迟"
    case "UNHEALTHY": return "GPU 故障"
    case "CONFLICT": return "归属待确认"
    case "DISABLED": return "已停用"
    case "MAINTENANCE", "DRAINING": return "不可分配"
    default: return "不可分配"
    }
}

func gpuPresentationLabel(_ gpu: GPURecord) -> String {
    if gpu.keepalive.state == "ERROR" {
        let reason = gpu.keepalive.reason.map(localizedStateReason) ?? "未知原因"
        return "可用 · 占卡异常：\(reason)"
    }
    if gpu.state == "AVAILABLE" { return "可用 · 未开启占卡" }
    if gpu.state == "KEEPALIVE" { return "可用 · 空闲占卡" }
    if ["HELD", "LEASED_IDLE", "RUNNING_MANAGED"].contains(gpu.state) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let task, !task.isEmpty else { return "使用中" }
        return task
    }
    if ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains(gpu.state) {
        return "任务占用"
    }
    return gpuStateLabel(gpu.state)
}

private func endpointStateIcon(_ state: String) -> String {
    switch state {
    case "ONLINE": return "server.rack"
    case "PENDING": return "hourglass"
    case "STALE": return "clock.badge.exclamationmark"
    case "ERROR": return "exclamationmark.triangle.fill"
    case "DISABLED": return "pause.circle.fill"
    case "DRAINING": return "arrow.down.forward.and.arrow.up.backward"
    default: return "questionmark.diamond.fill"
    }
}

private func localizedStateReason(_ reason: String) -> String {
    if reason == "no fresh telemetry after service start" {
        return "正在进行首次连接"
    }
    if reason == "GPU absent from latest complete endpoint observation" {
        return "本次更新未检测到这块 GPU"
    }
    if reason == "endpoint or GPU is disabled" {
        return "服务器或 GPU 已停用"
    }
    if reason == "lease/process attribution conflict" {
        return "此前观测到的任务进程与租约绑定不匹配；请由所属任务确认当前观测，或在任务结束后释放租约"
    }
    if reason == "lease expired while a compute process remains" {
        return "资源使用记录已到期，但服务器上仍有任务运行"
    }
    if reason == "bound workload process observed" {
        return "已检测到登记过的任务进程"
    }
    if reason == "compute process observed; admission blocked" {
        return "检测到未登记的计算进程，暂不能分配"
    }
    if reason == "exclusive lease active" {
        return "资源已分配给其他项目或任务"
    }
    if reason.hasPrefix("telemetry age "), reason.contains("exceeds stale threshold") {
        return "最近一次服务器数据已过期"
    }
    if reason.hasPrefix("reservation "), reason.contains(" is active") {
        return "预约正在生效"
    }
    return "状态需要人工确认"
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

private func historyTimestamp(_ value: String) -> String {
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    guard let date else { return value }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: date)
}

private struct LeaseSummaryGroup: Identifiable {
    let id: String
    let task: String
    let count: Int
}

private func leaseSummaryGroups(gpus: [GPURecord]) -> [LeaseSummaryGroup] {
    var groups: [String: Int] = [:]
    for gpu in gpus where isGPUClaimed(gpu) {
        let task = gpu.taskReference ?? "未标注任务"
        groups[task, default: 0] += 1
    }
    return groups.map { LeaseSummaryGroup(id: $0.key, task: $0.key, count: $0.value) }
        .sorted { lhs, rhs in
            lhs.count == rhs.count ? lhs.task < rhs.task : lhs.count > rhs.count
        }
}

private struct ServerLeaseSummary: View {
    let gpus: [GPURecord]

    private var groups: [LeaseSummaryGroup] {
        leaseSummaryGroups(gpus: gpus)
    }

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Image(systemName: "person.2.fill")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(groups.isEmpty ? DesignTokens.mutedInk : DesignTokens.interaction)
                .frame(width: 22, height: 22)
                .background(DesignTokens.surface.opacity(0.72), in: Circle())
            Text("归属")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(DesignTokens.mutedInk)
            Text(summaryText)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(DesignTokens.surface.opacity(0.64), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var summaryText: String {
        guard !gpus.isEmpty else { return "等待 GPU 数据" }
        guard let first = groups.first else { return "没有正在使用的资源" }
        let extra = groups.count > 1 ? "，+\(groups.count - 1)" : ""
        return "\(first.task) · \(first.count) GPU\(extra)"
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
                        Text("项目与任务")
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

                Picker("项目与任务资源状态", selection: $mode) {
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

                Label("只记录资源归属，不会停止任务", systemImage: "hand.raised.fill")
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
                        mode == .active ? "没有正在使用的资源" : "没有等待分配的申请",
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
    @State private var selectedGPUIDs: Set<String> = []
    @State private var reassignmentMessage: String?

    private var selectionChanged: Bool {
        selectedGPUIDs != Set(lease.gpuIDs)
    }

    private var selectionIsComplete: Bool {
        selectedGPUIDs.count == lease.gpuIDs.count
    }

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

                Divider()

                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("调整 GPU 分配")
                                .font(.system(size: 16, weight: .semibold))
                            Text("选择 \(lease.gpuIDs.count) 块 GPU。这里调整归属；对应 Agent 需要按新分配重启任务。")
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(DesignTokens.mutedInk)
                        }
                        Spacer(minLength: 12)
                        Button {
                            store.reassignLease(lease, gpuIDs: selectedGPUIDs.sorted()) { success, error in
                                reassignmentMessage = success
                                    ? "分配已更新。"
                                    : (error ?? "分配更新失败。")
                            }
                        } label: {
                            Label(
                                store.reassigningLeaseIDs.contains(lease.id) ? "应用中" : "应用分配",
                                systemImage: "arrow.triangle.swap"
                            )
                            .font(.system(size: 11, weight: .semibold))
                        }
                        .buttonStyle(HomeClaimButtonStyle(tint: DesignTokens.interaction.opacity(0.16), foreground: DesignTokens.interaction))
                        .disabled(
                            !store.allowsMutations
                                || store.reassigningLeaseIDs.contains(lease.id)
                                || !selectionChanged
                                || !selectionIsComplete
                        )
                    }

                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 210, maximum: 280), spacing: 10)], spacing: 10) {
                        ForEach(gpus.sorted { lhs, rhs in
                            lhs.endpointID == rhs.endpointID ? lhs.index < rhs.index : lhs.endpointID < rhs.endpointID
                        }) { gpu in
                            Button {
                                if selectedGPUIDs.contains(gpu.id) {
                                    selectedGPUIDs.remove(gpu.id)
                                } else if selectedGPUIDs.count < lease.gpuIDs.count {
                                    selectedGPUIDs.insert(gpu.id)
                                }
                            } label: {
                                HStack(spacing: 9) {
                                    Image(systemName: selectedGPUIDs.contains(gpu.id) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(selectedGPUIDs.contains(gpu.id) ? DesignTokens.interaction : DesignTokens.mutedInk)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text("\(gpu.endpointID) · GPU \(gpu.index)")
                                            .font(.system(size: 11, weight: .semibold, design: .monospaced))
                                            .lineLimit(1)
                                        Text(gpu.taskReference ?? gpu.state)
                                            .font(.system(size: 10, weight: .medium))
                                            .foregroundStyle(DesignTokens.mutedInk)
                                            .lineLimit(1)
                                    }
                                    Spacer(minLength: 0)
                                }
                                .padding(10)
                                .background(
                                    selectedGPUIDs.contains(gpu.id)
                                        ? DesignTokens.interaction.opacity(0.12)
                                        : DesignTokens.ink.opacity(0.035),
                                    in: RoundedRectangle(cornerRadius: 9, style: .continuous)
                                )
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }

                    if !selectionIsComplete {
                        Text("还需选择 \(lease.gpuIDs.count - selectedGPUIDs.count) 块 GPU。")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(DesignTokens.warning)
                    }
                    if let reassignmentMessage {
                        NoticeBanner(
                            message: reassignmentMessage,
                            color: reassignmentMessage == "分配已更新。" ? DesignTokens.success : DesignTokens.danger,
                            icon: reassignmentMessage == "分配已更新。" ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                        )
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
        .onAppear {
            selectedGPUIDs = Set(lease.gpuIDs)
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

            Label("ServerPilot 只管理资源归属，不会操作远端任务", systemImage: "hand.raised.fill")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .padding(.top, 2)
        }
    }

    private var leaseSectionSubtitle: String {
        let count = store.snapshot.leases.count
        return count == 0 ? "所有 GPU 都已归还" : "\(count) 个使用记录"
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
            LeaseOverviewItem(value: "\(leaseCount)", label: "使用记录", icon: "key.fill", color: DesignTokens.interaction)
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
        case "KEEPALIVE": return "shield.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.success
        case "HELD", "LEASED_IDLE", "KEEPALIVE": return DesignTokens.interaction
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }

    private var gpuTooltip: String {
        var details = "\(gpu.name) · \(gpu.vramLabel) · \(gpuStateLabel(gpu.state))"
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
    @State private var selectedGPUID: String?
    let endpointID: String
    let claim: () -> Void
    let edit: () -> Void

    private var endpoint: EndpointRecord? {
        store.snapshot.endpoint(id: endpointID)
    }

    private var gpus: [GPURecord] {
        guard let endpoint else { return [] }
        return store.snapshot.gpus(for: endpoint)
    }

    private var selectedGPUSelection: Binding<StableRecordSelection?> {
        Binding(
            get: { selectedGPUID.map(StableRecordSelection.init(id:)) },
            set: { selectedGPUID = $0?.id }
        )
    }

    private var availableGPUCount: Int {
        guard let endpoint else { return 0 }
        guard endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter(\.isPubliclyAvailable).count
    }

    private var claimedGPUCount: Int {
        gpus.filter(isGPUClaimed).count
    }

    private var conflictedLeases: [LeaseRecord] {
        reclaimableLeases.filter { lease in
            lease.gpuIDs.contains { gpuID in
                gpus.first(where: { $0.id == gpuID })?.state == "CONFLICT"
            }
        }
    }

    private var reclaimableLeases: [LeaseRecord] {
        store.snapshot.leases.filter { lease in
            guard lease.kind == "workload",
                  ["HELD", "ACTIVE", "CONFLICT"].contains(lease.state),
                  !lease.gpuIDs.isEmpty else { return false }
            return lease.gpuIDs.allSatisfy { gpuID in
                guard let gpu = gpus.first(where: { $0.id == gpuID }) else { return false }
                return ["HELD", "LEASED_IDLE", "CONFLICT"].contains(gpu.state)
            }
        }
    }

    private var reclaimableKeepaliveLeaseIDs: [String] {
        guard let endpoint else { return [] }
        let policyDisabled = endpoint.keepalive.policy == "disabled"
        let ids = gpus.compactMap { gpu -> String? in
            guard let leaseID = gpu.keepalive.leaseID else { return nil }
            // A normal start owns a held per-GPU lease while the helper starts.
            // It is not a stale record. Recovery appears only
            // after the user has asked to stop occupancy but that lease still
            // survives the fresh state projection.
            return policyDisabled ? leaseID : nil
        }
        return Array(Set(ids)).sorted()
    }

    private var isMutating: Bool {
        guard let endpoint else { return false }
        return store.mutatingEndpointIDs.contains(endpoint.id)
    }

    private var isPaused: Bool {
        guard let endpoint else { return false }
        return endpoint.lifecycleState == "DRAINING" || endpoint.monitorStatus == "DRAINING"
    }

    private var canApplyForGPU: Bool {
        availableGPUCount > 0 && store.allowsMutations && !isMutating
    }

    private var occupancyActionStarts: Bool {
        guard let endpoint else { return true }
        // A disabled policy can still have leases left by a partial/uncertain
        // stop. Keep the action as “结束占卡” so a human can retry the
        // authoritative stop instead of being forced through a new start.
        return !endpoint.keepalive.isEnabled && !endpoint.keepalive.hasResidualLease
    }

    private var occupancyActionTitle: String {
        occupancyActionStarts ? "开始占卡" : "结束占卡"
    }

    private var occupancyActionIcon: String {
        occupancyActionStarts ? "shield.fill" : "stop.circle.fill"
    }

    var body: some View {
        Group {
            if let endpoint {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        header(endpoint)

                        DetailCallout(
                            icon: "folder",
                            color: endpoint.workspacePath == nil ? DesignTokens.warning : DesignTokens.interaction,
                            message: endpoint.workspacePath.map { "远端工作区：\($0)" } ?? "远端工作区未设置；申请资源后仍需先补齐路径。"
                        )

                        serverActions(endpoint)

                        if endpoint.monitorStatus != "ONLINE" {
                            DetailCallout(
                                icon: endpointStateIcon(endpoint.monitorStatus),
                                color: endpoint.monitorStatus == "PENDING" ? DesignTokens.warning : DesignTokens.danger,
                                message: endpoint.monitorDetail ?? endpoint.monitorLabel
                            )
                        }

                        if endpoint.keepalive.configured {
                            occupancySummary(endpoint)
                        } else if store.supportsEndpointKeepalive {
                            DetailCallout(
                                icon: "shield",
                                color: DesignTokens.mutedInk,
                                message: "占卡程序由 ServerPilot 自动管理；点击“开始占卡”后会在符合条件的空闲 GPU 上运行。"
                            )
                        }

                        HStack(spacing: 10) {
                            detailStatistic(label: "可申请", value: capacityLabel, accent: DesignTokens.success)
                            detailStatistic(label: "已分配", value: "\(claimedGPUCount) / \(gpus.count)", accent: DesignTokens.interaction)
                            detailStatistic(label: "GPU 利用", value: percentageLabel(endpointAverageUtilizationFraction(endpoint: endpoint, gpus: gpus)), accent: DesignTokens.warning)
                            detailStatistic(label: "显存", value: percentageLabel(endpointAverageMemoryFraction(endpoint: endpoint, gpus: gpus)), accent: DesignTokens.ink)
                        }

                        if !gpus.isEmpty {
                            ServerLeaseSummary(gpus: gpus)
                        }

                        if !reclaimableKeepaliveLeaseIDs.isEmpty {
                            ForEach(reclaimableKeepaliveLeaseIDs, id: \.self) { leaseID in
                                let gpuCount = gpus.filter { $0.keepalive.leaseID == leaseID }.count
                                DetailCallout(
                                    icon: "shield.lefthalf.filled.badge.checkmark",
                                    color: DesignTokens.warning,
                                    message: "占卡已停止，但 \(gpuCount) 张 GPU 仍有遗留占卡租约；确认没有进程后可释放。",
                                    actionTitle: isMutating ? "处理中" : "释放遗留占卡",
                                    action: {
                                        guard !isMutating, confirmEmptyKeepaliveCleanup(gpuCount: gpuCount) else { return }
                                        store.clearEmptyConflictedLease(
                                            endpointID: endpoint.id,
                                            leaseID: leaseID
                                        ) { _, _ in }
                                    }
                                )
                            }
                        }

                        let conflictedGPUCount = gpus.filter { $0.state == "CONFLICT" }.count
                        if !reclaimableLeases.isEmpty {
                            ForEach(reclaimableLeases) { lease in
                                let conflict = conflictedLeases.contains(where: { $0.id == lease.id })
                                let gpuCount = lease.gpuIDs.filter { gpuID in
                                    guard let state = gpus.first(where: { $0.id == gpuID })?.state else { return false }
                                    return conflict ? state == "CONFLICT" : ["HELD", "LEASED_IDLE"].contains(state)
                                }.count
                                DetailCallout(
                                    icon: conflict ? "exclamationmark.shield.fill" : "clock.badge.exclamationmark",
                                    color: conflict ? DesignTokens.warning : DesignTokens.interaction,
                                    message: conflict
                                        ? "有 \(gpuCount) 张 GPU 的归属待确认；它们暂时不能申请，其余 \(availableGPUCount) 张仍可申请。"
                                        : "有 \(gpuCount) 张 GPU 仍被租约占用，但当前采集没有观察到进程；可确认后释放。",
                                    actionTitle: isMutating
                                        ? "处理中"
                                        : (conflict ? "清理遗留归属" : "释放空闲占用"),
                                    action: {
                                        guard confirmEmptyLeaseCleanup(lease, conflict: conflict) else { return }
                                        store.clearEmptyConflictedLease(
                                            endpointID: endpoint.id,
                                            leaseID: lease.id
                                        ) { _, _ in }
                                    }
                                )
                            }
                        } else if conflictedGPUCount > 0 {
                            DetailCallout(
                                icon: "exclamationmark.shield.fill",
                                color: DesignTokens.warning,
                                message: "有 \(conflictedGPUCount) 张 GPU 的归属待确认；它们暂时不能申请，其余 \(availableGPUCount) 张仍可申请。请刷新后重试清理。"
                            )
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
                                DetailCallout(
                                    icon: "square.grid.3x3",
                                    color: DesignTokens.mutedInk,
                                    message: endpoint.monitorStatus == "ONLINE" ? "未检测到 GPU" : "没有可显示的 GPU"
                                )
                            } else {
                                LazyVGrid(columns: [GridItem(.adaptive(minimum: gpus.count > 16 ? 142 : 220, maximum: 280), spacing: 10)], spacing: 10) {
                                    ForEach(gpus.sorted { $0.index < $1.index }) { gpu in
                                        ServerGPUDetailCard(gpu: gpu, compact: gpus.count > 16) {
                                            selectedGPUID = gpu.id
                                        }
                                    }
                                }
                            }
                        }

                        EndpointTelemetryHistoryPanel(store: store, endpoint: endpoint)

                        HStack {
                            Label(
                                endpoint.monitorStatus == "ONLINE" ? "状态按设定周期自动更新" : "当前数据已过期，暂不可申请 GPU",
                                systemImage: endpoint.monitorStatus == "ONLINE" ? "arrow.clockwise" : "hand.raised.fill"
                            )
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
            } else {
                ContentUnavailableView("服务器已不在当前快照中", systemImage: "server.rack")
                    .task { dismiss() }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: 560, idealWidth: 760, maxWidth: 920, minHeight: 420, idealHeight: 640, maxHeight: 780)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .accessibilityLabel("服务器详情")
        .accessibilityValue(accessibilityValue)
        .sheet(item: selectedGPUSelection) { selection in
            if let gpu = store.snapshot.gpu(id: selection.id) {
                GPUDetailSheet(gpu: gpu)
            }
        }
        .onChange(of: store.snapshot.endpoints.map(\.id)) { _, endpointIDs in
            if !endpointIDs.contains(endpointID) {
                dismiss()
            }
        }
        .onChange(of: gpus.map(\.id)) { _, gpuIDs in
            if let selectedGPUID, !gpuIDs.contains(selectedGPUID) {
                self.selectedGPUID = nil
            }
        }
    }

    private var capacityLabel: String {
        guard let endpoint else { return "已移除" }
        guard endpoint.monitorStatus == "ONLINE" else { return "不可分配" }
        return "\(availableGPUCount) / \(gpus.count)"
    }

    private var accessibilityValue: String {
        guard let endpoint else { return "服务器已不在当前快照中" }
        let occupancy = endpoint.keepalive.configured && !gpus.isEmpty
            ? "，占卡\(endpoint.keepalive.label)"
            : ""
        return "\(endpoint.displayName)，\(endpoint.monitorLabel)，\(gpus.count) 块 GPU\(occupancy)"
    }

    private func header(_ endpoint: EndpointRecord) -> some View {
        SheetTitle(
            icon: endpoint.monitorStatus == "ONLINE" ? "server.rack" : endpointStateIcon(endpoint.monitorStatus),
            title: "服务器详情",
            subtitle: endpoint.sshCommand
        )
    }

    @ViewBuilder
    private func serverActions(_ endpoint: EndpointRecord) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 9) {
                primaryServerActions(endpoint)
                Spacer(minLength: 0)
                serverOperations(endpoint)
            }
            VStack(alignment: .leading, spacing: 10) {
                primaryServerActions(endpoint)
                serverOperations(endpoint)
            }
        }
    }

    @ViewBuilder
    private func primaryServerActions(_ endpoint: EndpointRecord) -> some View {
        HStack(spacing: 9) {
            if !gpus.isEmpty {
                Button {
                    dismiss()
                    DispatchQueue.main.async { claim() }
                } label: {
                    Label("申请 GPU", systemImage: "key.fill")
                }
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.interaction, foreground: DesignTokens.onInteraction))
                .accessibilityIdentifier("server-detail-claim")
                .disabled(!canApplyForGPU)
                .help(canApplyForGPU ? "只申请这台服务器上的 GPU；不会启动任务" : unavailableReason)
            }

            if store.supportsEndpointKeepalive {
                Button {
                    if occupancyActionStarts || confirmKeepaliveEnd() {
                        store.setEndpointKeepalive(endpoint, enabled: occupancyActionStarts) { _, _ in }
                    }
                } label: {
                    Label(isMutating ? "处理中" : occupancyActionTitle, systemImage: occupancyActionIcon)
                }
                .buttonStyle(SecondaryActionButtonStyle())
                .accessibilityIdentifier("endpoint-keepalive-action")
                .disabled(
                    gpus.isEmpty
                        || !store.allowsEndpointLifecycleMutations
                        || isMutating
                )
                .help(occupancyActionHelp)
            }
        }
    }

    private func serverOperations(_ endpoint: EndpointRecord) -> some View {
        Menu {
            if store.supportsEndpointUpdate {
                Button("编辑服务器", systemImage: "slider.horizontal.3", action: edit)
                    .disabled(!store.allowsEndpointLifecycleMutations || isMutating)
            }
            if store.supportsEndpointPauseResume {
                Button(isPaused ? "恢复接收新任务" : "暂停接收新任务", systemImage: isPaused ? "play.fill" : "pause.fill") {
                    if isPaused {
                        store.resumeEndpoint(endpoint) { _, _ in }
                    } else {
                        store.pauseEndpoint(endpoint) { _, _ in }
                    }
                }
                .disabled(!store.allowsEndpointLifecycleMutations || isMutating)
            }
            if store.supportsEndpointUpdate || store.supportsEndpointPauseResume {
                Divider()
            }
        } label: {
            Label("服务器操作", systemImage: "ellipsis.circle")
                .font(.system(size: 12, weight: .semibold))
        }
        .menuStyle(.borderlessButton)
        .accessibilityLabel("服务器操作")
        .help("编辑或暂停服务器")
    }

    private func occupancySummary(_ endpoint: EndpointRecord) -> some View {
        HStack(spacing: 10) {
            Image(systemName: occupancyActionStarts ? "shield" : "shield.fill")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(occupancyActionStarts ? DesignTokens.mutedInk : DesignTokens.interaction)
                .frame(width: 32, height: 32)
                .background((occupancyActionStarts ? DesignTokens.mutedInk : DesignTokens.interaction).opacity(0.12), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text("占卡")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(endpoint.keepalive.coverageSummary(totalGPUCount: gpus.count, taskGPUCount: taskGPUCount))
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("占卡")
        .accessibilityValue(endpoint.keepalive.coverageSummary(totalGPUCount: gpus.count, taskGPUCount: taskGPUCount))
    }

    private func detailStatistic(label: String, value: String, accent: Color) -> some View {
        HStack(spacing: 6) {
            Circle().fill(accent).frame(width: 6, height: 6)
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
            Text(value)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }

    private var taskGPUCount: Int {
        gpus.filter(\.isTaskOccupancy).count
    }

    private var unavailableReason: String {
        if !store.allowsMutations { return store.mutationUnavailableReason }
        if isMutating { return "服务器操作正在处理中。" }
        if gpus.isEmpty { return "这台服务器没有 GPU。" }
        if availableGPUCount == 0 { return "当前没有可申请的 GPU。" }
        return "当前不可申请。"
    }

    private var occupancyActionHelp: String {
        if gpus.isEmpty { return "这台服务器没有 GPU。" }
        guard store.allowsEndpointLifecycleMutations else { return store.endpointLifecycleMutationUnavailableReason }
        return occupancyActionStarts
            ? "开始这台服务器上空闲 GPU 的占卡"
            : "结束这台服务器上空闲 GPU 的占卡；不会停止正在运行的任务"
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
                        Text(gpuPresentationLabel(gpu))
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
        .accessibilityValue("\(gpuPresentationLabel(gpu))，UUID \(gpu.uuidLabel)")
    }

    private var detailLine: String {
        let utilization = gpu.utilization.map { "\($0)%" } ?? "—"
        return "\(gpu.memoryLabel) · \(utilization)"
    }
}

private struct ServerPoolHeader: View {
    var body: some View {
        HStack(spacing: 16) {
            Text("连接")
                .frame(width: 260, alignment: .leading)
            Text("空闲 GPU")
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
            .help("申请此服务器的 GPU")
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
        case "ERROR", "STALE", "DISABLED": return DesignTokens.danger
        default: return DesignTokens.mutedInk
        }
    }
}

private struct AvailabilityIndicator: View {
    let gpus: [GPURecord]

    var body: some View {
        let available = gpus.filter(\.isPubliclyAvailable).count
        let title = gpus.isEmpty ? "—" : "\(available) / \(gpus.count)"
        VStack(alignment: .leading, spacing: 5) {
            Text(title)
                .font(.system(size: 15, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
            Text(gpus.isEmpty ? "等待状态" : "空闲 GPU")
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
        case "KEEPALIVE": return "shield.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.success
        case "HELD", "LEASED_IDLE", "KEEPALIVE": return DesignTokens.interaction
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }

    private var gpuTooltip: String {
        var details = "\(gpu.name) · \(gpu.vramLabel) · \(gpuStateLabel(gpu.state))"
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
            if let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines), !task.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("当前任务")
                        .fieldLabel()
                    Text(task)
                        .font(.system(size: 13, weight: .semibold, design: .monospaced))
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(2)
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
        guard let value = gpu.utilization else { return "—" }
        return "\(value)%"
    }

    private var temperatureLabel: String {
        guard let value = gpu.temperature else { return "—" }
        return "\(value)°C"
    }

    private var stateLabel: String { gpuStateLabel(gpu.state) }

    private var stateIcon: String {
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "KEEPALIVE": return "shield.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.success
        case "HELD", "LEASED_IDLE", "KEEPALIVE": return DesignTokens.interaction
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
    let actionTitle: String?
    let action: (() -> Void)?

    init(
        icon: String,
        color: Color,
        message: String,
        actionTitle: String? = nil,
        action: (() -> Void)? = nil
    ) {
        self.icon = icon
        self.color = color
        self.message = message
        self.actionTitle = actionTitle
        self.action = action
    }

    var body: some View {
        HStack(spacing: 10) {
            Label(message, systemImage: icon)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(DesignTokens.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(.borderless)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .fixedSize()
            }
        }
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
        guard let age = snapshot.dataAgeSeconds else { return "尚无 GPU 状态数据" }
        return "最旧一条约 \(Int(age.rounded())) 秒前更新；离线服务器不计入"
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
    @State private var workspacePath = "/media/datasets/OminiEWM_Data/tmp/ljp"
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(icon: "server.rack", title: "添加服务器", subtitle: "")
            VStack(alignment: .leading, spacing: 8) {
                Text("SSH 指令")
                    .fieldLabel()
                TextField("ssh -p 2097 root@10.40.1.181", text: $sshCommand)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, weight: .medium, design: .monospaced))
                    .accessibilityLabel("SSH 指令")
            }
            LabeledField(
                label: "远端工作区路径",
                placeholder: "/media/datasets/OminiEWM_Data/tmp/ljp",
                text: $workspacePath
            )
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
        .frame(width: 500)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private func submit() {
        do {
            let parsed = try parseSSHCommand(sshCommand)
            let draft = try EndpointDraft(
                host: parsed.host,
                port: parsed.port,
                sshUser: parsed.user,
                workspacePath: workspacePath,
                observationProfile: .serverScript,
                suppliedID: ""
            )
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

private struct ParsedSSHCommand {
    let user: String
    let host: String
    let port: Int
}

private func parseSSHCommand(_ command: String) throws -> ParsedSSHCommand {
    let parts = command.split(whereSeparator: \Character.isWhitespace).map(String.init)
    guard parts.first == "ssh" else { throw EndpointDraftError.invalidEndpointFields }
    var port = 22
    var destination: String?
    var index = 1
    while index < parts.count {
        if parts[index] == "-p" {
            guard index + 1 < parts.count, let parsedPort = Int(parts[index + 1]), (1...65535).contains(parsedPort) else {
                throw EndpointDraftError.invalidEndpointFields
            }
            port = parsedPort
            index += 2
        } else if parts[index].hasPrefix("-") {
            throw EndpointDraftError.invalidEndpointFields
        } else if destination == nil {
            destination = parts[index]
            index += 1
        } else {
            throw EndpointDraftError.invalidEndpointFields
        }
    }
    guard let destination else { throw EndpointDraftError.invalidEndpointFields }
    let identity = destination.split(separator: "@", omittingEmptySubsequences: false)
    guard identity.count == 2, !identity[0].isEmpty, !identity[1].isEmpty else {
        throw EndpointDraftError.invalidEndpointFields
    }
    return ParsedSSHCommand(user: String(identity[0]), host: String(identity[1]), port: port)
}

private struct EditServerSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let endpoint: EndpointRecord
    @State private var sshUser: String
    @State private var workspacePath: String
    @State private var observationProfile: EndpointObservationProfile
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    init(store: BrokerStore, endpoint: EndpointRecord) {
        self.store = store
        self.endpoint = endpoint
        _sshUser = State(initialValue: endpoint.sshUser)
        _workspacePath = State(initialValue: endpoint.workspacePath ?? "")
        _observationProfile = State(initialValue: EndpointObservationProfile(rawValueOrDefault: endpoint.observationProfile))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SheetTitle(icon: "slider.horizontal.3", title: "编辑服务器", subtitle: "端点地址和端口是身份边界，不能在此修改。")
            VStack(alignment: .leading, spacing: 7) {
                Text("端点")
                    .fieldLabel()
                Text(endpoint.sshCommand)
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .textSelection(.enabled)
            }
            LabeledField(label: "SSH 用户", placeholder: "collector", text: $sshUser)
            LabeledField(
                label: "远端工作区路径",
                placeholder: "/media/datasets/OminiEWM_Data/tmp/ljp",
                text: $workspacePath
            )
            EndpointObservationProfileField(selection: $observationProfile)
            if let validationMessage {
                InlineValidation(message: validationMessage)
            }
            HStack {
                Spacer()
                Button("取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("保存设置") { submit() }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                    .keyboardShortcut(.defaultAction)
                    .disabled(!store.allowsEndpointLifecycleMutations || !store.supportsEndpointUpdate || isSubmitting)
                    .help(store.allowsEndpointLifecycleMutations ? "保存采集设置" : store.endpointLifecycleMutationUnavailableReason)
            }
        }
        .padding(28)
        .frame(width: 520)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private func submit() {
        do {
            let draft = try EndpointUpdateDraft(
                sshUser: sshUser,
                workspacePath: workspacePath,
                observationProfile: observationProfile
            )
            validationMessage = nil
            isSubmitting = true
            store.updateEndpoint(endpoint, draft: draft) { success, error in
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

private struct EndpointObservationProfileField: View {
    @Binding var selection: EndpointObservationProfile

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("采集方式")
                .fieldLabel()
            Picker("采集方式", selection: $selection) {
                ForEach(EndpointObservationProfile.allCases) { profile in
                    Text(profile.label).tag(profile)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(selection.scriptInfo)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("采集方式")
        .accessibilityValue(selection.label)
    }
}

private struct ClaimSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let initialEndpointID: String
    @State private var projectID = ""
    @State private var taskReference = ""
    @State private var gpuCountText = "1"
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
            SheetTitle(icon: "checkmark.seal.fill", title: "申请 GPU", subtitle: "")
            HStack(spacing: 14) {
                LabeledField(label: "项目", placeholder: "project-a", text: $projectID)
                LabeledField(label: "任务", placeholder: "training-042", text: $taskReference)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("GPU 数量")
                    .fieldLabel()
                TextField("1", text: $gpuCountText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 110)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("服务器")
                    .fieldLabel()
                ClaimEndpointPicker(
                    endpoints: store.snapshot.operationalEndpoints,
                    selection: $endpointID
                )
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
                    Button("申请") { submit() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                        .disabled(!store.allowsMutations || isSubmitting)
                    .help(store.allowsMutations ? "提交 GPU 申请" : store.mutationUnavailableReason)
                } else {
                    Button("完成") { dismiss() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(28)
        .frame(width: 640)
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
        guard !project.isEmpty, !task.isEmpty else {
            validationMessage = "请填写项目和任务。"
            return
        }
        validationMessage = nil
        submissionResult = nil
        isSubmitting = true
        store.submitClaim(
            ClaimDraft(
                projectID: project,
                taskReference: task,
                purpose: task,
                gpuCount: gpuCount,
                endpointID: endpointID,
                minimumCPUCores: nil,
                minimumMemoryMiB: nil,
                minimumTotalVRAMMiB: nil,
                minimumFreeVRAMMiB: nil
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

}

private struct ClaimEndpointPicker: View {
    let endpoints: [EndpointRecord]
    @Binding var selection: String

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 7) {
                option(
                    id: "",
                    title: "自动选择",
                    detail: "由 ServerPilot 选择可用服务器"
                )
                ForEach(endpoints) { endpoint in
                    option(
                        id: endpoint.id,
                        title: endpoint.sshCommand,
                        detail: endpoint.workspacePath ?? "工作区未设置"
                    )
                }
            }
            .padding(1)
        }
        .frame(maxHeight: 224)
        .accessibilityLabel("服务器")
        .accessibilityValue(selection.isEmpty ? "自动选择" : selectedEndpointDescription)
    }

    private var selectedEndpointDescription: String {
        guard let endpoint = endpoints.first(where: { $0.id == selection }) else { return "未选择" }
        return "\(endpoint.sshCommand)，工作区 \(endpoint.workspacePath ?? "未设置")"
    }

    private func option(id: String, title: String, detail: String) -> some View {
        let selected = selection == id
        return Button {
            selection = id
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(.system(size: 11.5, weight: .semibold, design: id.isEmpty ? .default : .monospaced))
                        .foregroundStyle(DesignTokens.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(detail)
                        .font(.system(size: 10.5, weight: .medium, design: id.isEmpty ? .default : .monospaced))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? DesignTokens.interaction.opacity(0.11) : DesignTokens.ink.opacity(0.025),
                in: RoundedRectangle(cornerRadius: 9, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .stroke(selected ? DesignTokens.interaction.opacity(0.35) : DesignTokens.surfaceStroke, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityValue("工作区 \(detail)，\(selected ? "已选择" : "未选择")")
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
            SheetTitle(icon: "person.crop.circle", title: "本机设置", subtitle: "这个标识只用于本机审计，不是用户账号。")
            VStack(alignment: .leading, spacing: 8) {
                Text("本机操作标识")
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
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: allocated ? "checkmark.circle.fill" : "hourglass")
                .foregroundStyle(allocated ? DesignTokens.success : DesignTokens.warning)
            Text(message)
                .foregroundStyle(DesignTokens.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .font(.system(size: 12, weight: .medium))
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            (allocated ? DesignTokens.success : DesignTokens.warning).opacity(0.11),
            in: RoundedRectangle(cornerRadius: 10, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke((allocated ? DesignTokens.success : DesignTokens.warning).opacity(0.30), lineWidth: 1)
        )
    }
}
