import AppKit
import Darwin
import Foundation
import SwiftUI

private enum DesktopError: LocalizedError {
    case projectRootMissing
    case uvMissing
    case serverExecutableMissing
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .projectRootMissing:
            return "找不到 gpu-broker 项目目录。请将 GPU Broker.app 保留在项目的 dist/ 目录，或设置 GPU_BROKER_ROOT。"
        case .uvMissing:
            return "找不到 uv。请先安装 uv，或设置 GPU_BROKER_UV 指向它的绝对路径。"
        case .serverExecutableMissing:
            return "初始化完成，但找不到项目虚拟环境中的 gpu-broker 可执行文件。"
        case .commandFailed(let details):
            return details
        }
    }
}

// MARK: - Native desktop shell

final class DesktopAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let port = 8787
    private let brokerStore = BrokerStore()
    private var window: NSWindow?
    private var serverProcess: Process?
    private var isStarting = false

    private lazy var projectRoot: URL? = {
        if let configured = ProcessInfo.processInfo.environment["GPU_BROKER_ROOT"], !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
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

        let contentRect = NSRect(x: 0, y: 0, width: 1440, height: 820)
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
        createdWindow.appearance = NSAppearance(named: .aqua)
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
        connectOrStartServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let process = serverProcess, process.isRunning {
            process.terminate()
        }
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

    private func uvExecutable() -> URL? {
        let environment = ProcessInfo.processInfo.environment
        let home = environment["HOME"] ?? NSHomeDirectory()
        let candidates = [
            environment["GPU_BROKER_UV"],
            "\(home)/.local/bin/uv",
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv"
        ].compactMap { $0 }
        return candidates
            .map { URL(fileURLWithPath: $0) }
            .first(where: { FileManager.default.isExecutableFile(atPath: $0.path) })
    }

    private func processEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if let root = projectRoot {
            environment["GPU_BROKER_PROJECT_ROOT"] = root.path
        }
        return environment
    }

    private func connectOrStartServer(attempt: Int = 0) {
        healthCheck { [weak self] ready in
            DispatchQueue.main.async {
                guard let self else { return }
                if ready {
                    self.brokerStore.connect(to: self.baseURL)
                    return
                }
                if self.serverProcess == nil && !self.isStarting {
                    self.initializeAndStartServer()
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

    private func healthCheck(completion: @escaping (Bool) -> Void) {
        DispatchQueue.global(qos: .utility).async { [port] in
            let descriptor = Darwin.socket(AF_INET, SOCK_STREAM, 0)
            guard descriptor >= 0 else {
                completion(false)
                return
            }
            defer { Darwin.close(descriptor) }

            var timeout = timeval(tv_sec: 0, tv_usec: 800_000)
            withUnsafePointer(to: &timeout) { pointer in
                _ = Darwin.setsockopt(
                    descriptor,
                    SOL_SOCKET,
                    SO_RCVTIMEO,
                    pointer,
                    socklen_t(MemoryLayout<timeval>.size)
                )
            }
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = in_port_t(port).bigEndian
            guard inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) == 1 else {
                completion(false)
                return
            }
            let connected = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            guard connected == 0 else {
                completion(false)
                return
            }

            let request = "GET /health/live HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
            let sent = request.utf8CString.withUnsafeBytes { bytes in
                Darwin.send(descriptor, bytes.baseAddress, bytes.count - 1, 0)
            }
            guard sent > 0 else {
                completion(false)
                return
            }
            var buffer = [UInt8](repeating: 0, count: 256)
            let received = buffer.withUnsafeMutableBytes { bytes in
                Darwin.recv(descriptor, bytes.baseAddress, bytes.count, 0)
            }
            guard received > 0 else {
                completion(false)
                return
            }
            let header = String(decoding: buffer.prefix(received), as: UTF8.self)
            completion(header.hasPrefix("HTTP/1.1 200") || header.hasPrefix("HTTP/1.0 200"))
        }
    }

    private func initializeAndStartServer() {
        guard let root = projectRoot else {
            showFatalError(DesktopError.projectRootMissing.localizedDescription)
            return
        }
        guard let uv = uvExecutable() else {
            showFatalError(DesktopError.uvMissing.localizedDescription)
            return
        }
        isStarting = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try self.runCommand(
                    executable: uv,
                    arguments: [
                        "run", "--no-editable", "--reinstall-package", "gpu-broker",
                        "gpu-broker", "init", "--db", "state/gpu-broker.sqlite3",
                        "--inventory", "configs/inventory.yaml"
                    ],
                    root: root
                )
                let serverExecutable = root.appendingPathComponent(".venv/bin/gpu-broker")
                guard FileManager.default.isExecutableFile(atPath: serverExecutable.path) else {
                    throw DesktopError.serverExecutableMissing
                }
                DispatchQueue.main.async {
                    do {
                        try self.startServer(executable: serverExecutable, root: root)
                        self.isStarting = false
                        self.connectOrStartServer()
                    } catch {
                        self.isStarting = false
                        self.showFatalError(error.localizedDescription)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.isStarting = false
                    self.showFatalError(error.localizedDescription)
                }
            }
        }
    }

    private func runCommand(executable: URL, arguments: [String], root: URL) throws -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = root
        process.environment = processEnvironment()
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let details = String(data: data, encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            throw DesktopError.commandFailed("初始化本机状态失败：\(details)")
        }
        return details
    }

    private func startServer(executable: URL, root: URL) throws {
        let process = Process()
        process.executableURL = executable
        process.arguments = [
            "serve", "--db", "state/gpu-broker.sqlite3",
            "--inventory", "configs/inventory.yaml", "--host", "127.0.0.1", "--port", "\(port)"
        ]
        process.currentDirectoryURL = root
        process.environment = processEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        serverProcess = process
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

private struct ResourceSummary {
    var onlineServers = 0
    var totalServers = 0
    var totalGPUs = 0
    var availableGPUs = 0
    var busyGPUs = 0
    var claimedGPUs = 0
    var abnormalGPUs = 0

    init(raw: [String: Any] = [:]) {
        onlineServers = raw.int("online_servers")
        totalServers = raw.int("total_servers")
        totalGPUs = raw.int("total_gpus")
        availableGPUs = raw.int("available_gpus")
        busyGPUs = raw.int("busy_gpus")
        claimedGPUs = raw.int("claimed_gpus")
        abnormalGPUs = raw.int("abnormal_gpus")
    }
}

private struct EndpointRecord: Identifiable {
    let id: String
    let host: String
    let port: Int
    let sshUser: String
    let sshAlias: String?
    let enabled: Bool
    let monitorStatus: String
    let monitorError: String?
    let cpuCount: Int?
    let load1m: Double?
    let memoryTotalMiB: Int?
    let memoryAvailableMiB: Int?

    init?(raw: [String: Any]) {
        guard let id = raw.string("id"), let host = raw.string("host"), let sshUser = raw.string("ssh_user") else {
            return nil
        }
        self.id = id
        self.host = host
        self.port = raw.int("port", default: 22)
        self.sshUser = sshUser
        self.sshAlias = raw.string("ssh_alias")
        self.enabled = raw.bool("enabled", default: true)
        let monitor = raw["monitor"] as? [String: Any] ?? [:]
        self.monitorStatus = monitor.string("status") ?? "PENDING"
        self.monitorError = monitor.string("last_error")
        let hostTelemetry = raw["host_telemetry"] as? [String: Any] ?? [:]
        self.cpuCount = hostTelemetry.optionalInt("cpu_count")
        self.load1m = hostTelemetry.optionalDouble("load_1m")
        self.memoryTotalMiB = hostTelemetry.optionalInt("memory_total_mib")
        self.memoryAvailableMiB = hostTelemetry.optionalInt("memory_available_mib")
    }

    var sshCommand: String {
        let target = sshAlias ?? "\(sshUser)@\(host)"
        return "ssh -p \(port) \(target)"
    }

    var monitorLabel: String {
        switch monitorStatus {
        case "ONLINE": return "在线"
        case "PENDING": return "等待采集"
        case "STALE": return "数据过期"
        case "ERROR": return "采集异常"
        case "DISABLED": return "已停用"
        default: return monitorStatus
        }
    }

    var cpuLoadFraction: Double? {
        guard monitorStatus == "ONLINE", let cpuCount, cpuCount > 0, let load1m else { return nil }
        return min(max(load1m / Double(cpuCount), 0), 1)
    }

    var memoryFraction: Double? {
        guard
            monitorStatus == "ONLINE",
            let memoryTotalMiB,
            memoryTotalMiB > 0,
            let memoryAvailableMiB
        else { return nil }
        return min(max(1 - Double(memoryAvailableMiB) / Double(memoryTotalMiB), 0), 1)
    }
}

private struct GPURecord: Identifiable {
    let id: String
    let endpointID: String
    let index: Int
    let name: String
    let totalVRAMMiB: Int
    let state: String
    let stateReason: String?
    let memoryUsedMiB: Int?
    let utilization: Int?
    let temperature: Int?
    let owner: String?
    let taskReference: String?

    init?(raw: [String: Any]) {
        guard
            let id = raw.string("id"),
            let endpointID = raw.string("endpoint_id"),
            let name = raw.string("name")
        else {
            return nil
        }
        self.id = id
        self.endpointID = endpointID
        self.index = raw.int("gpu_index")
        self.name = name
        self.totalVRAMMiB = raw.int("total_vram_mib")
        self.state = raw.string("state") ?? "UNKNOWN_RECOVERING"
        self.stateReason = raw.string("state_reason")
        let telemetry = raw["telemetry"] as? [String: Any] ?? [:]
        self.memoryUsedMiB = telemetry.optionalInt("memory_used_mib")
        self.utilization = telemetry.optionalInt("gpu_utilization_pct")
        self.temperature = telemetry.optionalInt("temperature_c")
        let lease = raw["lease"] as? [String: Any] ?? [:]
        self.owner = lease.string("actor_id")
        self.taskReference = lease.string("task_ref")
    }

    var memoryFraction: Double {
        guard let memoryUsedMiB, totalVRAMMiB > 0 else { return 0 }
        return min(max(Double(memoryUsedMiB) / Double(totalVRAMMiB), 0), 1)
    }

    var memoryLabel: String {
        guard let memoryUsedMiB else { return "等待采集" }
        return "\(memoryUsedMiB / 1024) / \(max(totalVRAMMiB / 1024, 1)) GB"
    }

    var vramLabel: String {
        "\(max(totalVRAMMiB / 1024, 1)) GB"
    }
}

private struct BrokerSnapshot {
    var summary: ResourceSummary
    var endpoints: [EndpointRecord]
    var gpus: [GPURecord]
    var dataAgeSeconds: Double?
    var admissionBoundary: String

    static let empty = BrokerSnapshot(
        summary: ResourceSummary(),
        endpoints: [],
        gpus: [],
        dataAgeSeconds: nil,
        admissionBoundary: "GPU Broker 仅协调资源归属，不授权启动工作负载。"
    )

    init(payload: [String: Any]) {
        summary = ResourceSummary(raw: payload["summary"] as? [String: Any] ?? [:])
        endpoints = (payload["endpoints"] as? [[String: Any]] ?? []).compactMap(EndpointRecord.init)
        gpus = (payload["gpus"] as? [[String: Any]] ?? []).compactMap(GPURecord.init)
        dataAgeSeconds = payload.optionalDouble("data_age_seconds")
        admissionBoundary = payload.string("admission_boundary") ?? BrokerSnapshot.empty.admissionBoundary
    }

    init(summary: ResourceSummary, endpoints: [EndpointRecord], gpus: [GPURecord], dataAgeSeconds: Double?, admissionBoundary: String) {
        self.summary = summary
        self.endpoints = endpoints
        self.gpus = gpus
        self.dataAgeSeconds = dataAgeSeconds
        self.admissionBoundary = admissionBoundary
    }

    func gpus(for endpoint: EndpointRecord) -> [GPURecord] {
        gpus.filter { $0.endpointID == endpoint.id }
    }
}

private extension Dictionary where Key == String, Value == Any {
    func string(_ key: String) -> String? {
        self[key] as? String
    }

    func int(_ key: String, default fallback: Int = 0) -> Int {
        optionalInt(key) ?? fallback
    }

    func optionalInt(_ key: String) -> Int? {
        if let value = self[key] as? Int { return value }
        if let value = self[key] as? NSNumber { return value.intValue }
        if let value = self[key] as? String { return Int(value) }
        return nil
    }

    func optionalDouble(_ key: String) -> Double? {
        if let value = self[key] as? Double { return value }
        if let value = self[key] as? NSNumber { return value.doubleValue }
        if let value = self[key] as? String { return Double(value) }
        return nil
    }

    func bool(_ key: String, default fallback: Bool) -> Bool {
        if let value = self[key] as? Bool { return value }
        if let value = self[key] as? NSNumber { return value.boolValue }
        return fallback
    }
}

private final class BrokerStore: ObservableObject {
    @Published private(set) var snapshot = BrokerSnapshot.empty
    @Published private(set) var isConnected = false
    @Published private(set) var isRefreshing = false
    @Published private(set) var lastUpdated: Date?
    @Published var actorID: String
    @Published var notice: String?
    @Published var errorMessage: String?

    private var baseURL: URL?
    private var refreshTimer: Timer?

    init() {
        actorID = UserDefaults.standard.string(forKey: "gpuBrokerActorID") ?? "human"
    }

    deinit {
        refreshTimer?.invalidate()
    }

    func connect(to baseURL: URL) {
        if self.baseURL != baseURL {
            self.baseURL = baseURL
            refreshTimer?.invalidate()
            refreshTimer = Timer.scheduledTimer(withTimeInterval: 12, repeats: true) { [weak self] _ in
                self?.reload()
            }
        }
        reload()
    }

    func reload() {
        guard let url = baseURL?.appendingPathComponent("api/v1/snapshot") else { return }
        isRefreshing = true
        var request = URLRequest(url: url)
        request.timeoutInterval = 6
        request.setValue(actorID, forHTTPHeaderField: "X-GPU-Broker-Actor")
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.isRefreshing = false
                if let error {
                    self.isConnected = false
                    self.errorMessage = "无法更新资源：\(error.localizedDescription)"
                    return
                }
                guard
                    let response = response as? HTTPURLResponse,
                    (200..<300).contains(response.statusCode),
                    let data,
                    let object = try? JSONSerialization.jsonObject(with: data),
                    let envelope = object as? [String: Any],
                    let payload = envelope["data"] as? [String: Any]
                else {
                    self.isConnected = false
                    self.errorMessage = "本机服务返回了无法读取的资源快照。"
                    return
                }
                self.snapshot = BrokerSnapshot(payload: payload)
                self.isConnected = true
                self.lastUpdated = Date()
                self.errorMessage = nil
            }
        }.resume()
    }

    func setActor(_ value: String) {
        let cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else {
            errorMessage = "操作者标识不能为空。"
            return
        }
        actorID = cleaned
        UserDefaults.standard.set(cleaned, forKey: "gpuBrokerActorID")
        notice = "已切换为操作者 \(cleaned)。"
        reload()
    }

    func submitClaim(_ draft: ClaimDraft, completion: @escaping (Bool) -> Void) {
        let project = draft.projectID.trimmingCharacters(in: .whitespacesAndNewlines)
        let task = draft.taskReference.trimmingCharacters(in: .whitespacesAndNewlines)
        let purpose = draft.purpose.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !project.isEmpty, !task.isEmpty, !purpose.isEmpty, draft.gpuCount > 0 else {
            errorMessage = "请完整填写项目、任务、用途和 GPU 数量。"
            completion(false)
            return
        }
        var constraints: [String: Any] = [
            "gpu_count": draft.gpuCount,
            "placement": "pack"
        ]
        if !draft.endpointID.isEmpty {
            constraints["endpoint_ids"] = [draft.endpointID]
        }
        performMutation(
            path: "api/v1/claims",
            payload: [
                "project_id": project,
                "task_ref": task,
                "purpose": purpose,
                "constraints": constraints
            ],
            successMessage: "GPU 已提交给 Broker 认领。",
            completion: completion
        )
    }

    func addEndpoint(_ draft: EndpointDraft, completion: @escaping (Bool) -> Void) {
        performMutation(
            path: "api/v1/endpoints",
            payload: [
                "id": draft.id,
                "host": draft.host,
                "port": draft.port,
                "ssh_user": draft.sshUser,
                "labels": ["desktop-app"],
                "enabled": true
            ],
            successMessage: "已登记服务器 \(draft.id)，等待只读采集确认状态。",
            completion: completion
        )
    }

    private func performMutation(
        path: String,
        payload: [String: Any],
        successMessage: String,
        completion: @escaping (Bool) -> Void
    ) {
        guard let url = baseURL?.appendingPathComponent(path) else {
            errorMessage = "本机服务尚未连接。"
            completion(false)
            return
        }
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            errorMessage = "无法编码提交内容。"
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpBody = body
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(actorID, forHTTPHeaderField: "X-GPU-Broker-Actor")
        request.setValue(UUID().uuidString, forHTTPHeaderField: "Idempotency-Key")

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                if let error {
                    self.errorMessage = "提交失败：\(error.localizedDescription)"
                    completion(false)
                    return
                }
                guard let response = response as? HTTPURLResponse else {
                    self.errorMessage = "提交失败：未收到有效响应。"
                    completion(false)
                    return
                }
                guard (200..<300).contains(response.statusCode) else {
                    self.errorMessage = "提交失败：\(self.apiErrorMessage(from: data) ?? "服务拒绝了此操作。")"
                    completion(false)
                    return
                }
                self.notice = successMessage
                self.errorMessage = nil
                self.reload()
                completion(true)
            }
        }.resume()
    }

    private func apiErrorMessage(from data: Data?) -> String? {
        guard
            let data,
            let object = try? JSONSerialization.jsonObject(with: data),
            let payload = object as? [String: Any]
        else {
            return nil
        }
        if let detail = payload["detail"] as? String { return detail }
        if let message = payload["message"] as? String { return message }
        if let error = payload["error"] as? String { return error }
        return nil
    }
}

