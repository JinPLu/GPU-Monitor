import Foundation

public struct ServiceInfo: Equatable, Sendable {
    public let schemaVersion: String
    public let version: String?
    public let capabilities: Set<String>

    public init(schemaVersion: String, version: String? = nil, capabilities: Set<String>) {
        self.schemaVersion = schemaVersion
        self.version = version
        self.capabilities = capabilities
    }

    public init?(health: [String: Any]) {
        guard health.string("status") == "live", let schemaVersion = health.string("schema_version") else {
            return nil
        }
        self.schemaVersion = schemaVersion
        self.version = health.string("version")
        self.capabilities = Set((health["capabilities"] as? [String] ?? []).map { $0 })
    }

    public static let fixture = ServiceInfo(
        schemaVersion: "v1",
        version: "fixture",
        capabilities: ["instant_claims", "endpoint_deletion"]
    )

    public var supportsEndpointDeletion: Bool {
        capabilities.contains("endpoint_deletion") || capabilities.contains("server_deletion")
    }
}

public struct ResourceSummary: Equatable, Sendable {
    public var onlineServers = 0
    public var totalServers = 0
    public var totalGPUs = 0
    public var availableGPUs = 0
    public var busyGPUs = 0
    public var claimedGPUs = 0
    public var abnormalGPUs = 0
    public var attentionResources = 0

    public init(raw: [String: Any] = [:]) {
        onlineServers = raw.int("online_servers")
        totalServers = raw.int("total_servers")
        totalGPUs = raw.int("total_gpus")
        availableGPUs = raw.int("available_gpus")
        busyGPUs = raw.int("busy_gpus")
        claimedGPUs = raw.int("claimed_gpus")
        abnormalGPUs = raw.int("abnormal_gpus")
        let attention = raw["attention"] as? [String: Any] ?? [:]
        attentionResources = attention.int("total_resource_count", default: abnormalGPUs)
    }

    public init(
        onlineServers: Int = 0,
        totalServers: Int = 0,
        totalGPUs: Int = 0,
        availableGPUs: Int = 0,
        busyGPUs: Int = 0,
        claimedGPUs: Int = 0,
        abnormalGPUs: Int = 0,
        attentionResources: Int = 0
    ) {
        self.onlineServers = onlineServers
        self.totalServers = totalServers
        self.totalGPUs = totalGPUs
        self.availableGPUs = availableGPUs
        self.busyGPUs = busyGPUs
        self.claimedGPUs = claimedGPUs
        self.abnormalGPUs = abnormalGPUs
        self.attentionResources = attentionResources
    }
}

