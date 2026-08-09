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

    public var supportsEndpointTelemetryHistory: Bool {
        capabilities.contains("endpoint_telemetry_history") || capabilities.contains("telemetry_history")
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

public struct ResourceQuantityRecord: Equatable, Sendable {
    public let cpuCores: Double
    public let memoryMiB: Int
    public let gpuCount: Int
    public let nodeCount: Int
    public let schedulerUnits: Int

    public init(raw: [String: Any] = [:]) {
        cpuCores = raw.optionalDouble("cpu_cores")
            ?? raw.optionalDouble("min_available_cpu_cores")
            ?? raw.optionalDouble("minimum_cpu_cores")
            ?? 0
        memoryMiB = raw.optionalInt("memory_mib")
            ?? raw.optionalInt("memory_mb")
            ?? raw.optionalInt("min_available_memory_mib")
            ?? raw.optionalInt("minimum_memory_mib")
            ?? 0
        gpuCount = raw.optionalInt("gpu_count")
            ?? raw.optionalInt("gpus")
            ?? 0
        nodeCount = raw.optionalInt("node_count")
            ?? raw.optionalInt("nodes")
            ?? 0
        schedulerUnits = raw.optionalInt("scheduler_units")
            ?? raw.optionalInt("scheduler_unit_count")
            ?? 0
    }

    public init(
        cpuCores: Double = 0,
        memoryMiB: Int = 0,
        gpuCount: Int = 0,
        nodeCount: Int = 0,
        schedulerUnits: Int = 0
    ) {
        self.cpuCores = cpuCores
        self.memoryMiB = memoryMiB
        self.gpuCount = gpuCount
        self.nodeCount = nodeCount
        self.schedulerUnits = schedulerUnits
    }

    public var compactLabel: String {
        var parts: [String] = []
        if cpuCores > 0 {
            let rounded = (cpuCores * 10).rounded() / 10
            let label = rounded == Double(Int(rounded)) ? "\(Int(rounded))" : String(format: "%.1f", rounded)
            parts.append("\(label) CPU")
        }
        if memoryMiB > 0 {
            parts.append("\(Self.gibibytes(memoryMiB)) GB RAM")
        }
        if gpuCount > 0 {
            parts.append("\(gpuCount) GPU")
        }
        if nodeCount > 0 {
            parts.append("\(nodeCount) 节点")
        }
        if schedulerUnits > 0 {
            parts.append("\(schedulerUnits) 调度单元")
        }
        return parts.isEmpty ? "无资源" : parts.joined(separator: " · ")
    }

    public static func availableHost(endpoint: EndpointRecord) -> ResourceQuantityRecord {
        ResourceQuantityRecord(
            cpuCores: endpoint.availableCPUCores ?? 0,
            memoryMiB: endpoint.memoryAvailableMiB ?? 0
        )
    }

    private static func gibibytes(_ mebibytes: Int) -> Int {
        Int((Double(mebibytes) / 1024).rounded())
    }
}

public struct ResourceProviderRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let providerType: String
    public let displayName: String
    public let endpointID: String?
    public let schedulerTargetID: String?
    public let state: String
    public let enabled: Bool
    public let total: ResourceQuantityRecord
    public let committed: ResourceQuantityRecord
    public let available: ResourceQuantityRecord
    public let ownerProjectID: String?
    public let freshnessSeconds: Double?
    public let updatedAt: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id") else { return nil }
        self.id = id
        providerType = raw.string("provider_type") ?? raw.string("type") ?? "direct-gpu"
        displayName = raw.string("display_name") ?? raw.string("name") ?? id
        endpointID = raw.string("endpoint_id")
        schedulerTargetID = raw.string("scheduler_target_id")
        state = (raw.string("state") ?? raw.string("lifecycle_state") ?? "UNKNOWN").uppercased()
        enabled = raw.bool("enabled", default: true)
        total = ResourceQuantityRecord(raw: raw["total"] as? [String: Any] ?? raw["capacity"] as? [String: Any] ?? [:])
        committed = ResourceQuantityRecord(raw: raw["committed"] as? [String: Any] ?? raw["commitment"] as? [String: Any] ?? [:])
        available = ResourceQuantityRecord(raw: raw["available"] as? [String: Any] ?? raw["free"] as? [String: Any] ?? [:])
        ownerProjectID = raw.string("owner_project_id")
        freshnessSeconds = raw.optionalDouble("freshness_seconds")
        updatedAt = raw.string("updated_at") ?? raw.string("observed_at")
    }

    public init(
        id: String,
        providerType: String,
        displayName: String,
        endpointID: String? = nil,
        schedulerTargetID: String? = nil,
        state: String,
        enabled: Bool = true,
        total: ResourceQuantityRecord = ResourceQuantityRecord(),
        committed: ResourceQuantityRecord = ResourceQuantityRecord(),
        available: ResourceQuantityRecord = ResourceQuantityRecord(),
        ownerProjectID: String? = nil,
        freshnessSeconds: Double? = nil,
        updatedAt: String? = nil
    ) {
        self.id = id
        self.providerType = providerType
        self.displayName = displayName
        self.endpointID = endpointID
        self.schedulerTargetID = schedulerTargetID
        self.state = state
        self.enabled = enabled
        self.total = total
        self.committed = committed
        self.available = available
        self.ownerProjectID = ownerProjectID
        self.freshnessSeconds = freshnessSeconds
        self.updatedAt = updatedAt
    }

    public var providerLabel: String {
        switch providerType {
        case "direct-gpu": return "GPU"
        case "host-capacity": return "CPU 与内存"
        case "scheduler": return "外部计算平台"
        default: return providerType
        }
    }

    public var stateLabel: String {
        switch state {
        case "ONLINE", "READY", "AVAILABLE": return "可用"
        case "PENDING", "QUEUED", "SUBMITTED": return "等待外部系统确认"
        case "ALLOCATED", "LEASED", "RUNNING": return "已分配"
        case "STALE": return "状态过期"
        case "ERROR", "CONFLICT": return "需要处理"
        case "DISABLED": return "已停用"
        case "DRAINING": return "排空中"
        case "RETIRED": return "已退役"
        default: return state
        }
    }

    public var trustBoundary: String? {
        if providerType == "scheduler", ["PENDING", "QUEUED", "SUBMITTED"].contains(state) {
            return "外部系统尚未确认，因此暂不计入可用资源。"
        }
        if !enabled { return "此资源已停用，暂时不能申请。" }
        if ["STALE", "ERROR", "CONFLICT", "DISABLED", "DRAINING", "RETIRED"].contains(state) {
            return "数据已过期或资源不可用，暂时不能申请。"
        }
        return nil
    }
}