private struct ClaimDraft {
    var projectID: String
    var taskReference: String
    var purpose: String
    var gpuCount: Int
    var endpointID: String
}

private struct EndpointDraft {
    let id: String
    let host: String
    let port: Int
    let sshUser: String

    init(command: String, suppliedID: String) throws {
        let parsed = try ParsedSSHCommand(command: command)
        let cleanedID = suppliedID.trimmingCharacters(in: .whitespacesAndNewlines)
        id = cleanedID.isEmpty ? Self.defaultID(host: parsed.host, port: parsed.port) : cleanedID
        host = parsed.host
        port = parsed.port
        sshUser = parsed.user
    }

    private static func defaultID(host: String, port: Int) -> String {
        let normalized = host.lowercased().map { character -> Character in
            character.isASCII && (character.isLetter || character.isNumber) ? character : "-"
        }
        let compact = String(normalized)
            .split(separator: "-", omittingEmptySubsequences: true)
            .joined(separator: "-")
        let base = compact.first?.isLetter == true ? compact : "server-\(compact)"
        return String("\(base)-p\(port)".prefix(120))
    }
}

private enum EndpointDraftError: LocalizedError {
    case invalidSSHCommand

    var errorDescription: String? {
        "请输入形如 ssh -p 2201 gpu@server.example.com 的 SSH 指令。"
    }
}