public struct EndpointRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let host: String
    public let port: Int
    public let sshUser: String
    public let sshAlias: String?
    public let enabled: Bool
    public let lifecycleState: String?
    public let monitorStatus: String
    public let monitorError: String?
    public let monitorLastSuccessAt: String?
    public let monitorLastAttemptAt: String?
    public let cpuCount: Int?
    public let load1m: Double?
    public let memoryTotalMiB: Int?
    public let memoryAvailableMiB: Int?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id"), let host = raw.string("host"), let sshUser = raw.string("ssh_user") else {
            return nil
        }
        self.id = id
        self.host = host
        self.port = raw.int("port", default: 22)
        self.sshUser = sshUser
        self.sshAlias = raw.string("ssh_alias")
        self.enabled = raw.bool("enabled", default: true)
        self.lifecycleState = raw.string("lifecycle_state")
        let monitor = raw["monitor"] as? [String: Any] ?? [:]
        self.monitorStatus = monitor.string("status") ?? "PENDING"
        self.monitorError = monitor.string("last_error")
        self.monitorLastSuccessAt = monitor.string("last_success_at")
        self.monitorLastAttemptAt = monitor.string("last_attempt_at")
        let hostTelemetry = raw["host_telemetry"] as? [String: Any] ?? [:]
        self.cpuCount = hostTelemetry.optionalInt("cpu_count")
        self.load1m = hostTelemetry.optionalDouble("load_1m")
        self.memoryTotalMiB = hostTelemetry.optionalInt("memory_total_mib")
        self.memoryAvailableMiB = hostTelemetry.optionalInt("memory_available_mib")
    }

    public var sshCommand: String {
        let target = sshAlias ?? "\(sshUser)@\(host)"
        return "ssh -p \(port) \(target)"
    }

    public var displayName: String {
        sshAlias ?? "\(sshUser)@\(host):\(port)"
    }

    public var monitorLabel: String {
        switch monitorStatus {
        case "ONLINE": return "在线"
        case "PENDING": return "等待状态"
        case "STALE": return "状态过期"
        case "ERROR": return "连接异常"
        case "DISABLED": return "已停用"
        case "DRAINING": return "排空中"
        case "RETIRED": return "已退役"
        default: return monitorStatus
        }
    }

    public var monitorDetail: String? {
        if let monitorError, !monitorError.isEmpty {
            let lowered = monitorError.lowercased()
            if lowered.contains("operation timed out") || lowered.contains("connection timed out") {
                return "连接超时，请检查服务器是否在线以及 SSH 端口是否可达"
            }
            if lowered.contains("connection refused") {
                return "连接被拒绝，请检查 SSH 服务和端口设置"
            }
            if lowered.contains("permission denied") || lowered.contains("authentication") {
                return "SSH 身份验证失败，请检查账号和密钥"
            }
            if lowered.contains("no route to host") || lowered.contains("network is unreachable") {
                return "当前网络无法到达这台服务器"
            }
            return "连接失败，请检查服务器和 SSH 设置"
        }
        if let monitorLastSuccessAt, !monitorLastSuccessAt.isEmpty {
            return "上次连接成功：\(monitorLastSuccessAt)"
        }
        if let monitorLastAttemptAt, !monitorLastAttemptAt.isEmpty {
            return "上次尝试连接：\(monitorLastAttemptAt)"
        }
        if lifecycleState == "DRAINING" || monitorStatus == "DRAINING" {
            return "这台服务器正在排空，不再接收新的分配。"
        }
        if lifecycleState == "RETIRED" || monitorStatus == "RETIRED" {
            return "这台服务器已退役，仅保留历史状态。"
        }
        return nil
    }

    public var cpuLoadFraction: Double? {
        guard monitorStatus == "ONLINE", let cpuCount, cpuCount > 0, let load1m else { return nil }
        return min(max(load1m / Double(cpuCount), 0), 1)
    }

    public var memoryFraction: Double? {
        guard
            monitorStatus == "ONLINE",
            let memoryTotalMiB,
            memoryTotalMiB > 0,
            let memoryAvailableMiB
        else { return nil }
        return min(max(1 - Double(memoryAvailableMiB) / Double(memoryTotalMiB), 0), 1)
    }
}

public struct GPURecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let endpointID: String
    public let gpuUUID: String?
    public let index: Int
    public let name: String
    public let totalVRAMMiB: Int
    public let state: String
    public let stateReason: String?
    public let memoryUsedMiB: Int?
    public let utilization: Int?
    public let temperature: Int?
    public let owner: String?
    public let taskReference: String?

    public init?(raw: [String: Any]) {
        guard
            let endpointID = raw.string("endpoint_id"),
            let name = raw.string("name")
        else {
            return nil
        }
        let uuid = raw.string("gpu_uuid")
        if let suppliedID = raw.string("id"), !suppliedID.isEmpty {
            self.id = suppliedID
        } else if let uuid, !uuid.isEmpty {
            self.id = "\(endpointID):\(uuid)"
        } else {
            return nil
        }
        self.endpointID = endpointID
        self.gpuUUID = uuid
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

    public var memoryFraction: Double {
        guard let memoryUsedMiB, totalVRAMMiB > 0 else { return 0 }
        return min(max(Double(memoryUsedMiB) / Double(totalVRAMMiB), 0), 1)
    }

    public var memoryLabel: String {
        guard let memoryUsedMiB else { return "等待状态" }
        return "\(memoryUsedMiB / 1024) / \(max(totalVRAMMiB / 1024, 1)) GB"
    }

    public var vramLabel: String {
        "\(max(totalVRAMMiB / 1024, 1)) GB"
    }

    public var uuidLabel: String {
        guard let gpuUUID, !gpuUUID.isEmpty else { return String(id.suffix(12)) }
        return String(gpuUUID.suffix(12))
    }
}

public struct BrokerSnapshot: Equatable, Sendable {
    public var schemaVersion: String?
    public var snapshotRevision: Int?
    public var serverTime: String?
    public var summary: ResourceSummary
    public var endpoints: [EndpointRecord]
    public var gpus: [GPURecord]
    public var leases: [LeaseRecord]
    public var requests: [AllocationRequestRecord]
    public var reservations: [ReservationRecord]
    public var dataAgeSeconds: Double?
    public var freshnessSeconds: Double?
    public var admissionBoundary: String