public struct AllocatableUnitRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let providerID: String
    public let unitKey: String
    public let unitType: String
    public let endpointID: String?
    public let gpuID: String?
    public let schedulerTargetID: String?
    public let state: String
    public let quantities: ResourceQuantityRecord
    public let committed: ResourceQuantityRecord
    public let ownerProjectID: String?
    public let actorID: String?
    public let nativeSchedulerJobID: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id") else { return nil }
        self.id = id
        providerID = raw.string("provider_id") ?? raw.string("provider") ?? ""
        unitKey = raw.string("unit_key") ?? raw.string("key") ?? id
        unitType = raw.string("unit_type") ?? raw.string("type") ?? "gpu"
        endpointID = raw.string("endpoint_id")
        gpuID = raw.string("gpu_id")
        schedulerTargetID = raw.string("scheduler_target_id")
        state = (raw.string("state") ?? "UNKNOWN").uppercased()
        quantities = ResourceQuantityRecord(raw: raw["quantities"] as? [String: Any] ?? raw)
        committed = ResourceQuantityRecord(raw: raw["committed"] as? [String: Any] ?? raw["commitment"] as? [String: Any] ?? [:])
        ownerProjectID = raw.string("owner_project_id") ?? raw.string("project_id")
        actorID = raw.string("actor_id")
        nativeSchedulerJobID = raw.string("native_scheduler_job_id") ?? raw.string("scheduler_job_id")
    }
}

public struct SchedulerTargetRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let displayName: String
    public let kind: String
    public let adapter: String
    public let enabled: Bool
    public let accessStatus: String
    public let accessMessage: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id") else { return nil }
        self.id = id
        displayName = raw.string("display_name") ?? raw.string("name") ?? id
        kind = raw.string("kind") ?? "external-scheduler"
        adapter = raw.string("adapter") ?? ""
        enabled = raw.bool("enabled", default: true)
        let access = raw["last_access"] as? [String: Any] ?? [:]
        accessStatus = (access.string("status") ?? "unknown").uppercased()
        accessMessage = access.string("message")
    }
}