private struct ParsedSSHCommand {
    let host: String
    let port: Int
    let user: String

    init(command: String) throws {
        let parts = command
            .split(whereSeparator: { $0.isWhitespace })
            .map(String.init)
        guard parts.first == "ssh" else { throw EndpointDraftError.invalidSSHCommand }
        var port = 22
        var target: String?
        var index = 1
        while index < parts.count {
            let value = parts[index]
            if value == "-p", index + 1 < parts.count {
                guard let parsedPort = Int(parts[index + 1]), (1...65535).contains(parsedPort) else {
                    throw EndpointDraftError.invalidSSHCommand
                }
                port = parsedPort
                index += 2
                continue
            }
            if value.hasPrefix("-p"), value.count > 2 {
                guard let parsedPort = Int(value.dropFirst(2)), (1...65535).contains(parsedPort) else {
                    throw EndpointDraftError.invalidSSHCommand
                }
                port = parsedPort
            } else if !value.hasPrefix("-"), value.contains("@") {
                target = value
            }
            index += 1
        }
        guard
            let target,
            let separator = target.firstIndex(of: "@"),
            separator != target.startIndex,
            target.index(after: separator) != target.endIndex
        else {
            throw EndpointDraftError.invalidSSHCommand
        }
        let user = String(target[..<separator])
        let host = String(target[target.index(after: separator)...])
        guard Self.isValidUser(user), !host.isEmpty else {
            throw EndpointDraftError.invalidSSHCommand
        }
        self.host = host
        self.port = port
        self.user = user
    }

