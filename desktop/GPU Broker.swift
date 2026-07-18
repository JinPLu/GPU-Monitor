import AppKit
import Darwin
import Foundation
import WebKit

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

final class DesktopAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let port = 8787
    private var window: NSWindow?
    private var webView: WKWebView?
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
        let contentRect = NSRect(x: 0, y: 0, width: 1280, height: 860)
        let createdWindow = NSWindow(
            contentRect: contentRect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        createdWindow.title = "GPU Broker"
        createdWindow.minSize = NSSize(width: 960, height: 640)
        createdWindow.center()
        createdWindow.delegate = self

        let view = WKWebView(frame: contentRect, configuration: WKWebViewConfiguration())
        view.autoresizingMask = [.width, .height]
        createdWindow.contentView = view
        window = createdWindow
        webView = view
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
                    self.webView?.load(URLRequest(url: self.baseURL))
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
        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        try process.run()
        process.waitUntilExit()
        let output = String(data: stdout.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let errors = String(data: stderr.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            throw DesktopError.commandFailed("初始化本机状态失败：\(errors.isEmpty ? output : errors)")
        }
        return output
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

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = DesktopAppDelegate()
application.delegate = delegate
application.run()