public struct SchedulerJobRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let targetID: String
    public let actorID: String
    public let projectID: String
    public let taskReference: String
    public let state: String
    public let rawState: String?
    public let schedulerJobID: String?

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("id"),
            let targetID = raw.string("target_id"),
            let actorID = raw.string("actor_id"),
            let projectID = raw.string("project_id")
        else {
            return nil
        }
        self.id = id
        self.targetID = targetID
        self.actorID = actorID
        self.projectID = projectID
        taskReference = raw.string("task_ref") ?? raw.string("task_reference") ?? ""
        state = (raw.string("state") ?? "UNKNOWN").uppercased()
        rawState = raw.string("raw_state")
        schedulerJobID = raw.string("scheduler_job_id")
    }
}

public struct SchedulerTransferRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let targetID: String
    public let actorID: String
    public let projectID: String
    public let state: String
    public let remoteDirectory: String?
    public let remoteStagedPath: String?
    public let errorMessage: String?

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("id"),
            let targetID = raw.string("target_id"),
            let actorID = raw.string("actor_id"),
            let projectID = raw.string("project_id")
        else {
            return nil
        }
        self.id = id
        self.targetID = targetID
        self.actorID = actorID
        self.projectID = projectID
        state = (raw.string("state") ?? "UNKNOWN").uppercased()
        remoteDirectory = raw.string("remote_directory")
        remoteStagedPath = raw.string("remote_staged_path")
        errorMessage = raw.string("error_message")
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
        self.lifecycleState = raw.string("lifecycle_state")?.uppercased()
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

    public var isRetired: Bool {
        lifecycleState == "RETIRED" || monitorStatus == "RETIRED"
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

    public var availableCPUCores: Double? {
        guard monitorStatus == "ONLINE", let cpuCount, cpuCount > 0, let load1m else { return nil }
        return max(0, Double(cpuCount) - load1m)
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

public struct BrokerStateHistory: Equatable, Sendable {
    public let retiredEndpoints: [EndpointRecord]
    public let resourcePlanEvaluations: [ResourcePlanEvaluationRecord]
    public let resourceRunActuals: [ResourceRunActualRecord]

    public static let empty = BrokerStateHistory(raw: [:])

    public init(raw: [String: Any]) {
        retiredEndpoints = (raw["retired_endpoints"] as? [[String: Any]] ?? []).compactMap(EndpointRecord.init)
        resourcePlanEvaluations = (raw["resource_plan_evaluations"] as? [[String: Any]] ?? [])
            .compactMap(ResourcePlanEvaluationRecord.init)
        resourceRunActuals = (raw["resource_run_actuals"] as? [[String: Any]] ?? [])
            .compactMap(ResourceRunActualRecord.init)
    }
}

public enum EndpointTelemetryRange: String, CaseIterable, Identifiable, Sendable {
    case oneHour = "1h"
    case sixHours = "6h"
    case twentyFourHours = "24h"

    public var id: String { rawValue }

    public var windowSeconds: Int {
        switch self {
        case .oneHour:
            3_600
        case .sixHours:
            21_600
        case .twentyFourHours:
            86_400
        }
    }
}

public struct EndpointTelemetrySample: Identifiable, Equatable, Sendable {
    public var id: String { timestamp }
    public let timestamp: String
    public let cpuLoadFraction: Double?
    public let memoryFraction: Double?
    public let gpuUtilizationFraction: Double?
    public let gpuMemoryFraction: Double?
    public let status: String?

    public init?(raw: [String: Any]) {
        guard let timestamp = raw.string("timestamp") ?? raw.string("observed_at") ?? raw.string("server_time") else {
            return nil
        }
        self.timestamp = timestamp
        let cpuCount = raw.optionalDouble("cpu_count")
        let load1m = raw.optionalDouble("load_1m")
        cpuLoadFraction = raw.optionalFraction("cpu_load_fraction")
            ?? raw.optionalFraction("cpu_fraction")
            ?? raw.optionalFraction("load_fraction")
            ?? raw.optionalPercent("cpu_utilization_pct")
            ?? (cpuCount.flatMap { count in
                guard count > 0, let load1m else { return nil }
                return min(max(load1m / count, 0), 1)
            })
        memoryFraction = raw.optionalFraction("memory_fraction")
            ?? raw.optionalFraction("memory_used_fraction")
            ?? raw.optionalPercent("memory_used_pct")
        gpuUtilizationFraction = raw.optionalFraction("gpu_utilization_fraction")
            ?? raw.optionalPercent("gpu_utilization_pct")
        gpuMemoryFraction = raw.optionalFraction("gpu_memory_fraction")
            ?? raw.optionalFraction("vram_fraction")
            ?? raw.optionalFraction("gpu_memory_used_fraction")
        status = raw.string("status")?.uppercased()
    }
}

public struct EndpointGPUHistorySample: Identifiable, Equatable, Sendable {
    public var id: String { timestamp }
    public let timestamp: String
    public let gpuUtilizationFraction: Double?
    public let memoryFraction: Double?
    public let memoryUsedMiB: Double?
    public let memoryTotalMiB: Double?

    public init?(raw: [String: Any]) {
        guard let timestamp = raw.string("timestamp") ?? raw.string("observed_at") else {
            return nil
        }
        self.timestamp = timestamp
        gpuUtilizationFraction = raw.optionalFraction("gpu_utilization_fraction")
            ?? raw.optionalPercent("gpu_utilization_pct")
        memoryFraction = raw.optionalFraction("memory_fraction")
            ?? raw.optionalPercent("memory_used_pct")
        memoryUsedMiB = raw.optionalDouble("memory_used_mib")
        memoryTotalMiB = raw.optionalDouble("memory_total_mib")
    }
}

public struct EndpointGPUHistorySeries: Identifiable, Equatable, Sendable {
    public let id: String
    public let gpuUUID: String
    public let index: Int
    public let label: String
    public let samples: [EndpointGPUHistorySample]

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("gpu_id"),
            let gpuUUID = raw.string("gpu_uuid"),
            let index = raw.optionalInt("gpu_index")
        else {
            return nil
        }
        self.id = id
        self.gpuUUID = gpuUUID
        self.index = index
        label = raw.string("label") ?? "GPU \(index)"
        let payload = raw["points"] as? [[String: Any]]
            ?? raw["samples"] as? [[String: Any]]
            ?? []
        samples = payload.compactMap(EndpointGPUHistorySample.init)
    }
}

public struct EndpointTelemetryHistory: Equatable, Sendable {
    public let endpointID: String
    public let range: EndpointTelemetryRange
    public let samples: [EndpointTelemetrySample]
    public let gpuSeries: [EndpointGPUHistorySeries]
    public let generatedAt: String?

    public static func empty(endpointID: String, range: EndpointTelemetryRange) -> EndpointTelemetryHistory {
        EndpointTelemetryHistory(endpointID: endpointID, range: range, samples: [], gpuSeries: [], generatedAt: nil)
    }

    public init(
        endpointID: String,
        range: EndpointTelemetryRange,
        samples: [EndpointTelemetrySample],
        gpuSeries: [EndpointGPUHistorySeries] = [],
        generatedAt: String?
    ) {
        self.endpointID = endpointID
        self.range = range
        self.samples = samples
        self.gpuSeries = gpuSeries
        self.generatedAt = generatedAt
    }

    public init(endpointID: String, range: EndpointTelemetryRange, envelope: [String: Any]) {
        let data = envelope["data"] as? [String: Any] ?? envelope
        let samplePayload = data["points"] as? [[String: Any]]
            ?? data["samples"] as? [[String: Any]]
            ?? data["history"] as? [[String: Any]]
            ?? envelope["samples"] as? [[String: Any]]
            ?? []
        self.endpointID = data.string("endpoint_id") ?? endpointID
        self.range = EndpointTelemetryRange.allCases.first {
            $0.windowSeconds == data.optionalInt("window_seconds")
        } ?? EndpointTelemetryRange(rawValue: data.string("range") ?? range.rawValue) ?? range
        self.samples = samplePayload.compactMap(EndpointTelemetrySample.init)
        self.gpuSeries = (data["gpu_series"] as? [[String: Any]] ?? []).compactMap(EndpointGPUHistorySeries.init)
        self.generatedAt = envelope.string("server_time") ?? data.string("generated_at") ?? data.string("server_time")
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
    public var resourceProviders: [ResourceProviderRecord]
    public var allocatableUnits: [AllocatableUnitRecord]
    public var schedulerTargets: [SchedulerTargetRecord]
    public var schedulerJobs: [SchedulerJobRecord]
    public var schedulerTransfers: [SchedulerTransferRecord]
    public var resourceClaims: [ResourceClaimRecord]
    public var resourcePlanEvaluations: [ResourcePlanEvaluationRecord]
    public var resourceRunActuals: [ResourceRunActualRecord]
    public var history: BrokerStateHistory
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
        resourceProviders: [],
        allocatableUnits: [],
        schedulerTargets: [],
        schedulerJobs: [],
        schedulerTransfers: [],
        resourceClaims: [],
        resourcePlanEvaluations: [],
        resourceRunActuals: [],
        history: .empty,
        dataAgeSeconds: nil,
        freshnessSeconds: nil,
        admissionBoundary: "GPU Broker 只协调资源，不执行服务器上的任务。"
    )

    public var operationalEndpoints: [EndpointRecord] {
        endpoints.filter { !$0.isRetired }
    }

    public var operationalGPUs: [GPURecord] {
        let endpointIDs = Set(operationalEndpoints.map(\.id))
        return gpus.filter { endpointIDs.contains($0.endpointID) }
    }

    public init(envelope: [String: Any]) {
        let stateData = envelope["data"] as? [String: Any]
        let payload = stateData?["current"] as? [String: Any]
            ?? stateData
            ?? envelope
        self.init(
            payload: payload,
            schemaVersion: envelope.string("schema_version"),
            snapshotRevision: envelope.optionalInt("snapshot_revision"),
            serverTime: envelope.string("server_time"),
            history: BrokerStateHistory(raw: stateData?["history"] as? [String: Any] ?? [:])
        )
    }

    public init(
        payload: [String: Any],
        schemaVersion: String? = nil,
        snapshotRevision: Int? = nil,
        serverTime: String? = nil,
        history: BrokerStateHistory = .empty
    ) {
        self.schemaVersion = schemaVersion
        self.snapshotRevision = snapshotRevision
        self.serverTime = serverTime
        self.history = history
        summary = ResourceSummary(raw: payload["summary"] as? [String: Any] ?? [:])
        endpoints = (payload["endpoints"] as? [[String: Any]] ?? []).compactMap(EndpointRecord.init)
        gpus = (payload["gpus"] as? [[String: Any]] ?? []).compactMap(GPURecord.init)
        let operationalEndpointIDs = Set(endpoints.filter { !$0.isRetired }.map(\.id))
        let endpointAttention = endpoints.filter {
            !$0.isRetired && ["ERROR", "STALE", "DRAINING"].contains($0.monitorStatus)
        }.count
        let gpuAttentionStates = Set(["BUSY_UNMANAGED", "UNKNOWN_RECOVERING", "UNKNOWN_STALE", "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY", "DRAINING"])
        let gpuAttention = gpus.filter {
            operationalEndpointIDs.contains($0.endpointID) && gpuAttentionStates.contains($0.state)
        }.count
        summary.attentionResources = max(summary.attentionResources, endpointAttention + gpuAttention)
        leases = (payload["leases"] as? [[String: Any]] ?? []).compactMap(LeaseRecord.init)
        requests = (payload["requests"] as? [[String: Any]] ?? []).compactMap(AllocationRequestRecord.init)
        reservations = (payload["reservations"] as? [[String: Any]] ?? []).compactMap(ReservationRecord.init)
        resourceProviders = (payload["resource_providers"] as? [[String: Any]] ?? []).compactMap(ResourceProviderRecord.init)
        allocatableUnits = (payload["allocatable_units"] as? [[String: Any]] ?? []).compactMap(AllocatableUnitRecord.init)
        schedulerTargets = (payload["scheduler_targets"] as? [[String: Any]] ?? []).compactMap(SchedulerTargetRecord.init)
        schedulerJobs = (payload["scheduler_jobs"] as? [[String: Any]] ?? []).compactMap(SchedulerJobRecord.init)
        schedulerTransfers = (payload["scheduler_transfers"] as? [[String: Any]] ?? [])
            .compactMap(SchedulerTransferRecord.init)
        resourceClaims = (payload["resource_claims"] as? [[String: Any]] ?? []).compactMap(ResourceClaimRecord.init)
        let currentResourcePlanEvaluations = (payload["resource_plan_evaluations"] as? [[String: Any]] ?? [])
            .compactMap(ResourcePlanEvaluationRecord.init)
        resourcePlanEvaluations = currentResourcePlanEvaluations.isEmpty
            ? history.resourcePlanEvaluations
            : currentResourcePlanEvaluations
        let currentResourceRunActuals = (payload["resource_run_actuals"] as? [[String: Any]] ?? [])
            .compactMap(ResourceRunActualRecord.init)
        resourceRunActuals = currentResourceRunActuals.isEmpty
            ? history.resourceRunActuals
            : currentResourceRunActuals
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
        resourceProviders: [ResourceProviderRecord] = [],
        allocatableUnits: [AllocatableUnitRecord] = [],
        schedulerTargets: [SchedulerTargetRecord] = [],
        schedulerJobs: [SchedulerJobRecord] = [],
        schedulerTransfers: [SchedulerTransferRecord] = [],
        resourceClaims: [ResourceClaimRecord] = [],
        resourcePlanEvaluations: [ResourcePlanEvaluationRecord] = [],
        resourceRunActuals: [ResourceRunActualRecord] = [],
        history: BrokerStateHistory = .empty,
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
        self.resourceProviders = resourceProviders
        self.allocatableUnits = allocatableUnits
        self.schedulerTargets = schedulerTargets
        self.schedulerJobs = schedulerJobs
        self.schedulerTransfers = schedulerTransfers
        self.resourceClaims = resourceClaims
        self.resourcePlanEvaluations = resourcePlanEvaluations
        self.resourceRunActuals = resourceRunActuals
        self.history = history
        self.dataAgeSeconds = dataAgeSeconds
        self.freshnessSeconds = freshnessSeconds
        self.admissionBoundary = admissionBoundary
    }

    /// Compares the data that drives the desktop UI while intentionally ignoring
    /// the response timestamp. The service emits a new `serverTime` for every
    /// state read even when the revision and resource data have not changed.
    public func isSemanticallyEquivalentForRefresh(to other: BrokerSnapshot) -> Bool {
        schemaVersion == other.schemaVersion
            && snapshotRevision == other.snapshotRevision
            && summary == other.summary
            && endpoints == other.endpoints
            && gpus == other.gpus
            && leases == other.leases
            && requests == other.requests
            && reservations == other.reservations
            && resourceProviders == other.resourceProviders
            && allocatableUnits == other.allocatableUnits
            && schedulerTargets == other.schedulerTargets
            && schedulerJobs == other.schedulerJobs
            && schedulerTransfers == other.schedulerTransfers
            && resourceClaims == other.resourceClaims
            && resourcePlanEvaluations == other.resourcePlanEvaluations
            && resourceRunActuals == other.resourceRunActuals
            && history == other.history
            && dataAgeSeconds == other.dataAgeSeconds
            && freshnessSeconds == other.freshnessSeconds
            && admissionBoundary == other.admissionBoundary
    }

    public var monitoringProviders: [ResourceProviderRecord] {
        let operationalEndpointIDs = Set(operationalEndpoints.map(\.id))
        var providers = resourceProviders.filter {
            $0.endpointID == nil || operationalEndpointIDs.contains($0.endpointID ?? "")
        }
        let explicitKeys = Set(providers.map {
            "\($0.providerType):\($0.endpointID ?? $0.schedulerTargetID ?? $0.id)"
        })
        providers.append(contentsOf: operationalEndpoints.compactMap { endpoint in
            let key = "direct-gpu:\(endpoint.id)"
            guard !explicitKeys.contains(key) else { return nil }
            let endpointGPUs = gpus(for: endpoint)
            guard !endpointGPUs.isEmpty else { return nil }
            let availableGPUs = endpoint.monitorStatus == "ONLINE"
                ? endpointGPUs.filter { $0.state == "AVAILABLE" }.count
                : 0
            return ResourceProviderRecord(
                id: "direct-gpu:\(endpoint.id)",
                providerType: "direct-gpu",
                displayName: endpoint.displayName,
                endpointID: endpoint.id,
                state: endpoint.monitorStatus,
                enabled: endpoint.enabled,
                total: ResourceQuantityRecord(gpuCount: endpointGPUs.count),
                committed: ResourceQuantityRecord(gpuCount: max(0, endpointGPUs.count - availableGPUs)),
                available: ResourceQuantityRecord(gpuCount: availableGPUs),
                updatedAt: endpoint.monitorLastSuccessAt
            )
        })
        providers.append(contentsOf: operationalEndpoints.compactMap { endpoint in
            let key = "host-capacity:\(endpoint.id)"
            guard !explicitKeys.contains(key) else { return nil }
            guard endpoint.cpuCount != nil || endpoint.memoryTotalMiB != nil else { return nil }
            return ResourceProviderRecord(
                id: "host-capacity:\(endpoint.id)",
                providerType: "host-capacity",
                displayName: endpoint.displayName,
                endpointID: endpoint.id,
                state: endpoint.monitorStatus,
                enabled: endpoint.enabled,
                total: ResourceQuantityRecord(
                    cpuCores: Double(endpoint.cpuCount ?? 0),
                    memoryMiB: endpoint.memoryTotalMiB ?? 0
                ),
                committed: ResourceQuantityRecord(
                    cpuCores: max(0, Double(endpoint.cpuCount ?? 0) - (endpoint.availableCPUCores ?? 0)),
                    memoryMiB: max(0, (endpoint.memoryTotalMiB ?? 0) - (endpoint.memoryAvailableMiB ?? 0))
                ),
                available: ResourceQuantityRecord.availableHost(endpoint: endpoint),
                updatedAt: endpoint.monitorLastSuccessAt
            )
        })
        return providers
    }

    public func gpus(for endpoint: EndpointRecord) -> [GPURecord] {
        gpus.filter { $0.endpointID == endpoint.id }
    }

    public func endpoint(id: String) -> EndpointRecord? {
        endpoints.first { $0.id == id }
    }

    public func gpu(id: String) -> GPURecord? {
        gpus.first { $0.id == id }
    }

    public func stableEndpointSelection(currentID: String) -> String {
        endpoints.contains { $0.id == currentID } ? currentID : (endpoints.first?.id ?? "")
    }

    public func stableGPUSelection(currentID: String) -> String {
        gpus.contains { $0.id == currentID } ? currentID : (gpus.first?.id ?? "")
    }

    public func stableLeaseSelection(currentID: String) -> String {
        leases.contains { $0.id == currentID } ? currentID : (leases.first?.id ?? "")
    }

    public func stableRequestSelection(currentID: String) -> String {
        requests.contains { $0.id == currentID } ? currentID : (requests.first?.id ?? "")
    }
}

public struct ResourceClaimRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let actorID: String
    public let projectID: String
    public let taskReference: String
    public let purpose: String?
    public let state: String
    public let runtimeState: String
    public let providerType: String?
    public let quantities: ResourceQuantityRecord
    public let nativeLeaseIDs: [String]
    public let nativeRequestIDs: [String]
    public let createdAt: String?

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("id"),
            let actorID = raw.string("actor_id"),
            let projectID = raw.string("project_id")
        else {
            return nil
        }
        self.id = id
        self.actorID = actorID
        self.projectID = projectID
        self.taskReference = raw.string("task_ref") ?? raw.string("task_reference") ?? ""
        self.purpose = raw.string("purpose")
        self.state = (raw.string("state") ?? "UNKNOWN").uppercased()
        self.runtimeState = (raw.string("runtime_state") ?? self.state).uppercased()
        self.providerType = raw.string("provider_type")
        self.quantities = ResourceQuantityRecord(
            raw: raw["quantities"] as? [String: Any]
                ?? raw["requested_quantities"] as? [String: Any]
                ?? [:]
        )
        self.nativeLeaseIDs = raw["native_lease_ids"] as? [String] ?? []
        self.nativeRequestIDs = raw["native_request_ids"] as? [String] ?? []
        self.createdAt = raw.string("created_at")
    }

    public var stateLabel: String {
        if runtimeState == "RUNNING" { return "运行中" }
        switch state {
        case "QUEUED": return "排队中"
        case "PENDING_APPROVAL": return "等待批准"
        case "BLOCKED": return "等待资源"
        case "HELD", "ACTIVE": return "已分配"
        case "RUNNING": return "运行中"
        case "REJECTED": return "已拒绝"
        case "RELEASED": return "已释放"
        default: return state
        }
    }
}

public struct ResourcePlanEvaluationRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let claimID: String?
    public let actorID: String
    public let projectID: String
    public let taskReference: String
    public let selectedCandidateKey: String?
    public let minimumSavedSeconds: Int?
    public let minimumSavedRatio: Double?
    public let createdAt: String?
    public let candidates: [ResourcePlanCandidateRecord]

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("id"),
            let actorID = raw.string("actor_id"),
            let projectID = raw.string("project_id")
        else {
            return nil
        }
        self.id = id
        self.claimID = raw.string("claim_id")
        self.actorID = actorID
        self.projectID = projectID
        self.taskReference = raw.string("task_ref") ?? raw.string("task_reference") ?? ""
        self.selectedCandidateKey = raw.string("selected_candidate_key")
        self.minimumSavedSeconds = raw.optionalInt("minimum_saved_seconds") ?? raw.optionalInt("min_saved_seconds")
            ?? raw.optionalInt("marginal_min_saved_seconds")
        self.minimumSavedRatio = raw.optionalDouble("minimum_saved_ratio")
            ?? raw.optionalDouble("min_saved_ratio")
            ?? raw.optionalDouble("marginal_min_saved_ratio")
        self.createdAt = raw.string("created_at")
        self.candidates = (raw["candidates"] as? [[String: Any]] ?? []).compactMap(ResourcePlanCandidateRecord.init)
    }

    public var selectedCandidate: ResourcePlanCandidateRecord? {
        candidates.first { $0.candidateKey == selectedCandidateKey || $0.selected }
    }
}