    private static func isValidUser(_ value: String) -> Bool {
        guard let first = value.unicodeScalars.first else { return false }
        let firstValid = CharacterSet.letters.union(CharacterSet(charactersIn: "_")).contains(first)
        guard firstValid else { return false }
        return value.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "_-")) .contains($0)
        }
    }
}

// MARK: - Apple Home inspired native interface

private struct NativeBrokerRoot: View {
    @ObservedObject var store: BrokerStore
    @State private var showAddServer = false
    @State private var showClaim = false
    @State private var showSettings = false
    @State private var selectedEndpointID = ""
    @State private var selectedGPU: GPURecord?
    @State private var selectedEndpoint: EndpointRecord?
    @State private var selectedDashboardSection: DashboardSection = .overview

    var body: some View {
        ZStack {
            AmbientBackground()
            HStack(spacing: 0) {
                AppSidebar(
                    store: store,
                    selectedSection: selectedDashboardSection,
                    navigate: { selectedDashboardSection = $0 },
                    addServer: { showAddServer = true },
                    claimGPU: {
                        selectedEndpointID = ""
                        showClaim = true
                    },
                    openSettings: { showSettings = true }
                )
                .frame(width: 246)

                Divider().overlay(Color.white.opacity(0.4))

                VStack(spacing: 0) {
                    AppToolbar(
                        store: store,
                        refresh: store.reload,
                        addServer: { showAddServer = true },
                        claimGPU: {
                            selectedEndpointID = ""
                            showClaim = true
                        }
                    )
                    DashboardView(
                        store: store,
                        refresh: store.reload,
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
                        selectedSection: $selectedDashboardSection,
                        selectGPU: { gpu in
                            selectedGPU = gpu
                        }
                    )
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(.ultraThinMaterial)
            }
            .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .stroke(Color.white.opacity(0.32), lineWidth: 1)
            )
            .padding(10)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .sheet(isPresented: $showAddServer) {
            AddServerSheet(store: store)
        }
        .sheet(isPresented: $showClaim) {
            ClaimSheet(store: store, initialEndpointID: selectedEndpointID)
        }
        .sheet(isPresented: $showSettings) {
            ActorSettingsSheet(store: store)
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
    let navigate: (DashboardSection) -> Void
    let addServer: () -> Void
    let claimGPU: () -> Void
    let openSettings: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .fill(DesignTokens.harborSlate.opacity(0.9))
                    Image(systemName: "square.3.layers.3d.top.filled")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(.white)
                }
                .frame(width: 36, height: 36)
                VStack(alignment: .leading, spacing: 2) {
                    Text("GPU Broker")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Text("本机资源控制面")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 30)
            .padding(.bottom, 25)

            Text("概览")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(DesignTokens.mutedInk)
                .padding(.horizontal, 18)
                .padding(.bottom, 8)

            SidebarSelection(title: "资源总览", systemImage: "square.grid.2x2.fill", selected: selectedSection == .overview) {
                navigate(.overview)
            }
            SidebarSelection(title: "服务器池", systemImage: "server.rack", selected: selectedSection == .serverPool) {
                navigate(.serverPool)
            }
            SidebarSelection(title: "租约状态", systemImage: "key.fill", selected: selectedSection == .leases) {
                navigate(.leases)
            }

            Text("快捷操作")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(DesignTokens.mutedInk)
                .padding(.horizontal, 18)
                .padding(.top, 26)
                .padding(.bottom, 8)

            SidebarAction(title: "添加服务器", systemImage: "plus", action: addServer)
            SidebarAction(title: "认领 GPU", systemImage: "checkmark.seal.fill", action: claimGPU)

            Spacer(minLength: 22)

            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 7) {
                    Circle()
                        .fill(store.isConnected ? DesignTokens.green : DesignTokens.amber)
                        .frame(width: 7, height: 7)
                    Text(store.isConnected ? "本机服务已连接" : "正在连接本机服务")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(DesignTokens.ink)
                }
                Text("操作者：\(store.actorID)")
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                Button(action: openSettings) {
                    Label("桌面设置", systemImage: "slider.horizontal.3")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.plain)
                .foregroundStyle(DesignTokens.mutedInk)
            }
            .padding(14)
            .background(Color.white.opacity(0.36), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .padding(16)
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(DesignTokens.fogBlue.opacity(0.82))
    }
}

private struct SidebarSelection: View {
    let title: String
    let systemImage: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 11) {
                Image(systemName: systemImage)
                    .font(.system(size: 14, weight: .semibold))
                    .frame(width: 18)
                Text(title)
                    .font(.system(size: 13, weight: selected ? .semibold : .medium))
                Spacer()
            }
            .foregroundStyle(selected ? .white : DesignTokens.ink)
            .padding(.horizontal, 15)
            .frame(height: 38)
            .background(selected ? DesignTokens.harborSlate.opacity(0.86) : .clear, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 10)
    }
}

private struct SidebarAction: View {
    let title: String
    let systemImage: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 11) {
                Image(systemName: systemImage)
                    .font(.system(size: 14, weight: .semibold))
                    .frame(width: 18)
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                Spacer()
            }
            .foregroundStyle(DesignTokens.ink)
            .padding(.horizontal, 15)
            .frame(height: 36)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 10)
    }
}

private struct AppToolbar: View {
    @ObservedObject var store: BrokerStore
    let refresh: () -> Void
    let addServer: () -> Void
    let claimGPU: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text("资源总览")
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(statusText)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Spacer()
            Button(action: refresh) {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 13, weight: .semibold))
                    .frame(width: 30, height: 30)
            }
            .buttonStyle(SoftIconButtonStyle())
            .help("刷新资源状态")
            .disabled(store.isRefreshing)

            Button(action: addServer) {
                Label("添加服务器", systemImage: "plus")
                    .font(.system(size: 13, weight: .semibold))
            }
            .buttonStyle(SoftButtonStyle(tint: DesignTokens.seaGlass, foreground: DesignTokens.ink))

            Button(action: claimGPU) {
                Label("认领 GPU", systemImage: "checkmark.seal.fill")
                    .font(.system(size: 13, weight: .semibold))
            }
            .buttonStyle(SoftButtonStyle(tint: DesignTokens.harborSlate, foreground: .white))
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 17)
        .background(Color.white.opacity(0.30))
    }

    private var statusText: String {
        if let lastUpdated = store.lastUpdated {
            let elapsed = max(0, Int(Date().timeIntervalSince(lastUpdated)))
            let updateLabel = elapsed < 5 ? "刚刚更新" : "\(elapsed) 秒前更新"
            return "本机实时资源 · \(updateLabel)"
        }
        return "正在连接本机 GPU Broker 服务"
    }
}

