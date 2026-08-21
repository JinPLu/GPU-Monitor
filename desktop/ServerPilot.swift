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
            guard info.capabilities.contains("endpoint_conflict_cleanup"),
                  info.capabilities.contains("endpoint_delete"),
                  info.capabilities.contains("operator_lease_release"),
                  info.capabilities.contains("telemetry_recent_averages") else {
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
        let captureDelay = TimeInterval(environment["SERVERPILOT_DESKTOP_SCREENSHOT_DELAY"] ?? "") ?? 0.5
        DispatchQueue.main.asyncAfter(deadline: .now() + max(captureDelay, 0)) {
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
    alert.messageText = conflict ? "确认任务已结束后清理记录？" : "释放这笔空闲占用？"
    alert.informativeText = "ServerPilot 会先重新采集这台服务器；只有确认这笔租约覆盖的 GPU 都没有运行中的进程时才会释放。此操作不会停止远端任务。"
    alert.addButton(withTitle: conflict ? "确认任务已结束后清理" : "释放空闲占用")
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

@discardableResult
private func confirmEndpointDelete(_ endpoint: EndpointRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "从 ServerPilot 移除这台服务器？"
    alert.informativeText = """
    \(endpoint.sshCommand)

    这会停止对本机的监控与协调，并移除本机关联记录，不会停止远端进程。
    """
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "从 ServerPilot 移除")
    alert.buttons.last?.hasDestructiveAction = true
    return alert.runModal() == .alertSecondButtonReturn
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

    private func openFixtureDetailIfRequested(endpointIDs: [String]) {
#if DEBUG || DESKTOP_FIXTURES
        guard
            selectedEndpointDetailID == nil,
            let requestedID = ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_SELECTED_ENDPOINT"],
            endpointIDs.contains(requestedID)
        else { return }
        selectedEndpointDetailID = requestedID
#endif
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
                EditServerSheet(store: store, endpoint: endpoint) {
                    selectedEndpointDetailID = nil
                    editingEndpointID = nil
                }
            }
        }
        .sheet(item: selectedGPUSelection) { selection in
            if let gpu = store.snapshot.gpu(id: selection.id) {
                GPUDetailSheet(gpu: gpu)
            }
        }
        .onAppear {
            openFixtureDetailIfRequested(endpointIDs: store.snapshot.endpoints.map(\.id))
        }
        .onChange(of: store.snapshot.endpoints.map(\.id)) { _, endpointIDs in
            openFixtureDetailIfRequested(endpointIDs: endpointIDs)
            if let selectedEndpointDetailID, !endpointIDs.contains(selectedEndpointDetailID) {
                self.selectedEndpointDetailID = nil
            }
            if let editingEndpointID, !endpointIDs.contains(editingEndpointID) {
                self.editingEndpointID = nil
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
        return "任务占用：\(runningLease.projectID) · \(task) · \(runningLease.gpuIDs.count) GPU。"
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
                            Text("数据采集间隔")
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(DesignTokens.mutedInk)
                            Spacer()
                            Picker(
                                "数据采集间隔",
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
                            .accessibilityLabel("数据采集间隔")
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
        case .gpuUtilization: return "GPU 利用率"
        case .gpuMemory: return "显存占用率"
        case .cpuLoad: return "CPU 负载"
        case .memory: return "内存占用率"
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
    let claimEndpoint: (String) -> Void
    let openEndpoint: (EndpointRecord) -> Void
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
        VStack(spacing: 0) {
            resourceSummary
            Divider().opacity(0.45)
            endpointTable
                .background(DesignTokens.surface)
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

                Label("资源指标：近 10 分钟均值", systemImage: "clock")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                    .help("GPU、显存、CPU 负载和内存占用率按最近 10 分钟的观测均值展示。点开服务器可查看当前观测。")

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

            GeometryReader { proxy in
                let compactLayout = EndpointTableLayout.usesCompactLayout(tableWidth: proxy.size.width)
                VStack(spacing: 0) {
                    EndpointTableHeader(
                        sort: sort,
                        direction: sortDirection,
                        compactLayout: compactLayout,
                        selectSort: selectSort
                    )
                        .padding(.horizontal, 16)
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
                                        compactLayout: compactLayout,
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
            let left = endpointOverviewGPUUtilizationFraction(endpoint: lhs, gpus: store.snapshot.gpus(for: lhs)) ?? -1
            let right = endpointOverviewGPUUtilizationFraction(endpoint: rhs, gpus: store.snapshot.gpus(for: rhs)) ?? -1
            comparison = compare(left, right)
        case .gpuMemory:
            let left = endpointOverviewGPUMemoryFraction(endpoint: lhs, gpus: store.snapshot.gpus(for: lhs)) ?? -1
            let right = endpointOverviewGPUMemoryFraction(endpoint: rhs, gpus: store.snapshot.gpus(for: rhs)) ?? -1
            comparison = compare(left, right)
        case .cpuLoad:
            comparison = compare(endpointOverviewCPULoadFraction(endpoint: lhs) ?? -1, endpointOverviewCPULoadFraction(endpoint: rhs) ?? -1)
        case .memory:
            comparison = compare(endpointOverviewMemoryFraction(endpoint: lhs) ?? -1, endpointOverviewMemoryFraction(endpoint: rhs) ?? -1)
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

private enum EndpointTableLayout {
    static let columnSpacing: CGFloat = 8
    static let serverWidth: CGFloat = 190
    static let assignmentMinimumWidth: CGFloat = 170
    static let gpuModelWidth: CGFloat = 184
    static let availabilityWidth: CGFloat = 80
    static let pressureWidth: CGFloat = 88
    static let disclosureWidth: CGFloat = 10
    static let rowHorizontalInset: CGFloat = 32

    static var standardContentMinimumWidth: CGFloat {
        serverWidth
            + assignmentMinimumWidth
            + gpuModelWidth
            + availabilityWidth
            + pressureWidth * 4
            + disclosureWidth
            + columnSpacing * 8
    }

    static func usesCompactLayout(tableWidth: CGFloat) -> Bool {
        tableWidth < standardContentMinimumWidth + rowHorizontalInset
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
        HStack(spacing: EndpointTableLayout.columnSpacing) {
                header("服务器", icon: "server.rack", column: .id, alignment: .leading)
                    .frame(width: EndpointTableLayout.serverWidth, alignment: .leading)
                header("项目 · 任务", icon: "folder", column: .assignment, alignment: .leading)
                    .frame(minWidth: EndpointTableLayout.assignmentMinimumWidth, maxWidth: .infinity, alignment: .leading)
                header("GPU 配置", icon: "square.stack.3d.up", column: .gpuModel, alignment: .leading)
                    .frame(width: EndpointTableLayout.gpuModelWidth, alignment: .leading)
                header("空闲", accessibilityTitle: "空闲 GPU", icon: "checkmark.circle", column: .availableGPU, alignment: .trailing)
                    .frame(width: EndpointTableLayout.availabilityWidth)
                header("GPU 利用率", icon: nil, column: .gpuUtilization, alignment: .trailing)
                    .frame(width: EndpointTableLayout.pressureWidth)
                header("显存占用率", icon: nil, column: .gpuMemory, alignment: .trailing)
                    .frame(width: EndpointTableLayout.pressureWidth)
                header("CPU 负载", icon: nil, column: .cpuLoad, alignment: .trailing)
                    .frame(width: EndpointTableLayout.pressureWidth)
                header("内存占用率", icon: nil, column: .memory, alignment: .trailing)
                    .frame(width: EndpointTableLayout.pressureWidth)
                Color.clear.frame(width: EndpointTableLayout.disclosureWidth, height: 10)
        }
        .frame(minWidth: EndpointTableLayout.standardContentMinimumWidth, maxWidth: .infinity, alignment: .leading)
    }

    private var compactHeader: some View {
        VStack(alignment: .leading, spacing: 7) {
            header("服务器", icon: "server.rack", column: .id, alignment: .leading)
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
                header("GPU 利用率", icon: "chart.bar.fill", column: .gpuUtilization, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("显存占用率", icon: "memorychip", column: .gpuMemory, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("CPU 负载", icon: "cpu", column: .cpuLoad, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                header("内存占用率", icon: "memorychip.fill", column: .memory, alignment: .leading)
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
                        .frame(minWidth: EndpointTableLayout.standardContentMinimumWidth, maxWidth: .infinity, alignment: .leading)
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
        .accessibilityValue("\(assignmentHelp)，GPU 配置 \(gpuModelSummary)，\(gpuCaption)，资源指标为近 10 分钟均值：CPU 负载 \(percentageLabel(cpuLoadPressure))，内存占用率 \(percentageLabel(memoryPressure))，GPU 利用率 \(percentageLabel(gpuPressure))")
    }

    private var standardRow: some View {
        HStack(alignment: .center, spacing: EndpointTableLayout.columnSpacing) {
            endpointTitle
                .frame(width: EndpointTableLayout.serverWidth, alignment: .leading)
            assignmentCell.frame(minWidth: EndpointTableLayout.assignmentMinimumWidth, maxWidth: .infinity, alignment: .leading)
            gpuModelCell.frame(width: EndpointTableLayout.gpuModelWidth, alignment: .leading)
            availabilitySummary
                .frame(width: EndpointTableLayout.availabilityWidth, alignment: .trailing)
            TablePressureCell(label: "GPU 利用率", fraction: gpuPressure).frame(width: EndpointTableLayout.pressureWidth)
            TablePressureCell(label: "显存占用率", fraction: gpuMemoryPressure).frame(width: EndpointTableLayout.pressureWidth)
            TablePressureCell(label: "CPU 负载", fraction: cpuLoadPressure).frame(width: EndpointTableLayout.pressureWidth)
            TablePressureCell(label: "内存占用率", fraction: memoryPressure).frame(width: EndpointTableLayout.pressureWidth)
            Image(systemName: "chevron.right")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(DesignTokens.mutedInk)
                .frame(width: EndpointTableLayout.disclosureWidth)
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
                CompactPressureLabel(label: "GPU 利用率", fraction: gpuPressure)
                    .frame(maxWidth: .infinity, alignment: .leading)
                CompactPressureLabel(label: "显存占用率", fraction: gpuMemoryPressure)
                    .frame(maxWidth: .infinity, alignment: .leading)
                CompactPressureLabel(label: "CPU 负载", fraction: cpuLoadPressure)
                    .frame(maxWidth: .infinity, alignment: .leading)
                CompactPressureLabel(label: "内存占用率", fraction: memoryPressure)
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
        endpointOverviewGPUUtilizationFraction(endpoint: endpoint, gpus: gpus)
    }

    private var gpuMemoryPressure: Double? {
        endpointOverviewGPUMemoryFraction(endpoint: endpoint, gpus: gpus)
    }

    private var cpuLoadPressure: Double? {
        endpointOverviewCPULoadFraction(endpoint: endpoint)
    }

    private var memoryPressure: Double? {
        endpointOverviewMemoryFraction(endpoint: endpoint)
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

    private var legacyWorkloadReviewGPUCount: Int {
        gpus.filter(gpuHasLegacyWorkloadProcessReview).count
    }

    private var nonWorkloadConflictGPUCount: Int {
        conflictedGPUCount - legacyWorkloadReviewGPUCount
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
            legacyWorkloadReviewGPUCount > 0
                ? "\(availabilityLabel)，\(legacyWorkloadReviewGPUCount) 张任务归属待核对"
                : (conflictedGPUCount > 0
                    ? "\(availabilityLabel)，\(conflictedGPUCount) 张 GPU 状态需要处理"
                : availabilityLabel
                )
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
        if nonWorkloadConflictGPUCount > 0 { return "不可分配" }
        if availableGPUCount > 0, legacyWorkloadReviewGPUCount > 0 {
            return "\(availableGPUCount) 张可申请 · \(legacyWorkloadReviewGPUCount) 张任务待核对"
        }
        if gpus.contains(where: { ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains($0.state) }) { return "任务占用" }
        if legacyWorkloadReviewGPUCount > 0 { return "任务归属待核对" }
        if gpus.contains(where: gpuNeedsAttention) { return "不可分配" }
        if endpointHighPressure(endpoint: endpoint, gpus: gpus) { return "压力较高" }
        return endpoint.monitorLabel
    }

    private var statusColor: Color {
        if !isSnapshotFresh { return DesignTokens.danger }
        switch endpoint.monitorStatus {
        case "ONLINE":
            return endpointRequiresAttention(endpoint: endpoint, gpus: gpus) || conflictedGPUCount > 0
                ? DesignTokens.warning
                : DesignTokens.success
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
                .frame(width: 48)
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
                    columns: [
                        GridItem(.flexible(minimum: 300), spacing: 12),
                        GridItem(.flexible(minimum: 300), spacing: 12),
                    ],
                    alignment: .leading,
                    spacing: 12
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
                    .frame(maxWidth: .infinity, minHeight: 178, alignment: .leading)
            } else {
#if canImport(Charts)
                chart
#else
                Text("当前系统没有 Swift Charts，使用文本降级。")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 178, alignment: .leading)
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
        .frame(height: 160)
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

private func endpointGPUModelSummary(_ gpus: [GPURecord]) -> String {
    guard !gpus.isEmpty else { return "无 GPU" }
    let names = Array(Set(gpus.map(\.name))).sorted()
    guard let first = names.first else { return "无 GPU" }
    return names.count == 1 ? first : "\(first) +\(names.count - 1) 类"
}

private func endpointOverviewGPUMemoryFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { $0.recentTelemetryAverage?.memoryFraction }
    guard !values.isEmpty else { return nil }
    return min(max(values.reduce(0, +) / Double(values.count), 0), 1)
}

private func endpointOverviewGPUUtilizationFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { $0.recentTelemetryAverage?.utilizationFraction }
    guard !values.isEmpty else { return nil }
    return min(max(values.reduce(0, +) / Double(values.count), 0), 1)
}

private func endpointOverviewCPULoadFraction(endpoint: EndpointRecord) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    return endpoint.recentTelemetryAverage?.cpuLoadFraction
}

private func endpointOverviewMemoryFraction(endpoint: EndpointRecord) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    return endpoint.recentTelemetryAverage?.memoryFraction
}

private func percentageLabel(_ value: Double?) -> String {
    value.map { "\(Int(($0 * 100).rounded()))%" } ?? "—"
}

private let endpointHighPressureThreshold = 0.85

private func endpointPressureFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    [
        endpointOverviewCPULoadFraction(endpoint: endpoint),
        endpointOverviewMemoryFraction(endpoint: endpoint),
        endpointOverviewGPUUtilizationFraction(endpoint: endpoint, gpus: gpus)
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

/// Older brokers can report a workload's ordinary worker replacement as a
/// process-attribution conflict.  That is distinct from a keeper or foreign
/// process conflict, both of which remain error/attention states.
private func gpuHasLegacyWorkloadProcessReview(_ gpu: GPURecord) -> Bool {
    let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
    return gpu.state == "CONFLICT"
        && gpu.stateReason == "lease/process attribution conflict"
        && gpu.keepalive.leaseID == nil
        && task?.isEmpty == false
}

private func gpuTaskObservationLabel(_ gpu: GPURecord) -> String? {
    let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
    if gpuHasLegacyWorkloadProcessReview(gpu) {
        if let task, !task.isEmpty {
            return "任务：\(task) · 观测到进程已更新"
        }
        return "任务已指派 · 观测到进程已更新"
    }
    if let task, !task.isEmpty, !gpu.isPubliclyAvailable {
        return "任务：\(task)"
    }
    if ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains(gpu.state) {
        return "观测到计算任务"
    }
    return nil
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
    if let publicStatus = gpu.publicStatus {
        return publicStatus
    }
    if gpuHasLegacyWorkloadProcessReview(gpu) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        return task.map { "任务占用 · \($0)" } ?? "任务占用 · 进程已更新"
    }
    if gpu.keepalive.state == "ERROR" {
        let reason = gpu.keepalive.reason.map(localizedStateReason) ?? "未知原因"
        return gpu.isPubliclyAvailable
            ? "可用 · 占卡异常：\(reason)"
            : "占卡异常：\(reason)"
    }
    if gpu.state == "AVAILABLE" {
        return gpu.keepalive.desired == "ON"
            ? "可用 · 占卡未运行"
            : "可用 · 未开启占卡"
    }
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

private func gpuVisualStateColor(_ gpu: GPURecord) -> Color {
    gpuHasLegacyWorkloadProcessReview(gpu) ? DesignTokens.warning : gpuStateColor(gpu.state)
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
    if reason == "bound workload process observed" || reason == "compute process observed on assigned GPU" {
        return "已指派任务，当前检测到计算进程"
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

private struct ServerDetailSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
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

    private var availableGPUCount: Int {
        guard let endpoint else { return 0 }
        guard endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter(\.isPubliclyAvailable).count
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
                        serverActions(endpoint)

                        if endpoint.monitorStatus != "ONLINE" {
                            DetailCallout(
                                icon: endpointStateIcon(endpoint.monitorStatus),
                                color: endpoint.monitorStatus == "PENDING" ? DesignTokens.warning : DesignTokens.danger,
                                message: endpoint.monitorDetail ?? endpoint.monitorLabel
                            )
                        }

                        DetailCallout(
                            icon: "folder",
                            color: endpoint.workspacePath == nil ? DesignTokens.warning : DesignTokens.interaction,
                            message: endpoint.workspacePath.map { "远端工作区：\($0)" } ?? "远端工作区未设置；申请资源后仍需先补齐路径。"
                        )

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
                        let legacyWorkloadReviewGPUCount = gpus.filter(gpuHasLegacyWorkloadProcessReview).count
                        if !reclaimableLeases.isEmpty {
                            ForEach(reclaimableLeases) { lease in
                                let conflict = conflictedLeases.contains(where: { $0.id == lease.id })
                                let legacyWorkloadReview = lease.gpuIDs.contains { gpuID in
                                    gpus.first(where: { $0.id == gpuID }).map(gpuHasLegacyWorkloadProcessReview) ?? false
                                }
                                let gpuCount = lease.gpuIDs.filter { gpuID in
                                    guard let state = gpus.first(where: { $0.id == gpuID })?.state else { return false }
                                    return conflict ? state == "CONFLICT" : ["HELD", "LEASED_IDLE"].contains(state)
                                }.count
                                let task = lease.taskReference ?? lease.purpose ?? "未命名任务"
                                DetailCallout(
                                    icon: conflict
                                        ? (legacyWorkloadReview ? "exclamationmark.shield.fill" : "exclamationmark.triangle.fill")
                                        : "clock.badge.exclamationmark",
                                    color: conflict
                                        ? (legacyWorkloadReview ? DesignTokens.warning : DesignTokens.danger)
                                        : DesignTokens.interaction,
                                    message: conflict
                                        ? (legacyWorkloadReview
                                            ? "有 \(gpuCount) 张 GPU 仍指派给 \(lease.projectID) · \(task)。当前观测到计算进程变更；worker 重启或替换不会被当作硬件故障，也不会自动释放或停止任务。它们暂不能申请，其余 \(availableGPUCount) 张仍可申请。如需更正实际任务-GPU 指派，请在“使用情况”中选择任务后调整 GPU 分配。"
                                            : "有 \(gpuCount) 张 GPU 的归属状态需要处理；它们暂不能申请，其余 \(availableGPUCount) 张仍可申请。请在“使用情况”中核对任务-GPU 指派，并根据实际任务决定改派或在任务结束后释放。")
                                        : "有 \(gpuCount) 张 GPU 仍被租约占用，但当前采集没有观察到进程；可确认后释放。",
                                    actionTitle: isMutating
                                        ? "处理中"
                                        : (conflict ? "任务结束后清理记录" : "释放空闲占用"),
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
                                icon: legacyWorkloadReviewGPUCount == conflictedGPUCount
                                    ? "exclamationmark.shield.fill"
                                    : "exclamationmark.triangle.fill",
                                color: legacyWorkloadReviewGPUCount == conflictedGPUCount
                                    ? DesignTokens.warning
                                    : DesignTokens.danger,
                                message: legacyWorkloadReviewGPUCount == conflictedGPUCount
                                    ? "有 \(conflictedGPUCount) 张 GPU 仍有任务归属待核对。任务运行中的 worker 可正常更换；请根据当前任务与显存、利用率观测判断。它们暂不能申请，其余 \(availableGPUCount) 张仍可申请；如需更正任务-GPU 指派，请在“使用情况”中调整分配。"
                                    : "有 \(conflictedGPUCount) 张 GPU 的状态需要人工处理；它们暂不能申请，其余 \(availableGPUCount) 张仍可申请。请在“使用情况”中核对任务-GPU 指派。"
                            )
                        }

                        if let error = store.errorMessage {
                            InlineValidation(message: error)
                        }

                        if !gpus.isEmpty {
                            ServerGPUMemoryStatusGrid(gpus: gpus)
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
        .frame(minWidth: 720, idealWidth: 1_040, maxWidth: 1_260, minHeight: 620, idealHeight: 800, maxHeight: 920)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .accessibilityLabel("服务器详情")
        .accessibilityValue(accessibilityValue)
        .onChange(of: store.snapshot.endpoints.map(\.id)) { _, endpointIDs in
            if !endpointIDs.contains(endpointID) {
                dismiss()
            }
        }
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
                serverOperations()
            }
            VStack(alignment: .leading, spacing: 10) {
                primaryServerActions(endpoint)
                serverOperations()
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

    @ViewBuilder
    private func serverOperations() -> some View {
        if store.supportsEndpointUpdate || store.supportsEndpointDelete {
            Menu {
                Button("编辑或移除服务器", systemImage: "slider.horizontal.3", action: edit)
                    .disabled(isMutating)
            } label: {
                Label("服务器操作", systemImage: "ellipsis.circle")
                    .font(.system(size: 12, weight: .semibold))
            }
            .menuStyle(.borderlessButton)
            .accessibilityLabel("服务器操作")
            .help("编辑或移除服务器")
        }
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

private enum GPUStatusSection: String, CaseIterable, Identifiable {
    case available
    case keepalive
    case busy
    case error

    var id: String { rawValue }

    var label: String {
        switch self {
        case .available: return "空闲"
        case .keepalive: return "占卡"
        case .busy: return "繁忙"
        case .error: return "错误"
        }
    }

    var icon: String {
        switch self {
        case .available: return "checkmark.circle.fill"
        case .keepalive: return "shield.fill"
        case .busy: return "bolt.fill"
        case .error: return "exclamationmark.triangle.fill"
        }
    }

    var tint: Color {
        switch self {
        case .available: return DesignTokens.success
        case .keepalive: return DesignTokens.interaction
        case .busy: return DesignTokens.warning
        case .error: return DesignTokens.danger
        }
    }
}

private struct ServerGPUMemoryStatusGrid: View {
    let gpus: [GPURecord]

    private func section(for gpu: GPURecord) -> GPUStatusSection {
        if gpuHasLegacyWorkloadProcessReview(gpu) { return .busy }
        if gpuNeedsAttention(gpu) { return .error }
        if gpu.keepalive.isActive { return .keepalive }
        if gpu.isPubliclyAvailable { return .available }
        return .busy
    }

    private var orderedGPUs: [GPURecord] {
        gpus.sorted { lhs, rhs in
            let lhsRank = GPUStatusSection.allCases.firstIndex(of: section(for: lhs)) ?? 0
            let rhsRank = GPUStatusSection.allCases.firstIndex(of: section(for: rhs)) ?? 0
            if lhsRank == rhsRank { return lhs.index < rhs.index }
            return lhsRank < rhsRank
        }
    }

    private func count(in section: GPUStatusSection) -> Int {
        gpus.filter { self.section(for: $0) == section }.count
    }

    private var accessibilityValue: String {
        let gpuStates = gpus
            .sorted { $0.index < $1.index }
            .map { gpu in
                let observation = gpuTaskObservationLabel(gpu).map { " · \($0)" } ?? ""
                return "GPU \(gpu.index) \(section(for: gpu).label)\(observation) 显存 \(gpuMemoryPercent(gpu))"
            }
            .joined(separator: "，")
        let counts = GPUStatusSection.allCases
            .map { "\($0.label) \(count(in: $0)) 张" }
            .joined(separator: "，")
        return "\(gpuStates)；\(counts)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Label("GPU 状态", systemImage: "memorychip")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Spacer(minLength: 0)
                Text("当前显存 · \(gpus.count) 张 GPU")
                    .font(.system(size: 10, weight: .medium, design: .rounded))
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            HStack(spacing: 7) {
                ForEach(GPUStatusSection.allCases) { section in
                    GPUStatusCountChip(section: section, count: count(in: section))
                }
                Spacer(minLength: 0)
            }

            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 235, maximum: 360), spacing: 10)],
                alignment: .leading,
                spacing: 10
            ) {
                ForEach(orderedGPUs) { gpu in
                    GPUMemoryStatusRow(gpu: gpu, status: section(for: gpu))
                }
            }
        }
        .padding(14)
        .background(DesignTokens.surface.opacity(0.70), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(DesignTokens.surfaceStroke.opacity(0.82), lineWidth: 1)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU 显存状态")
        .accessibilityValue(accessibilityValue)
        .help("每行代表一张 GPU；环图表示当前显存占用，状态标签表示可用状态。")
    }
}

private struct GPUStatusCountChip: View {
    let section: GPUStatusSection
    let count: Int

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(section.tint)
                .frame(width: 6, height: 6)
            Text(section.label)
            Text("\(count)")
                .fontWeight(.semibold)
                .foregroundStyle(count == 0 ? DesignTokens.mutedInk : DesignTokens.ink)
        }
        .font(.system(size: 10, weight: .medium, design: .rounded))
        .foregroundStyle(DesignTokens.mutedInk)
        .padding(.horizontal, 8)
        .frame(height: 24)
        .background(DesignTokens.ink.opacity(count == 0 ? 0.025 : 0.045), in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(section.label)
        .accessibilityValue("\(count) 张 GPU")
    }
}

private struct GPUMemoryStatusRow: View {
    let gpu: GPURecord
    let status: GPUStatusSection

    var body: some View {
        HStack(spacing: 11) {
            GPUMemoryGlyph(gpu: gpu, diameter: 40)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 7) {
                    Text("GPU \(gpu.index)")
                        .font(.system(size: 12, weight: .semibold, design: .rounded))
                        .foregroundStyle(DesignTokens.ink)
                    Label(status.label, systemImage: status.icon)
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(status.tint)
                        .padding(.horizontal, 7)
                        .frame(height: 20)
                        .background(status.tint.opacity(0.10), in: Capsule())
                }
                Text(gpu.memoryLabel)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                if let observation = gpuTaskObservationLabel(gpu) {
                    Text(observation)
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(status == .error ? DesignTokens.danger : DesignTokens.mutedInk)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }

            Spacer(minLength: 8)

            VStack(alignment: .trailing, spacing: 2) {
                Text(gpuMemoryPercent(gpu))
                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                    .foregroundStyle(memoryPressureColor(gpu))
                Text("显存")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        .padding(.horizontal, 12)
        .frame(maxWidth: .infinity, minHeight: 74, alignment: .leading)
        .background(DesignTokens.surface.opacity(0.92), in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .stroke(DesignTokens.surfaceStroke, lineWidth: 1)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU \(gpu.index)")
        .accessibilityValue("\(status.label)，\(gpuTaskObservationLabel(gpu) ?? "无任务指派")，当前显存 \(gpu.memoryLabel)，占用 \(gpuMemoryPercent(gpu))")
        .help("GPU \(gpu.index) · \(status.label) · \(gpuTaskObservationLabel(gpu) ?? "无任务指派") · 当前显存 \(gpu.memoryLabel)")
    }

    private func memoryPressureColor(_ gpu: GPURecord) -> Color {
        guard gpu.memoryUsedMiB != nil, gpu.totalVRAMMiB > 0 else { return DesignTokens.mutedInk }
        return pressureColor(gpu.memoryFraction)
    }
}

private struct GPUMemoryGlyph: View {
    let gpu: GPURecord
    let diameter: CGFloat

    private var memoryFraction: Double? {
        guard gpu.memoryUsedMiB != nil, gpu.totalVRAMMiB > 0 else { return nil }
        return min(max(gpu.memoryFraction, 0), 1)
    }

    private var memoryColor: Color {
        pressureColor(memoryFraction)
    }

    var body: some View {
        ZStack {
            Circle()
                .stroke(DesignTokens.ink.opacity(0.09), lineWidth: 4)

            if let memoryFraction {
                Circle()
                    .trim(from: 0, to: memoryFraction)
                    .stroke(memoryColor, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                Circle()
                    .fill(memoryColor.opacity(0.11))
                    .frame(width: diameter * 0.56, height: diameter * 0.56)
            } else {
                Circle()
                    .stroke(DesignTokens.mutedInk.opacity(0.32), style: StrokeStyle(lineWidth: 2, dash: [3, 3]))
            }

            Text("\(gpu.index)")
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(DesignTokens.ink)
        }
        .frame(width: diameter, height: diameter)
        .accessibilityHidden(true)
    }
}

private func gpuMemoryPercent(_ gpu: GPURecord) -> String {
    guard gpu.memoryUsedMiB != nil, gpu.totalVRAMMiB > 0 else { return "—" }
    return "\(Int((min(max(gpu.memoryFraction, 0), 1) * 100).rounded()))%"
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
            if gpuHasLegacyWorkloadProcessReview(gpu) {
                DetailCallout(
                    icon: "info.circle.fill",
                    color: DesignTokens.warning,
                    message: "任务仍保持 GPU 指派，采集到的计算进程与此前观测不同。worker 重启或替换会出现这一提示，并不表示 GPU 硬件故障；仅在实际任务-GPU 指派需要更正时，才到“使用情况”调整分配。"
                )
            } else if let reason = gpu.stateReason {
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

    private var stateLabel: String { gpuPresentationLabel(gpu) }

    private var stateIcon: String {
        if gpuHasLegacyWorkloadProcessReview(gpu) { return "bolt.fill" }
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "KEEPALIVE": return "shield.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        if gpuHasLegacyWorkloadProcessReview(gpu) { return DesignTokens.warning }
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

private struct AddServerSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var sshCommand = ""
    @State private var workspacePath = ""
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(icon: "server.rack", title: "添加服务器", subtitle: "")
            VStack(alignment: .leading, spacing: 8) {
                Text("SSH 指令")
                    .fieldLabel()
                TextField("ssh -p 22 gpu@node-a.example", text: $sshCommand)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13, weight: .medium, design: .monospaced))
                    .accessibilityLabel("SSH 指令")
            }
            LabeledField(
                label: "远端工作区路径",
                placeholder: "/srv/serverpilot-workspace",
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
    let onRemoved: () -> Void
    @State private var sshUser: String
    @State private var workspacePath: String
    @State private var observationProfile: EndpointObservationProfile
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    init(store: BrokerStore, endpoint: EndpointRecord, onRemoved: @escaping () -> Void = {}) {
        self.store = store
        self.endpoint = endpoint
        self.onRemoved = onRemoved
        _sshUser = State(initialValue: endpoint.sshUser)
        _workspacePath = State(initialValue: endpoint.workspacePath ?? "")
        _observationProfile = State(initialValue: EndpointObservationProfile(rawValueOrDefault: endpoint.observationProfile))
    }

    var body: some View {
        ScrollView {
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
                    placeholder: "/srv/serverpilot-workspace",
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
                if store.supportsEndpointDelete {
                    VStack(alignment: .leading, spacing: 10) {
                        Divider()
                        Text("危险操作")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(DesignTokens.danger)
                        Text("从本机控制面移除这台服务器。会停止监控与协调并删除本机关联记录，不会停止远端进程。")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("从 ServerPilot 移除…") { deleteServer() }
                            .buttonStyle(SoftButtonStyle(tint: DesignTokens.danger, foreground: DesignTokens.onInteraction))
                            .disabled(!store.allowsEndpointLifecycleMutations || isSubmitting)
                            .help(deleteHelp)
                            .accessibilityIdentifier("endpoint-delete-action")
                    }
                }
            }
            .padding(28)
        }
        .frame(width: 520, height: 560)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private var deleteHelp: String {
        guard store.allowsEndpointLifecycleMutations else {
            return store.endpointLifecycleMutationUnavailableReason
        }
        return "从本机控制面移除这台服务器；不会停止远端进程"
    }

    private func deleteServer() {
        guard confirmEndpointDelete(endpoint) else { return }
        validationMessage = nil
        isSubmitting = true
        store.deleteEndpoint(endpoint) { success, error in
            isSubmitting = false
            if success {
                onRemoved()
                dismiss()
            } else {
                validationMessage = error
            }
        }
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