    public static let empty = BrokerSnapshot(
        schemaVersion: nil,
        snapshotRevision: nil,
        serverTime: nil,
        summary: ResourceSummary(),
        endpoints: [],
        gpus: [],
        leases: [],
        requests: [],
        reservations: [],
        dataAgeSeconds: nil,
        freshnessSeconds: nil,
        admissionBoundary: "这里只负责分配 GPU，不代表可以启动或停止远端任务。"
    )

    public init(envelope: [String: Any]) {
        let payload = envelope["data"] as? [String: Any] ?? envelope
        self.init(
            payload: payload,
            schemaVersion: envelope.string("schema_version"),
            snapshotRevision: envelope.optionalInt("snapshot_revision"),
            serverTime: envelope.string("server_time")
        )
    }

    public init(
        payload: [String: Any],
        schemaVersion: String? = nil,
        snapshotRevision: Int? = nil,
        serverTime: String? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.snapshotRevision = snapshotRevision
        self.serverTime = serverTime
        summary = ResourceSummary(raw: payload["summary"] as? [String: Any] ?? [:])
        endpoints = (payload["endpoints"] as? [[String: Any]] ?? []).compactMap(EndpointRecord.init)
        gpus = (payload["gpus"] as? [[String: Any]] ?? []).compactMap(GPURecord.init)
        let endpointAttention = endpoints.filter { ["ERROR", "STALE", "DRAINING", "RETIRED"].contains($0.monitorStatus) }.count
        let gpuAttentionStates = Set(["BUSY_UNMANAGED", "UNKNOWN_RECOVERING", "UNKNOWN_STALE", "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY", "DRAINING", "RETIRED"])
        let gpuAttention = gpus.filter { gpuAttentionStates.contains($0.state) }.count
        summary.attentionResources = max(summary.attentionResources, endpointAttention + gpuAttention)
        leases = (payload["leases"] as? [[String: Any]] ?? []).compactMap(LeaseRecord.init)
        requests = (payload["requests"] as? [[String: Any]] ?? []).compactMap(AllocationRequestRecord.init)
        reservations = (payload["reservations"] as? [[String: Any]] ?? []).compactMap(ReservationRecord.init)
        dataAgeSeconds = payload.optionalDouble("data_age_seconds")
        freshnessSeconds = payload.optionalDouble("freshness_seconds")
        admissionBoundary = payload.string("admission_boundary") ?? BrokerSnapshot.empty.admissionBoundary
    }

    public init(
        schemaVersion: String? = nil,
        snapshotRevision: Int? = nil,
        serverTime: String? = nil,
        summary: ResourceSummary,
        endpoints: [EndpointRecord],
        gpus: [GPURecord],
        leases: [LeaseRecord],
        requests: [AllocationRequestRecord],
        reservations: [ReservationRecord] = [],
        dataAgeSeconds: Double?,
        freshnessSeconds: Double? = nil,
        admissionBoundary: String
    ) {
        self.schemaVersion = schemaVersion
        self.snapshotRevision = snapshotRevision
        self.serverTime = serverTime
        self.summary = summary
        self.endpoints = endpoints
        self.gpus = gpus
        self.leases = leases
        self.requests = requests
        self.reservations = reservations
        self.dataAgeSeconds = dataAgeSeconds
        self.freshnessSeconds = freshnessSeconds
        self.admissionBoundary = admissionBoundary
    }

    public func gpus(for endpoint: EndpointRecord) -> [GPURecord] {
        gpus.filter { $0.endpointID == endpoint.id }
    }

    public func stableEndpointSelection(currentID: String) -> String {
        endpoints.contains { $0.id == currentID } ? currentID : (endpoints.first?.id ?? "")
    }

    public func stableLeaseSelection(currentID: String) -> String {
        leases.contains { $0.id == currentID } ? currentID : (leases.first?.id ?? "")
    }

    public func stableRequestSelection(currentID: String) -> String {
        requests.contains { $0.id == currentID } ? currentID : (requests.first?.id ?? "")
    }
}

public struct ReservationRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let projectID: String?
    public let actorID: String?
    public let gpuIDs: [String]
    public let startsAt: String?
    public let endsAt: String?
    public let state: String
    public let purpose: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id") else { return nil }
        self.id = id
        self.projectID = raw.string("project_id")
        self.actorID = raw.string("actor_id")
        self.gpuIDs = raw["gpu_ids"] as? [String] ?? []
        self.startsAt = raw.string("starts_at") ?? raw.string("start_time")
        self.endsAt = raw.string("ends_at") ?? raw.string("end_time")
        self.state = raw.string("state") ?? "ACTIVE"
        self.purpose = raw.string("purpose")
    }

    public var stateLabel: String {
        switch state {
        case "ACTIVE": return "生效中"
        case "PENDING": return "等待生效"
        case "EXPIRED": return "已过期"
        case "CANCELLED": return "已取消"
        default: return state
        }
    }
}