private struct DashboardView: View {
    @ObservedObject var store: BrokerStore
    let refresh: () -> Void
    let addServer: () -> Void
    let claimGPU: () -> Void
    let claimEndpoint: (String) -> Void
    let openEndpoint: (EndpointRecord) -> Void
    @Binding var selectedSection: DashboardSection
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if let error = store.errorMessage {
                        NoticeBanner(message: error, color: DesignTokens.coral, icon: "exclamationmark.triangle.fill")
                    } else if let notice = store.notice {
                        NoticeBanner(message: notice, color: DesignTokens.green, icon: "checkmark.circle.fill")
                    }

                    ComputeSpaceGallery(snapshot: store.snapshot)
                        .id(DashboardSection.overview)

                    QuickScenes(refresh: refresh, addServer: addServer, claimGPU: claimGPU)

                    ServerPool(snapshot: store.snapshot, claimEndpoint: claimEndpoint, openEndpoint: openEndpoint, selectGPU: selectGPU)
                        .id(DashboardSection.serverPool)

                    HStack(alignment: .top, spacing: 16) {
                        DataFreshnessCard(snapshot: store.snapshot)
                        CoordinationBoundaryCard(message: store.snapshot.admissionBoundary)
                    }
                    .id(DashboardSection.leases)
                }
                .padding(28)
                .padding(.bottom, 36)
            }
            .onChange(of: selectedSection) { _, section in
                withAnimation(.easeInOut(duration: 0.28)) {
                    proxy.scrollTo(section, anchor: .top)
                }
            }
        }
        .background(Color.white.opacity(0.15))
    }
}

private struct ComputeSpaceGallery: View {
    let snapshot: BrokerSnapshot

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .bottomLeading) {
                if let image = DesktopAssets.computeStudio {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFill()
                        .frame(width: proxy.size.width, height: proxy.size.height, alignment: .bottom)
                } else {
                    LinearGradient(
                        colors: [DesignTokens.harborSlate.opacity(0.86), DesignTokens.ink.opacity(0.94)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                    .frame(width: proxy.size.width, height: proxy.size.height)
                }

                LinearGradient(
                    colors: [Color.black.opacity(0.30), Color.black.opacity(0.10), Color.black.opacity(0.70)],
                    startPoint: .top,
                    endPoint: .bottom
                )
                .frame(width: proxy.size.width, height: proxy.size.height)

                VStack(alignment: .leading, spacing: 0) {
                    HStack(alignment: .top, spacing: 18) {
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(spacing: 7) {
                                Text("计算空间")
                                    .font(.system(size: 21, weight: .semibold))
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 12, weight: .bold))
                                    .foregroundStyle(Color.white.opacity(0.72))
                            }
                            Text("本机 GPU 资源总览")
                                .font(.system(size: 12, weight: .medium))
                                .foregroundStyle(Color.white.opacity(0.76))
                        }

                        Spacer(minLength: 20)

                        Text("\(snapshot.summary.onlineServers) / \(snapshot.summary.totalServers) 台服务器在线")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(Color.white.opacity(0.88))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(Color.black.opacity(0.30), in: Capsule())
                    }
                    .padding(24)

                    Spacer(minLength: 0)

                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 22) {
                            ComputeOverviewMetric(title: "可用", value: "\(snapshot.summary.availableGPUs)", icon: "checkmark.circle.fill", color: DesignTokens.green)
                            ComputeOverviewDivider()
                            ComputeOverviewMetric(title: "已认领", value: "\(snapshot.summary.claimedGPUs)", icon: "key.fill", color: DesignTokens.cyan)
                            ComputeOverviewDivider()
                            ComputeOverviewMetric(title: "运行中", value: "\(snapshot.summary.busyGPUs)", icon: "bolt.fill", color: DesignTokens.amber)
                            ComputeOverviewDivider()
                            ComputeOverviewMetric(title: "需处理", value: "\(snapshot.summary.abnormalGPUs)", icon: "exclamationmark.triangle.fill", color: DesignTokens.coral)
                            Spacer(minLength: 0)
                        }

                        VStack(alignment: .leading, spacing: 8) {
                            HStack(spacing: 18) {
                                ComputeOverviewMetric(title: "可用", value: "\(snapshot.summary.availableGPUs)", icon: "checkmark.circle.fill", color: DesignTokens.green)
                                ComputeOverviewMetric(title: "已认领", value: "\(snapshot.summary.claimedGPUs)", icon: "key.fill", color: DesignTokens.cyan)
                            }
                            HStack(spacing: 18) {
                                ComputeOverviewMetric(title: "运行中", value: "\(snapshot.summary.busyGPUs)", icon: "bolt.fill", color: DesignTokens.amber)
                                ComputeOverviewMetric(title: "需处理", value: "\(snapshot.summary.abnormalGPUs)", icon: "exclamationmark.triangle.fill", color: DesignTokens.coral)
                            }
                        }
                    }
                    .padding(.horizontal, 20)
                    .padding(.vertical, 14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.black.opacity(0.38))
                }
                .foregroundStyle(.white)
                .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
            }
        }
        .frame(height: 190)
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(Color.white.opacity(0.46), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.14), radius: 18, y: 9)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("计算空间，本机 GPU 资源总览，可用 \(snapshot.summary.availableGPUs)，已认领 \(snapshot.summary.claimedGPUs)，运行中 \(snapshot.summary.busyGPUs)，需处理 \(snapshot.summary.abnormalGPUs)，\(snapshot.summary.onlineServers) / \(snapshot.summary.totalServers) 台服务器在线")
    }
}

private struct ComputeOverviewMetric: View {
    let title: String
    let value: String
    let icon: String
    let color: Color

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 25, height: 25)
                .background(Color.white.opacity(0.18), in: Circle())
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Color.white.opacity(0.72))
                Text(value)
                    .font(.system(size: 20, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)
            }
        }
        .frame(minWidth: 86, alignment: .leading)
    }
}