public struct ResourcePlanCandidateRecord: Identifiable, Equatable, Sendable {
    public var id: String { candidateKey }
    public let candidateKey: String
    public let providerType: String?
    public let quantities: ResourceQuantityRecord
    public let predictedRuntimeSeconds: Int
    public let predictedSavedSeconds: Int
    public let predictedSavedRatio: Double
    public let selected: Bool
    public let rejectionReason: String?

    public init?(raw: [String: Any]) {
        guard let candidateKey = raw.string("candidate_key") ?? raw.string("id") else { return nil }
        self.candidateKey = candidateKey
        self.providerType = raw.string("provider_type")
        self.quantities = ResourceQuantityRecord(raw: raw["quantities"] as? [String: Any] ?? raw)
        self.predictedRuntimeSeconds = raw.optionalInt("predicted_runtime_seconds")
            ?? raw.optionalInt("predicted_remaining_seconds")
            ?? 0
        self.predictedSavedSeconds = raw.optionalInt("predicted_saved_seconds") ?? 0
        self.predictedSavedRatio = raw.optionalDouble("predicted_saved_ratio") ?? 0
        self.selected = raw.bool("selected", default: false)
        self.rejectionReason = raw.string("rejection_reason")
    }

    public var decisionLabel: String {
        if selected { return "已选择" }
        if let rejectionReason, !rejectionReason.isEmpty { return rejectionReason }
        return "未选择"
    }
}

