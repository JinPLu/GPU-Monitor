import Foundation

public enum FixtureSnapshotError: LocalizedError, Equatable, Sendable {
    case rejectedProductionState(URL)
    case outsideFixtureRoot(URL)
    case missing(URL)
    case invalid(URL)

    public var errorDescription: String? {
        switch self {
        case .rejectedProductionState(let url):
            return "桌面测试夹具拒绝读取项目 state 路径：\(url.path)"
        case .outsideFixtureRoot(let url):
            return "桌面测试夹具只能从 desktop/Fixtures 读取：\(url.path)"
        case .missing(let url):
            return "找不到桌面测试夹具：\(url.path)"
        case .invalid(let url):
            return "无法解析桌面测试夹具：\(url.path)"
        }
    }
}

public enum FixtureSnapshots {
    public static func resolve(
        _ value: String,
        fixturesRoot: URL,
        projectRoot: URL? = nil
    ) throws -> URL {
        let rawURL: URL
        if value.hasPrefix("/") {
            rawURL = URL(fileURLWithPath: value)
        } else if value.hasSuffix(".json") {
            rawURL = fixturesRoot.appendingPathComponent(value)
        } else {
            rawURL = fixturesRoot.appendingPathComponent("\(value).json")
        }
        let url = rawURL.standardizedFileURL.resolvingSymlinksInPath()
        let root = fixturesRoot.standardizedFileURL.resolvingSymlinksInPath()
        if let projectRoot {
            let state = projectRoot
                .standardizedFileURL
                .resolvingSymlinksInPath()
                .appendingPathComponent("state", isDirectory: true)
                .resolvingSymlinksInPath()
            if url.path == state.path || url.path.hasPrefix(state.path + "/") {
                throw FixtureSnapshotError.rejectedProductionState(url)
            }
        }
        if !url.path.hasPrefix(root.path + "/") && url.path != root.path {
            throw FixtureSnapshotError.outsideFixtureRoot(url)
        }
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw FixtureSnapshotError.missing(url)
        }
        return url
    }

    public static func load(from url: URL) throws -> BrokerSnapshot {
        let data = try Data(contentsOf: url)
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let envelope = object as? [String: Any],
            let stateData = envelope["data"] as? [String: Any],
            stateData["current"] is [String: Any],
            stateData["history"] is [String: Any]
        else {
            throw FixtureSnapshotError.invalid(url)
        }
        return BrokerSnapshot(envelope: envelope)
    }
}