private struct ComputeOverviewDivider: View {
    var body: some View {
        Rectangle()
            .fill(Color.white.opacity(0.22))
            .frame(width: 1, height: 34)
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
            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(DesignTokens.mutedInk)
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

private struct ComputePreviewTile: View {
    enum Emphasis {
        case primary
        case secondary
        case compact
    }

    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let emphasis: Emphasis
    let imageAlignment: Alignment

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            if let image = DesktopAssets.computeStudio {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFill()
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: imageAlignment)
            } else {
                DesignTokens.harborSlate
            }
            LinearGradient(
                colors: [Color.clear, Color.black.opacity(0.66)],
                startPoint: .top,
                endPoint: .bottom
            )
            VStack(alignment: .leading, spacing: 4) {
                Text(endpoint.sshCommand)
                    .font(.system(size: emphasis == .primary ? 13 : 11, weight: .semibold, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(resourceLabel)
                    .font(.system(size: emphasis == .compact ? 10 : 12, weight: .medium))
                    .foregroundStyle(Color.white.opacity(0.82))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, emphasis == .compact ? 8 : 10)
            .padding(.vertical, emphasis == .compact ? 6 : 8)
            .background(Color.black.opacity(0.42), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            .padding(emphasis == .compact ? 8 : 12)
        }
        .clipped()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(endpoint.sshCommand)，\(resourceLabel)")
    }

    private var resourceLabel: String {
        guard endpoint.monitorStatus == "ONLINE" else { return endpoint.monitorLabel }
        let available = gpus.filter { $0.state == "AVAILABLE" }.count
        return gpus.isEmpty ? "在线 · 等待 GPU 数据" : "\(available) / \(gpus.count) 块 GPU 可用"
    }
}

private struct ComputePreviewPlaceholder: View {
    let value: String
    let label: String
    let emphasis: ComputePreviewTile.Emphasis

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            LinearGradient(
                colors: [DesignTokens.harborSlate.opacity(0.86), DesignTokens.ink.opacity(0.94)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            VStack(alignment: .leading, spacing: 3) {
                Text(value)
                    .font(.system(size: emphasis == .primary ? 28 : 20, weight: .semibold, design: .rounded))
                Text(label)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Color.white.opacity(0.78))
            }
            .foregroundStyle(.white)
            .padding(emphasis == .compact ? 10 : 14)
        }
    }
}

private struct QuickScenes: View {
    let refresh: () -> Void
    let addServer: () -> Void
    let claimGPU: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HomeSectionTitle(title: "快捷操作")
            HStack(spacing: 10) {
                HomeSceneButton(
                    title: "认领 GPU",
                    subtitle: "创建资源租约",
                    systemImage: "key.fill",
                    tint: DesignTokens.cyan,
                    action: claimGPU
                )
                HomeSceneButton(
                    title: "刷新状态",
                    subtitle: "同步最新遥测",
                    systemImage: "arrow.clockwise",
                    tint: DesignTokens.green,
                    action: refresh
                )
                HomeSceneButton(
                    title: "添加服务器",
                    subtitle: "登记 SSH 端点",
                    systemImage: "plus",
                    tint: DesignTokens.amber,
                    action: addServer
                )
            }
        }
    }
}

private struct HomeSceneButton: View {
    let title: String
    let subtitle: String
    let systemImage: String
    let tint: Color
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: systemImage)
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(tint)
                    .frame(width: 34, height: 34)
                    .background(tint.opacity(0.15), in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.system(size: 13, weight: .semibold))
                    Text(subtitle)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Spacer(minLength: 0)
            }
            .foregroundStyle(DesignTokens.ink)
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity, minHeight: 62)
            .background(Color.white.opacity(hovering ? 0.78 : 0.58), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(Color.white.opacity(0.68), lineWidth: 1)
            )
            .shadow(color: Color.black.opacity(hovering ? 0.10 : 0.04), radius: hovering ? 10 : 4, y: 4)
        }
        .buttonStyle(.plain)
        .scaleEffect(hovering ? 1.008 : 1)
        .animation(.easeOut(duration: 0.2), value: hovering)
        .onHover { hovering = $0 }
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

private struct SummaryStrip: View {
    let summary: ResourceSummary

    var body: some View {
        HStack(spacing: 12) {
            SummaryPill(title: "可用", value: summary.availableGPUs, icon: "checkmark.circle.fill", color: DesignTokens.green)
            SummaryPill(title: "已认领", value: summary.claimedGPUs, icon: "key.fill", color: DesignTokens.cyan)
            SummaryPill(title: "运行中", value: summary.busyGPUs, icon: "bolt.fill", color: DesignTokens.amber)
            SummaryPill(title: "需处理", value: summary.abnormalGPUs, icon: "exclamationmark.triangle.fill", color: DesignTokens.coral)
            Spacer(minLength: 0)
            Text("\(summary.onlineServers) / \(summary.totalServers) 台服务器在线")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
        }
    }
}

private struct SummaryPill: View {
    let title: String
    let value: Int
    let icon: String
    let color: Color

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 28, height: 28)
                .background(color.opacity(0.14), in: Circle())
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                Text("\(value)")
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(DesignTokens.ink)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Color.white.opacity(0.54), in: Capsule())
        .overlay(Capsule().stroke(Color.white.opacity(0.62), lineWidth: 1))
    }
}

private struct ServerPool: View {
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
                    Text("每台机器的容量和状态")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Spacer()
                Text("\(snapshot.summary.totalGPUs) 个 GPU")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(DesignTokens.harborSlate)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(DesignTokens.seaGlass.opacity(0.85), in: Capsule())
            }

            if snapshot.endpoints.isEmpty {
                EmptyServerPool()
                    .background(Color.white.opacity(0.48), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 300, maximum: 430), spacing: 12)],
                    alignment: .leading,
                    spacing: 12
                ) {
                    ForEach(snapshot.endpoints) { endpoint in
                        ServerAccessoryCard(
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
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let claim: () -> Void
    let open: () -> Void
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

    private var isUnavailable: Bool {
        endpoint.monitorStatus == "ERROR" || endpoint.monitorStatus == "STALE" || endpoint.monitorStatus == "DISABLED"
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
                        Text("\(endpoint.id) · \(endpoint.monitorLabel)")
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
                    Text(gpus.isEmpty ? "等待数据" : "GPU 可用")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Button(action: open) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(DesignTokens.harborSlate)
                        .frame(width: 28, height: 28)
                        .background(DesignTokens.seaGlass.opacity(0.86), in: Circle())
                }
                .buttonStyle(.plain)
                .help("查看服务器详情")
            }

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 11) {
                ServerMetric(label: "平均 GPU 显存", value: averageMemoryFraction, tint: DesignTokens.cyan)
                ServerMetric(label: "平均 GPU 利用率", value: averageUtilizationFraction, tint: DesignTokens.amber)
                ServerMetric(label: "CPU 负载", value: endpoint.cpuLoadFraction, tint: DesignTokens.harborSlate, help: "1 分钟负载 ÷ CPU 核数，不代表 CPU busy 利用率")
                ServerMetric(label: "内存占用", value: endpoint.memoryFraction, tint: DesignTokens.green)
            }

            ServerLeaseSummary(gpus: gpus)

            VStack(alignment: .leading, spacing: 9) {
                if gpus.isEmpty {
                    Text(isUnavailable ? "遥测不可用，未显示历史数值" : "等待 GPU 遥测")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                } else {
                    LazyVGrid(columns: Array(repeating: GridItem(.fixed(28), spacing: 5), count: min(max(gpus.count, 1), 8)), spacing: 5) {
                        ForEach(gpus.sorted { $0.index < $1.index }) { gpu in
                            GPUUsageRing(gpu: gpu, diameter: 28, select: { selectGPU(gpu) })
                        }
                    }
                }
                HStack {
                    Text(gpus.isEmpty ? "暂无 GPU 详情" : "小环显示 GPU 利用率，点击查看详情")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    Button(action: claim) {
                        Label("认领", systemImage: "key.fill")
                            .font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(HomeClaimButtonStyle())
                    .help("仅在此服务器上申请 GPU")
                }
            }
        }
        .padding(16)
        .frame(minHeight: 218, alignment: .top)
        .background(cardBackground, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Color.white.opacity(hovering ? 0.88 : 0.68), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(hovering ? 0.13 : 0.06), radius: hovering ? 13 : 7, y: 5)
        .scaleEffect(hovering ? 1.006 : 1)
        .animation(.easeOut(duration: 0.2), value: hovering)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .contain)
    }

    private var statusColor: Color {
        switch endpoint.monitorStatus {
        case "ONLINE": return DesignTokens.green
        case "PENDING": return DesignTokens.amber
        case "ERROR", "STALE": return DesignTokens.coral
        default: return DesignTokens.mutedInk
        }
    }

    private var cardBackground: Color {
        if isUnavailable { return Color.white.opacity(0.42) }
        if endpoint.monitorStatus == "PENDING" { return DesignTokens.seaGlass.opacity(0.48) }
        return Color.white.opacity(0.62)
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
    if gpu.owner != nil { return true }
    return ["HELD", "LEASED_IDLE", "RUNNING_MANAGED", "BUSY_UNMANAGED", "CONFLICT"].contains(gpu.state)
}