public struct ResourceRunActualRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let evaluationID: String?
    public let claimID: String?
    public let actorID: String
    public let projectID: String
    public let taskReference: String
    public let providerType: String?
    public let quantities: ResourceQuantityRecord
    public let predictedDurationSeconds: Int?
    public let actualDurationSeconds: Int?
    public let createdAt: String?

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("id") ?? raw.optionalInt("id").map(String.init),
            let actorID = raw.string("actor_id"),
            let projectID = raw.string("project_id")
        else {
            return nil
        }
        self.id = id
        self.evaluationID = raw.string("evaluation_id")
        self.claimID = raw.string("claim_id")
        self.actorID = actorID
        self.projectID = projectID
        self.taskReference = raw.string("task_ref") ?? raw.string("task_reference") ?? ""
        self.providerType = raw.string("provider_type")
        self.quantities = ResourceQuantityRecord(raw: raw["quantities"] as? [String: Any] ?? [:])
        self.predictedDurationSeconds = raw.optionalInt("predicted_duration_seconds")
            ?? raw.optionalInt("predicted_runtime_seconds")
        self.actualDurationSeconds = raw.optionalInt("actual_duration_seconds")
        self.createdAt = raw.string("created_at")
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
    public let runtimeState: String
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
        self.state = (raw.string("state") ?? "UNKNOWN").uppercased()
        self.runtimeState = (raw.string("runtime_state") ?? "ASSIGNED").uppercased()
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

    func optionalFraction(_ key: String) -> Double? {
        optionalDouble(key).map { Swift.min(Swift.max($0, 0), 1) }
    }

    func optionalPercent(_ key: String) -> Double? {
        optionalDouble(key).map { Swift.min(Swift.max($0 / 100, 0), 1) }
    }

    func bool(_ key: String, default fallback: Bool) -> Bool {
        if let value = self[key] as? Bool { return value }
        if let value = self[key] as? NSNumber { return value.boolValue }
        return fallback
    }
}