public struct LeaseRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let requestID: String?
    public let actorID: String
    public let projectID: String
    public let state: String
    public let gpuIDs: [String]
    public let issuedAt: String?
    public let expiresAt: String?
    public let taskReference: String?
    public let purpose: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id"), let actorID = raw.string("actor_id"), let projectID = raw.string("project_id") else {
            return nil
        }
        self.id = id
        self.requestID = raw.string("request_id")
        self.actorID = actorID
        self.projectID = projectID
        self.state = raw.string("state") ?? "UNKNOWN"
        self.gpuIDs = raw["gpu_ids"] as? [String] ?? []
        self.issuedAt = raw.string("issued_at")
        self.expiresAt = raw.string("expires_at")
        self.taskReference = raw.string("task_ref")
        self.purpose = raw.string("purpose")
    }

    public var stateLabel: String {
        switch state {
        case "ACTIVE": return "使用中"
        case "HELD": return "已保留"
        case "CONFLICT": return "需要处理"
        case "ORPHANED_BUSY": return "释放后仍占用"
        case "RELEASED": return "已释放"
        case "EXPIRED": return "已过期"
        default: return state
        }
    }
}

public struct AllocationRequestRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let actorID: String
    public let projectID: String
    public let taskReference: String
    public let purpose: String
    public let state: String
    public let blockedReason: String?
    public let gpuCount: Int
    public let createdAt: String?

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("id"),
            let actorID = raw.string("actor_id"),
            let projectID = raw.string("project_id"),
            let taskReference = raw.string("task_ref")
        else {
            return nil
        }
        self.id = id
        self.actorID = actorID
        self.projectID = projectID
        self.taskReference = taskReference
        self.purpose = raw.string("purpose") ?? ""
        self.state = raw.string("state") ?? "UNKNOWN"
        self.blockedReason = raw.string("blocked_reason")
        self.gpuCount = (raw["constraints"] as? [String: Any])?.int("gpu_count", default: 1) ?? 1
        self.createdAt = raw.string("created_at")
    }

    public var stateLabel: String {
        switch state {
        case "QUEUED": return "排队中"
        case "PENDING_APPROVAL": return "等待批准"
        case "ACTIVE": return "已分配"
        case "CANCELLED": return "已取消"
        case "RELEASED": return "已释放"
        default: return state
        }
    }
}

public struct ClaimSubmissionResult: Equatable, Sendable {
    public let allocated: Bool
    public let message: String

    public init(allocated: Bool, message: String) {
        self.allocated = allocated
        self.message = message
    }
}

public struct ClaimDraft: Equatable, Sendable {
    public var projectID: String
    public var taskReference: String
    public var purpose: String
    public var gpuCount: Int
    public var endpointID: String
    public var minimumCPUCores: Double?
    public var minimumMemoryMiB: Int?
    public var minimumTotalVRAMMiB: Int?
    public var minimumFreeVRAMMiB: Int?

    public init(
        projectID: String,
        taskReference: String,
        purpose: String,
        gpuCount: Int,
        endpointID: String,
        minimumCPUCores: Double? = nil,
        minimumMemoryMiB: Int? = nil,
        minimumTotalVRAMMiB: Int? = nil,
        minimumFreeVRAMMiB: Int? = nil
    ) {
        self.projectID = projectID
        self.taskReference = taskReference
        self.purpose = purpose
        self.gpuCount = gpuCount
        self.endpointID = endpointID
        self.minimumCPUCores = minimumCPUCores
        self.minimumMemoryMiB = minimumMemoryMiB
        self.minimumTotalVRAMMiB = minimumTotalVRAMMiB
        self.minimumFreeVRAMMiB = minimumFreeVRAMMiB
    }
}

public struct EndpointDraft: Equatable, Sendable {
    public let id: String
    public let host: String
    public let port: Int
    public let sshUser: String

    public init(command: String, suppliedID: String) throws {
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

public enum EndpointDraftError: LocalizedError, Equatable, Sendable {
    case invalidSSHCommand

    public var errorDescription: String? {
        "请输入形如 ssh -p 2201 gpu@server.example.com 的 SSH 指令。"
    }
}

public struct ParsedSSHCommand: Equatable, Sendable {
    public let host: String
    public let port: Int
    public let user: String

    public init(command: String) throws {
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
            CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "_-")).contains($0)
        }
    }
}

public extension Dictionary where Key == String, Value == Any {
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