private func gpuStateColor(_ state: String) -> Color {
    switch state {
    case "AVAILABLE": return DesignTokens.green
    case "HELD", "LEASED_IDLE": return DesignTokens.cyan
    case "RUNNING_MANAGED", "BUSY_UNMANAGED": return DesignTokens.amber
    default: return DesignTokens.coral
    }
}

private func gpuStateLabel(_ state: String) -> String {
    switch state {
    case "AVAILABLE": return "可用"
    case "HELD", "LEASED_IDLE": return "已认领"
    case "RUNNING_MANAGED": return "运行中"
    case "BUSY_UNMANAGED": return "非托管占用"
    case "DISABLED": return "已停用"
    case "MAINTENANCE": return "维护中"
    default: return "需处理"
    }
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
                .foregroundStyle(groups.isEmpty ? DesignTokens.mutedInk : DesignTokens.cyan)
                .frame(width: 24, height: 24)
                .background(Color.white.opacity(0.48), in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text("认领信息")
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
                    .foregroundStyle(DesignTokens.harborSlate)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Color.white.opacity(0.36), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var summaryText: String {
        guard !gpus.isEmpty else { return "等待 GPU 数据" }
        guard let first = groups.first else { return "无活跃租约" }
        let extra = groups.count > 1 ? "，+\(groups.count - 1)" : ""
        return "\(first.owner) · \(first.task) · \(first.count) GPU\(extra)"
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
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label) \(value.map { "\(Int(($0 * 100).rounded()))%" } ?? "无数据")")
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
            .background(Color.white.opacity(0.50), in: Capsule())
        }
        .buttonStyle(.plain)
        .help(gpuTooltip)
    }

    private var stateIcon: String {
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.green
        case "HELD", "LEASED_IDLE": return DesignTokens.cyan
        case "RUNNING_MANAGED", "BUSY_UNMANAGED": return DesignTokens.amber
        default: return DesignTokens.coral
        }
    }

    private var gpuTooltip: String {
        var details = "\(gpu.name) · \(gpu.vramLabel) · \(gpu.state)"
        if let owner = gpu.owner { details += " · \(owner)" }
        if let task = gpu.taskReference { details += " · \(task)" }
        return details
    }
}

private struct HomeClaimButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(DesignTokens.harborSlate)
            .padding(.horizontal, 11)
            .frame(height: 28)
            .background(DesignTokens.seaGlass.opacity(configuration.isPressed ? 0.62 : 0.90), in: Capsule())
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

private struct ServerDetailSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var selectedGPU: GPURecord?
    @State private var showClaim = false
    let endpoint: EndpointRecord
    let gpus: [GPURecord]

    private var availableGPUCount: Int {
        gpus.filter { $0.state == "AVAILABLE" }.count
    }

    private var claimedGPUCount: Int {
        gpus.filter(isGPUClaimed).count
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(
                icon: endpoint.monitorStatus == "ONLINE" ? "server.rack" : "exclamationmark.triangle.fill",
                title: "服务器详情",
                subtitle: endpoint.sshCommand
            )

            HStack(spacing: 12) {
                GPUDetailMetric(label: "GPU 可用", value: gpus.isEmpty ? "等待数据" : "\(availableGPUCount) / \(gpus.count)", accent: DesignTokens.green)
                GPUDetailMetric(label: "已认领", value: gpus.isEmpty ? "等待数据" : "\(claimedGPUCount) / \(gpus.count)", accent: DesignTokens.cyan)
                GPUDetailMetric(label: "平均利用率", value: percentageLabel(endpointAverageUtilizationFraction(endpoint: endpoint, gpus: gpus)), accent: DesignTokens.amber)
                GPUDetailMetric(label: "平均显存", value: percentageLabel(endpointAverageMemoryFraction(endpoint: endpoint, gpus: gpus)), accent: DesignTokens.harborSlate)
            }

            ServerLeaseSummary(gpus: gpus)

            VStack(alignment: .leading, spacing: 10) {
                Text("GPU 明细")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(DesignTokens.ink)

                if gpus.isEmpty {
                    DetailCallout(icon: "waveform.path.ecg", color: DesignTokens.amber, message: "等待采集 GPU 遥测。")
                } else {
                    ScrollView {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 10)], spacing: 10) {
                            ForEach(gpus.sorted { $0.index < $1.index }) { gpu in
                                ServerGPUDetailCard(gpu: gpu) {
                                    selectedGPU = gpu
                                }
                            }
                        }
                        .padding(.vertical, 1)
                    }
                    .frame(maxHeight: 310)
                }
            }

            HStack {
                Button("认领此服务器") { showClaim = true }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.harborSlate, foreground: .white))
                Spacer()
                Button("关闭") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(28)
        .frame(width: 720)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .sheet(item: $selectedGPU) { gpu in
            GPUDetailSheet(gpu: gpu)
        }
        .sheet(isPresented: $showClaim) {
            ClaimSheet(store: store, initialEndpointID: endpoint.id)
        }
    }
}

private struct ServerGPUDetailCard: View {
    let gpu: GPURecord
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 10) {
                GPUUsageGlyph(gpu: gpu, diameter: 34)
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
                    Text(detailLine)
                        .font(.system(size: 10, weight: .medium, design: .rounded))
                        .foregroundStyle(DesignTokens.ink.opacity(0.78))
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.48), in: RoundedRectangle(cornerRadius: 13, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 13, style: .continuous)
                    .stroke(Color.white.opacity(0.58), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .help("查看 GPU \(gpu.index) 详情")
    }

    private var detailLine: String {
        let utilization = gpu.utilization.map { "\($0)%" } ?? "等待采集"
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
        .foregroundStyle(.white)
        .padding(.horizontal, 18)
        .frame(height: 42)
        .background(DesignTokens.harborSlate.opacity(0.96))
        .clipShape(RoundedRectangle(cornerRadius: 15, style: .continuous))
        .padding(4)
    }
}

private struct EmptyServerPool: View {
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "server.rack")
                .font(.system(size: 22, weight: .medium))
                .foregroundStyle(DesignTokens.cyan)
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
                    Text("\(endpoint.id) · \(endpoint.monitorLabel)")
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
                    Text("等待采集 GPU 遥测")
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
                            .background(Color.white.opacity(0.48), in: Circle())
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: claim) {
                Image(systemName: "checkmark.seal.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(DesignTokens.harborSlate)
                    .frame(width: 32, height: 32)
            }
            .buttonStyle(.plain)
            .background(DesignTokens.seaGlass.opacity(0.8), in: Circle())
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
        case "ONLINE": return DesignTokens.green
        case "PENDING": return DesignTokens.amber
        case "ERROR", "STALE": return DesignTokens.coral
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
            Text(gpus.isEmpty ? "等待数据" : "GPU 可用")
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
            .background(Color.white.opacity(0.58), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(Color.white.opacity(0.64), lineWidth: 1)
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
        case "RUNNING_MANAGED", "BUSY_UNMANAGED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.green
        case "HELD", "LEASED_IDLE": return DesignTokens.cyan
        case "RUNNING_MANAGED", "BUSY_UNMANAGED": return DesignTokens.amber
        default: return DesignTokens.coral
        }
    }

    private var gpuTooltip: String {
        var details = "\(gpu.name) · \(gpu.vramLabel) · \(gpu.state)"
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
                GPUDetailMetric(label: "已用显存", value: gpu.memoryLabel, accent: DesignTokens.cyan)
                GPUDetailMetric(label: "计算利用率", value: utilizationLabel, accent: DesignTokens.amber)
                GPUDetailMetric(label: "温度", value: temperatureLabel, accent: DesignTokens.coral)
            }
            if let reason = gpu.stateReason {
                DetailCallout(icon: "info.circle.fill", color: stateColor, message: reason)
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
                .background(DesignTokens.seaGlass.opacity(0.64), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            HStack {
                Spacer()
                Button("关闭") { dismiss() }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.harborSlate, foreground: .white))
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(width: 560)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private var utilizationLabel: String {
        guard let value = gpu.utilization else { return "等待采集" }
        return "\(value)%"
    }

    private var temperatureLabel: String {
        guard let value = gpu.temperature else { return "等待采集" }
        return "\(value)°C"
    }

    private var stateLabel: String {
        switch gpu.state {
        case "AVAILABLE": return "可用"
        case "HELD", "LEASED_IDLE": return "已认领"
        case "RUNNING_MANAGED": return "运行中"
        case "BUSY_UNMANAGED": return "非托管占用"
        case "DISABLED": return "已停用"
        case "MAINTENANCE": return "维护中"
        default: return "需处理"
        }
    }

    private var stateIcon: String {
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.green
        case "HELD", "LEASED_IDLE": return DesignTokens.cyan
        case "RUNNING_MANAGED", "BUSY_UNMANAGED": return DesignTokens.amber
        default: return DesignTokens.coral
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
        .background(Color.white.opacity(0.56), in: RoundedRectangle(cornerRadius: 13, style: .continuous))
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
                .foregroundStyle(DesignTokens.cyan)
                .frame(width: 36, height: 36)
                .background(DesignTokens.cyan.opacity(0.12), in: Circle())
            VStack(alignment: .leading, spacing: 3) {
                Text("采集新鲜度")
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
        .background(DesignTokens.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private var freshnessLabel: String {
        guard let age = snapshot.dataAgeSeconds else { return "尚无 GPU 遥测数据" }
        return "最近数据约 \(Int(age.rounded())) 秒前"
    }
}

private struct CoordinationBoundaryCard: View {
    let message: String

    var body: some View {
        HStack(spacing: 11) {
            Image(systemName: "hand.raised.fill")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(DesignTokens.amber)
                .frame(width: 36, height: 36)
                .background(DesignTokens.amber.opacity(0.14), in: Circle())
            Text(message)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity)
        .background(DesignTokens.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
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
            SheetTitle(icon: "server.rack", title: "添加服务器", subtitle: "粘贴标准 SSH 指令，桌面端会登记受控端点。")
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
            Text("登记后仅等待固定只读 SSH 采集；不会启动、停止或变更远端工作负载。")
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
                Button("登记服务器") { submit() }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.harborSlate, foreground: .white))
                    .keyboardShortcut(.defaultAction)
                    .disabled(isSubmitting)
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
            store.addEndpoint(draft) { success in
                isSubmitting = false
                if success { dismiss() }
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
    @State private var endpointID: String
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    init(store: BrokerStore, initialEndpointID: String) {
        self.store = store
        self.initialEndpointID = initialEndpointID
        _endpointID = State(initialValue: initialEndpointID)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(icon: "checkmark.seal.fill", title: "认领 GPU", subtitle: "Broker 将原子地协调归属；排队不是运行许可。")
            HStack(spacing: 14) {
                LabeledField(label: "项目", placeholder: "project-a", text: $projectID)
                LabeledField(label: "任务", placeholder: "training-042", text: $taskReference)
            }
            LabeledField(label: "用途", placeholder: "说明本次需要的工作负载", text: $purpose)
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
                        Text("由 Broker 自动安排").tag("")
                        ForEach(store.snapshot.endpoints) { endpoint in
                            Text(endpoint.sshCommand).tag(endpoint.id)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: .infinity)
                }
            }
            if let validationMessage {
                InlineValidation(message: validationMessage)
            }
            HStack {
                Spacer()
                Button("取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("提交认领") { submit() }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.harborSlate, foreground: .white))
                    .keyboardShortcut(.defaultAction)
                    .disabled(isSubmitting)
            }
        }
        .padding(28)
        .frame(width: 570)
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
        isSubmitting = true
        store.submitClaim(
            ClaimDraft(
                projectID: project,
                taskReference: task,
                purpose: trimmedPurpose,
                gpuCount: gpuCount,
                endpointID: endpointID
            )
        ) { success in
            isSubmitting = false
            if success { dismiss() }
        }
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
            SheetTitle(icon: "person.crop.circle", title: "桌面设置", subtitle: "操作者标识用于本机审计与 REST 调用。")
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
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.harborSlate, foreground: .white))
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
                .foregroundStyle(DesignTokens.harborSlate)
                .frame(width: 42, height: 42)
                .background(DesignTokens.seaGlass, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
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
            .foregroundStyle(DesignTokens.coral)
    }
}
