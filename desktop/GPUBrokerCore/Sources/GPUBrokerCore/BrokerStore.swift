import Combine
import Foundation

public enum BrokerRefreshFreshness: Equatable, Sendable {
    case waiting
    case fresh
    case stale
    case failed
}

public enum BrokerRefreshError: LocalizedError, Equatable, Sendable {
    case missingClient
    case timeout
    case invalidSnapshot
    case serviceRejected(Int)
    case snapshotRevisionBehind(required: Int, received: Int?)

    public var errorDescription: String? {
        switch self {
        case .missingClient:
            return "本机服务尚未连接。"
        case .timeout:
            return "资源刷新超时。"
        case .invalidSnapshot:
            return "本机服务返回了无法读取的资源快照。"
        case .serviceRejected(let status):
            return "本机服务拒绝了资源刷新（HTTP \(status)）。"
        case .snapshotRevisionBehind(let required, let received):
            let receivedLabel = received.map(String.init) ?? "未知"
            return "本机服务返回了旧资源快照（\(receivedLabel)，需要至少 \(required)）。请稍后刷新。"
        }
    }
}

public protocol BrokerSnapshotClient: AnyObject, Sendable {
    func snapshot(actorID: String) async throws -> BrokerSnapshot
}

public protocol BrokerEndpointTelemetryHistoryClient: AnyObject, Sendable {
    func history(endpointID: String, range: EndpointTelemetryRange, actorID: String) async throws -> EndpointTelemetryHistory
}

public final class URLSessionBrokerSnapshotClient: BrokerSnapshotClient {
    private let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    public func snapshot(actorID: String) async throws -> BrokerSnapshot {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/v1/state"))
        request.timeoutInterval = 6
        request.setValue(actorID, forHTTPHeaderField: "X-GPU-Broker-Actor")
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw BrokerRefreshError.invalidSnapshot
        }
        guard (200..<300).contains(response.statusCode) else {
            throw BrokerRefreshError.serviceRejected(response.statusCode)
        }
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let envelope = object as? [String: Any],
            let stateData = envelope["data"] as? [String: Any],
            stateData["current"] is [String: Any],
            stateData["history"] is [String: Any]
        else {
            throw BrokerRefreshError.invalidSnapshot
        }
        return BrokerSnapshot(envelope: envelope)
    }
}

public final class URLSessionEndpointTelemetryHistoryClient: BrokerEndpointTelemetryHistoryClient {
    private let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    public func history(
        endpointID: String,
        range: EndpointTelemetryRange,
        actorID: String
    ) async throws -> EndpointTelemetryHistory {
        let endpointURL = baseURL
            .appendingPathComponent("api/v1/endpoints")
            .appendingPathComponent(endpointID)
            .appendingPathComponent("history")
        var components = URLComponents(url: endpointURL, resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "window_seconds", value: String(range.windowSeconds)),
            URLQueryItem(name: "points", value: "120"),
        ]
        guard let url = components?.url else { throw BrokerRefreshError.invalidSnapshot }
        var request = URLRequest(url: url)
        request.timeoutInterval = 6
        request.setValue(actorID, forHTTPHeaderField: "X-GPU-Broker-Actor")
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw BrokerRefreshError.invalidSnapshot
        }
        guard (200..<300).contains(response.statusCode) else {
            throw BrokerRefreshError.serviceRejected(response.statusCode)
        }
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let envelope = object as? [String: Any]
        else {
            throw BrokerRefreshError.invalidSnapshot
        }
        return EndpointTelemetryHistory(endpointID: endpointID, range: range, envelope: envelope)
    }
}

@MainActor
public final class BrokerStore: ObservableObject {
    private struct PendingEndpointRemoval {
        let endpointID: String
        let endpointDisplayName: String
        let expectedLifecycleState: String
        let completion: @MainActor @Sendable (Bool, String?) -> Void
    }

    private struct EndpointTelemetryHistoryCacheKey: Hashable {
        let endpointID: String
        let range: EndpointTelemetryRange
    }

    private struct CachedEndpointTelemetryHistory {
        let history: EndpointTelemetryHistory
        let fetchedAt: Date
    }

    @Published public private(set) var snapshot = BrokerSnapshot.empty
    @Published public private(set) var lastGoodSnapshot: BrokerSnapshot?
    @Published public private(set) var freshness: BrokerRefreshFreshness = .waiting
    @Published public private(set) var isConnected = false
    @Published public private(set) var isRefreshing = false
    @Published public private(set) var lastUpdated: Date?
    @Published public private(set) var serviceInfo: ServiceInfo?
    @Published public private(set) var deletingEndpointIDs: Set<String> = []
    @Published public private(set) var releasingLeaseIDs: Set<String> = []
    @Published public private(set) var endpointTelemetryHistory: [String: EndpointTelemetryHistory] = [:]
    @Published public private(set) var endpointTelemetryHistoryLoading: Set<String> = []
    @Published public private(set) var endpointTelemetryHistoryErrors: [String: String] = [:]
    @Published public var actorID: String
    @Published public var notice: String?
    @Published public var errorMessage: String?

    private var baseURL: URL?
    private var snapshotClient: BrokerSnapshotClient?
    private var endpointTelemetryHistoryClient: BrokerEndpointTelemetryHistoryClient?
    private var periodicRefreshTask: Task<Void, Never>?
    private var activeRefreshTask: Task<Void, Never>?
    private var endpointTelemetryHistoryTasks: [EndpointTelemetryHistoryCacheKey: Task<Void, Never>] = [:]
    private var endpointTelemetryHistoryGenerations: [EndpointTelemetryHistoryCacheKey: UInt64] = [:]
    private var endpointTelemetryHistoryCache: [EndpointTelemetryHistoryCacheKey: CachedEndpointTelemetryHistory] = [:]
    private var endpointTelemetryHistoryCacheRecency: [EndpointTelemetryHistoryCacheKey] = []
    private var activeEndpointTelemetryHistoryKey: EndpointTelemetryHistoryCacheKey?
    private var pendingRefresh = false
    private var refreshGeneration: UInt64 = 0
    private var discardedRefreshGeneration: UInt64?
    private var pendingEndpointRemovals: [PendingEndpointRemoval] = []
    private var minimumRequiredSnapshotRevision: Int?
    private let refreshTimeoutSeconds: TimeInterval
    private let refreshIntervalSeconds: TimeInterval
    private let dateProvider: () -> Date

    private static let endpointTelemetryHistoryCacheMaximumAge: TimeInterval = 30
    private static let endpointTelemetryHistoryCacheLimit = 12

    public init(
        actorID: String? = nil,
        refreshTimeoutSeconds: TimeInterval = 6,
        refreshIntervalSeconds: TimeInterval = 12,
        dateProvider: @escaping () -> Date = Date.init
    ) {
        self.actorID = actorID ?? UserDefaults.standard.string(forKey: "gpuBrokerActorID") ?? "human"
        self.refreshTimeoutSeconds = refreshTimeoutSeconds
        self.refreshIntervalSeconds = refreshIntervalSeconds
        self.dateProvider = dateProvider
    }

    deinit {
        periodicRefreshTask?.cancel()
        activeRefreshTask?.cancel()
        endpointTelemetryHistoryTasks.values.forEach { $0.cancel() }
    }

    public var supportsEndpointDeletion: Bool {
        serviceInfo?.supportsEndpointDeletion == true
    }

    public var supportsEndpointTelemetryHistory: Bool {
        serviceInfo?.supportsEndpointTelemetryHistory == true && endpointTelemetryHistoryClient != nil
    }

    public var allowsMutations: Bool {
        baseURL != nil && isConnected && freshness == .fresh
    }

    public var allowsEndpointLifecycleMutations: Bool {
        baseURL != nil && isConnected
    }

    public var canRefresh: Bool {
        snapshotClient != nil
    }

    public var mutationUnavailableReason: String {
        if baseURL == nil {
            return "当前为只读测试夹具或尚未连接本机服务，不能执行资源变更。"
        }
        if !isConnected {
            return "本机服务连接已中断，不能基于旧数据执行资源变更。请先刷新重试。"
        }
        return "资源数据已过期或尚未就绪，不能执行资源变更。请先刷新到最新状态。"
    }

    public var endpointLifecycleMutationUnavailableReason: String {
        if baseURL == nil {
            return "当前为只读测试夹具或尚未连接本机服务，不能移除服务器。"
        }
        return "本机服务连接已中断，暂时不能移除服务器。请先刷新重试。"
    }

    public func connect(to baseURL: URL, serviceInfo: ServiceInfo) {
        self.baseURL = baseURL
        configureSnapshotClient(
            URLSessionBrokerSnapshotClient(baseURL: baseURL),
            endpointTelemetryHistoryClient: serviceInfo.supportsEndpointTelemetryHistory
                ? URLSessionEndpointTelemetryHistoryClient(baseURL: baseURL)
                : nil,
            serviceInfo: serviceInfo,
            startPeriodicRefresh: true
        )
    }

    public func connectForTesting(
        snapshotClient: BrokerSnapshotClient,
        endpointTelemetryHistoryClient: BrokerEndpointTelemetryHistoryClient? = nil,
        serviceInfo: ServiceInfo = .fixture,
        baseURL: URL? = nil,
        startPeriodicRefresh: Bool = false
    ) {
        self.baseURL = baseURL
        configureSnapshotClient(
            snapshotClient,
            endpointTelemetryHistoryClient: endpointTelemetryHistoryClient,
            serviceInfo: serviceInfo,
            startPeriodicRefresh: startPeriodicRefresh
        )
    }

    public func useFixture(
        snapshot: BrokerSnapshot,
        serviceInfo: ServiceInfo = .fixture,
        endpointTelemetryHistoryClient: BrokerEndpointTelemetryHistoryClient? = nil
    ) {
        invalidateRefreshWork()
        invalidateEndpointTelemetryHistoryWork()
        self.endpointTelemetryHistoryClient = endpointTelemetryHistoryClient
        self.snapshot = snapshot
        self.lastGoodSnapshot = snapshot
        self.freshness = Self.freshness(for: snapshot)
        self.isConnected = true
        self.isRefreshing = false
        self.lastUpdated = dateProvider()
        self.serviceInfo = serviceInfo
        self.errorMessage = nil
        self.notice = "正在使用桌面测试夹具。"
    }

    public func endpointTelemetryHistory(
        endpointID: String,
        range: EndpointTelemetryRange
    ) -> EndpointTelemetryHistory? {
        let history = endpointTelemetryHistory[endpointID]
        return history?.range == range ? history : nil
    }

    public func requestEndpointTelemetryHistory(endpointID: String, range: EndpointTelemetryRange) {
        guard serviceInfo?.supportsEndpointTelemetryHistory == true else {
            endpointTelemetryHistoryErrors[endpointID] = "当前本机服务未提供资源历史能力。"
            endpointTelemetryHistoryLoading.remove(endpointID)
            return
        }
        guard let endpointTelemetryHistoryClient else {
            endpointTelemetryHistoryErrors[endpointID] = "资源历史客户端尚未连接。"
            endpointTelemetryHistoryLoading.remove(endpointID)
            return
        }
        let key = EndpointTelemetryHistoryCacheKey(endpointID: endpointID, range: range)
        cancelInactiveEndpointTelemetryHistoryRequests(keeping: key)
        if let cachedHistory = cachedEndpointTelemetryHistory(for: key) {
            activeEndpointTelemetryHistoryKey = key
            presentEndpointTelemetryHistory(cachedHistory, for: key)
            activeEndpointTelemetryHistoryKey = nil
            return
        }
        guard endpointTelemetryHistoryTasks[key] == nil else { return }

        let generation = (endpointTelemetryHistoryGenerations[key] ?? 0) &+ 1
        endpointTelemetryHistoryGenerations[key] = generation
        activeEndpointTelemetryHistoryKey = key
        endpointTelemetryHistoryLoading.insert(endpointID)
        endpointTelemetryHistoryErrors[endpointID] = nil
        let actorID = self.actorID
        endpointTelemetryHistoryTasks[key] = Task { [weak self] in
            let result: Result<EndpointTelemetryHistory, Error>
            do {
                result = .success(try await endpointTelemetryHistoryClient.history(
                    endpointID: endpointID,
                    range: range,
                    actorID: actorID
                ))
            } catch {
                result = .failure(error)
            }
            self?.completeEndpointTelemetryHistory(
                result,
                for: key,
                generation: generation
            )
        }
    }

    public func reload() {
        requestRefresh()
    }

    public func setActor(_ value: String) {
        let cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else {
            errorMessage = "本机操作标识不能为空。"
            return
        }
        actorID = cleaned
        UserDefaults.standard.set(cleaned, forKey: "gpuBrokerActorID")
        notice = "本机操作记录已切换为：\(cleaned)。"
        invalidateActiveRefresh()
        requestRefresh()
    }

    public func submitClaim(_ draft: ClaimDraft, completion: @escaping @MainActor @Sendable (ClaimSubmissionResult?, String?) -> Void) {
        guard allowsMutations else {
            let message = mutationUnavailableMessage
            errorMessage = message
            completion(nil, message)
            return
        }
        let project = draft.projectID.trimmingCharacters(in: .whitespacesAndNewlines)
        let task = draft.taskReference.trimmingCharacters(in: .whitespacesAndNewlines)
        let purpose = draft.purpose.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !project.isEmpty, !task.isEmpty, !purpose.isEmpty, draft.gpuCount > 0 else {
            completion(nil, "请完整填写项目、任务、用途和 GPU 数量。")
            return
        }
        var constraints: [String: Any] = [
            "gpu_count": draft.gpuCount,
            "placement": "pack"
        ]
        if !draft.endpointID.isEmpty {
            constraints["endpoint_ids"] = [draft.endpointID]
        }
        if let minimumCPUCores = draft.minimumCPUCores {
            constraints["min_available_cpu_cores"] = minimumCPUCores
        }
        if let minimumMemoryMiB = draft.minimumMemoryMiB {
            constraints["min_available_memory_mib"] = minimumMemoryMiB
        }
        if let minimumTotalVRAMMiB = draft.minimumTotalVRAMMiB {
            constraints["min_total_vram_mib"] = minimumTotalVRAMMiB
        }
        if let minimumFreeVRAMMiB = draft.minimumFreeVRAMMiB {
            constraints["min_free_vram_mib"] = minimumFreeVRAMMiB
        }
        performMutationWithPayload(
            path: "api/v1/claims",
            payload: [
                "project_id": project,
                "task_ref": task,
                "purpose": purpose,
                "constraints": constraints
            ]
        ) { [weak self] payload, error in
            guard let self else { return }
            if let error {
                completion(nil, error)
                return
            }
            let lease = payload?["lease"] as? [String: Any]
            let request = payload?["request"] as? [String: Any]
            let requestID = request?.string("id") ?? "未知请求"
            let leaseID = lease?.string("id")
            let allocated = lease != nil
            let message: String
            if let leaseID {
                let gpuIDs = lease?["gpu_ids"] as? [String] ?? []
                message = "已分配 \(max(gpuIDs.count, draft.gpuCount)) 个 GPU，租约 \(leaseID) 已生效。这里只分配资源，不会启动任务。"
            } else {
                message = "资源不足或需要等待，请求 \(requestID) 已进入队列。排队期间请先不要启动任务。"
            }
            self.notice = message
            self.errorMessage = nil
            self.reload()
            completion(ClaimSubmissionResult(allocated: allocated, message: message), nil)
        }
    }

    public func addEndpoint(_ draft: EndpointDraft, completion: @escaping @MainActor @Sendable (Bool, String?) -> Void) {
        guard allowsMutations else {
            let message = mutationUnavailableMessage
            errorMessage = message
            completion(false, message)
            return
        }
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
            successMessage: "已添加服务器 \(draft.id)，正在确认状态。",
            completion: completion
        )
    }

    public func deleteEndpoint(_ endpoint: EndpointRecord, completion: @escaping @MainActor @Sendable (Bool, String?) -> Void) {
        guard allowsEndpointLifecycleMutations else {
            let message = endpointLifecycleMutationUnavailableReason
            errorMessage = message
            completion(false, message)
            return
        }
        guard supportsEndpointDeletion else {
            let message = "当前本机服务不支持移除服务器。请重启或升级 GPU Broker 服务后再试。"
            errorMessage = message
            completion(false, message)
            return
        }
        deletingEndpointIDs.insert(endpoint.id)
        advanceEndpointLifecycle(endpoint, drainWasAccepted: false, completion: completion)
    }

    private func advanceEndpointLifecycle(
        _ endpoint: EndpointRecord,
        drainWasAccepted: Bool,
        completion: @escaping @MainActor @Sendable (Bool, String?) -> Void
    ) {
        guard let url = baseURL?
            .appendingPathComponent("api/v1/endpoints")
            .appendingPathComponent(endpoint.id)
        else {
            deletingEndpointIDs.remove(endpoint.id)
            let message = "本机服务尚未连接。"
            errorMessage = message
            completion(false, message)
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        request.timeoutInterval = 10
        request.setValue(actorID, forHTTPHeaderField: "X-GPU-Broker-Actor")
        request.setValue(UUID().uuidString, forHTTPHeaderField: "Idempotency-Key")

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.deletingEndpointIDs.remove(endpoint.id)
                    let message = "移除失败：\(error.localizedDescription)"
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                guard let response = response as? HTTPURLResponse else {
                    self.deletingEndpointIDs.remove(endpoint.id)
                    let message = "移除失败：未收到有效响应。"
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                guard (200..<300).contains(response.statusCode) else {
                    if let code = Self.apiErrorCode(from: data), [
                        "endpoint_already_retired",
                        "endpoint_not_found"
                    ].contains(code) {
                        self.confirmEndpointRemovalAfterMutation(
                            endpoint,
                            expectedLifecycleState: "RETIRED"
                        ) { success, message in
                            self.deletingEndpointIDs.remove(endpoint.id)
                            completion(success, message)
                        }
                        return
                    }
                    self.deletingEndpointIDs.remove(endpoint.id)
                    let reason = self.apiErrorMessage(from: data) ?? "服务拒绝了此操作。"
                    let message = drainWasAccepted
                        ? "服务器已进入排空，但尚未完成退役：\(reason)"
                        : "移除失败：\(reason)"
                    if drainWasAccepted {
                        self.notice = "\(endpoint.displayName) 已停止接收新分配。"
                        self.requestRefresh()
                    }
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                guard let lifecycleState = Self.endpointLifecycleState(from: data) else {
                    self.deletingEndpointIDs.remove(endpoint.id)
                    let message = "移除失败：本机服务返回了无法识别的服务器状态。"
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                if lifecycleState == "DRAINING", !drainWasAccepted {
                    self.raiseMinimumRequiredSnapshotRevision(from: data)
                    self.advanceEndpointLifecycle(
                        endpoint,
                        drainWasAccepted: true,
                        completion: completion
                    )
                    return
                }
                guard lifecycleState == "RETIRED" else {
                    self.deletingEndpointIDs.remove(endpoint.id)
                    let message = "移除失败：服务器停留在 \(lifecycleState) 状态。"
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                self.raiseMinimumRequiredSnapshotRevision(from: data)
                self.confirmEndpointRemovalAfterMutation(
                    endpoint,
                    expectedLifecycleState: lifecycleState
                ) { success, message in
                    self.deletingEndpointIDs.remove(endpoint.id)
                    completion(success, message)
                }
            }
        }.resume()
    }

    static func endpointLifecycleState(from data: Data?) -> String? {
        guard
            let data,
            let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let endpoint = ((payload["data"] as? [String: Any])?["endpoint"] as? [String: Any])
                ?? (payload["endpoint"] as? [String: Any]),
            let lifecycleState = endpoint["lifecycle_state"] as? String
        else {
            return nil
        }
        return lifecycleState.uppercased()
    }

    static func apiErrorCode(from data: Data?) -> String? {
        guard
            let data,
            let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let error = payload["error"] as? [String: Any]
        else {
            return nil
        }
        return error["code"] as? String
    }

    public func releaseLease(_ lease: LeaseRecord, completion: @escaping @MainActor @Sendable (Bool, String?) -> Void) {
        guard allowsMutations else {
            let message = mutationUnavailableMessage
            errorMessage = message
            completion(false, message)
            return
        }
        guard let url = baseURL?
            .appendingPathComponent("api/v1/leases")
            .appendingPathComponent(lease.id)
            .appendingPathComponent("release")
        else {
            let message = "本机服务尚未连接。"
            errorMessage = message
            completion(false, message)
            return
        }
        guard let body = try? JSONSerialization.data(withJSONObject: ["reason": "desktop release"]) else {
            let message = "无法编码释放请求。"
            errorMessage = message
            completion(false, message)
            return
        }
        releasingLeaseIDs.insert(lease.id)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpBody = body
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(actorID, forHTTPHeaderField: "X-GPU-Broker-Actor")
        request.setValue(UUID().uuidString, forHTTPHeaderField: "Idempotency-Key")
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            Task { @MainActor in
                guard let self else { return }
                self.releasingLeaseIDs.remove(lease.id)
                if let error {
                    let message = "释放失败：\(error.localizedDescription)"
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                guard let response = response as? HTTPURLResponse else {
                    let message = "释放失败：未收到有效响应。"
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                guard (200..<300).contains(response.statusCode) else {
                    let message = "释放失败：\(self.apiErrorMessage(from: data) ?? "服务拒绝了此操作。")"
                    self.errorMessage = message
                    completion(false, message)
                    return
                }
                self.raiseMinimumRequiredSnapshotRevision(from: data)
                self.notice = "已释放租约 \(lease.id)。"
                self.errorMessage = nil
                self.reload()
                completion(true, nil)
            }
        }.resume()
    }

    private func configureSnapshotClient(
        _ snapshotClient: BrokerSnapshotClient,
        endpointTelemetryHistoryClient: BrokerEndpointTelemetryHistoryClient?,
        serviceInfo: ServiceInfo,
        startPeriodicRefresh: Bool
    ) {
        invalidateRefreshWork()
        invalidateEndpointTelemetryHistoryWork()
        self.snapshotClient = snapshotClient
        self.endpointTelemetryHistoryClient = endpointTelemetryHistoryClient
        self.serviceInfo = serviceInfo
        if startPeriodicRefresh {
            startPeriodicRefreshLoop()
        }
        requestRefresh()
    }

    private func completeEndpointTelemetryHistory(
        _ result: Result<EndpointTelemetryHistory, Error>,
        for key: EndpointTelemetryHistoryCacheKey,
        generation: UInt64
    ) {
        guard endpointTelemetryHistoryGenerations[key] == generation else { return }
        endpointTelemetryHistoryTasks[key] = nil
        let isActiveRequest = activeEndpointTelemetryHistoryKey == key
        if isActiveRequest {
            activeEndpointTelemetryHistoryKey = nil
            endpointTelemetryHistoryLoading.remove(key.endpointID)
        }
        switch result {
        case .success(let history):
            guard history.endpointID == key.endpointID, history.range == key.range else {
                setEndpointTelemetryHistoryError(BrokerRefreshError.invalidSnapshot, for: key.endpointID)
                return
            }
            cacheEndpointTelemetryHistory(history, for: key)
            if isActiveRequest {
                presentEndpointTelemetryHistory(history, for: key)
            }
        case .failure(let error):
            if isActiveRequest {
                setEndpointTelemetryHistoryError(error, for: key.endpointID)
            }
        }
    }

    private func cancelInactiveEndpointTelemetryHistoryRequests(keeping key: EndpointTelemetryHistoryCacheKey) {
        let inactiveRequests = endpointTelemetryHistoryTasks.filter { $0.key != key }
        for (inactiveKey, task) in inactiveRequests {
            task.cancel()
            endpointTelemetryHistoryTasks[inactiveKey] = nil
            endpointTelemetryHistoryGenerations[inactiveKey] = (endpointTelemetryHistoryGenerations[inactiveKey] ?? 0) &+ 1
            endpointTelemetryHistoryLoading.remove(inactiveKey.endpointID)
        }
        if activeEndpointTelemetryHistoryKey != key {
            activeEndpointTelemetryHistoryKey = nil
        }
    }

    private func cachedEndpointTelemetryHistory(
        for key: EndpointTelemetryHistoryCacheKey
    ) -> EndpointTelemetryHistory? {
        guard let cached = endpointTelemetryHistoryCache[key] else { return nil }
        let age = max(0, dateProvider().timeIntervalSince(cached.fetchedAt))
        guard age <= Self.endpointTelemetryHistoryCacheMaximumAge else {
            endpointTelemetryHistoryCache[key] = nil
            endpointTelemetryHistoryCacheRecency.removeAll { $0 == key }
            return nil
        }
        endpointTelemetryHistoryCacheRecency.removeAll { $0 == key }
        endpointTelemetryHistoryCacheRecency.append(key)
        return cached.history
    }

    private func cacheEndpointTelemetryHistory(
        _ history: EndpointTelemetryHistory,
        for key: EndpointTelemetryHistoryCacheKey
    ) {
        endpointTelemetryHistoryCache[key] = CachedEndpointTelemetryHistory(
            history: history,
            fetchedAt: dateProvider()
        )
        endpointTelemetryHistoryCacheRecency.removeAll { $0 == key }
        endpointTelemetryHistoryCacheRecency.append(key)
        while endpointTelemetryHistoryCacheRecency.count > Self.endpointTelemetryHistoryCacheLimit {
            let leastRecentKey = endpointTelemetryHistoryCacheRecency.removeFirst()
            endpointTelemetryHistoryCache[leastRecentKey] = nil
        }
    }

    private func presentEndpointTelemetryHistory(
        _ history: EndpointTelemetryHistory,
        for key: EndpointTelemetryHistoryCacheKey
    ) {
        if endpointTelemetryHistory[key.endpointID] != history {
            endpointTelemetryHistory[key.endpointID] = history
        }
        if endpointTelemetryHistoryLoading.contains(key.endpointID) {
            endpointTelemetryHistoryLoading.remove(key.endpointID)
        }
        if endpointTelemetryHistoryErrors[key.endpointID] != nil {
            endpointTelemetryHistoryErrors[key.endpointID] = nil
        }
    }

    private func setEndpointTelemetryHistoryError(_ error: Error, for endpointID: String) {
        endpointTelemetryHistoryLoading.remove(endpointID)
        let message = "无法读取资源历史：\(error.localizedDescription)"
        if endpointTelemetryHistoryErrors[endpointID] != message {
            endpointTelemetryHistoryErrors[endpointID] = message
        }
    }

    private func requestRefresh() {
        guard snapshotClient != nil else { return }
        if activeRefreshTask != nil {
            pendingRefresh = true
            return
        }
        startRefresh()
    }

    private func startRefresh() {
        guard let snapshotClient else { return }
        refreshGeneration &+= 1
        let generation = refreshGeneration
        let actorID = self.actorID
        let timeoutSeconds = refreshTimeoutSeconds
        isRefreshing = true
        activeRefreshTask = Task { [weak self] in
            let result = await Self.fetchSnapshot(
                snapshotClient,
                actorID: actorID,
                timeoutSeconds: timeoutSeconds
            )
            self?.completeRefresh(result, generation: generation)
        }
    }

    private static func fetchSnapshot(
        _ client: BrokerSnapshotClient,
        actorID: String,
        timeoutSeconds: TimeInterval
    ) async -> Result<BrokerSnapshot, Error> {
        let requestTask = Task {
            try await client.snapshot(actorID: actorID)
        }
        let timeoutTask = Task<BrokerSnapshot, Error> {
            try await Task.sleep(nanoseconds: secondsToNanoseconds(timeoutSeconds))
            throw BrokerRefreshError.timeout
        }
        return await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                let lock = NSLock()
                var didResume = false
                func resume(_ result: Result<BrokerSnapshot, Error>) {
                    lock.lock()
                    defer { lock.unlock() }
                    guard !didResume else { return }
                    didResume = true
                    requestTask.cancel()
                    timeoutTask.cancel()
                    continuation.resume(returning: result)
                }
                Task {
                    do {
                        resume(.success(try await requestTask.value))
                    } catch {
                        resume(.failure(error))
                    }
                }
                Task {
                    do {
                        resume(.success(try await timeoutTask.value))
                    } catch {
                        resume(.failure(error))
                    }
                }
            }
        } onCancel: {
            requestTask.cancel()
            timeoutTask.cancel()
        }
    }

    private func completeRefresh(_ result: Result<BrokerSnapshot, Error>, generation: UInt64) {
        guard generation == refreshGeneration else { return }
        activeRefreshTask = nil
        let shouldDiscard = discardedRefreshGeneration == generation
        if shouldDiscard {
            discardedRefreshGeneration = nil
        } else {
            let resolvedResult: Result<BrokerSnapshot, Error>
            switch result {
            case .success(let snapshot):
                if let revisionError = snapshotRevisionFloorError(for: snapshot) {
                    if isConnected {
                        self.isConnected = false
                    }
                    let failedFreshness: BrokerRefreshFreshness = lastGoodSnapshot == nil ? .failed : .stale
                    if freshness != failedFreshness {
                        self.freshness = failedFreshness
                    }
                    let message = "无法更新资源：\(revisionError.localizedDescription)"
                    if errorMessage != message {
                        self.errorMessage = message
                    }
                    resolvedResult = .failure(revisionError)
                } else {
                    let shouldPublishSnapshot = !self.snapshot.isSemanticallyEquivalentForRefresh(to: snapshot)
                    if shouldPublishSnapshot {
                        self.snapshot = snapshot
                    }
                    if lastGoodSnapshot?.isSemanticallyEquivalentForRefresh(to: snapshot) != true {
                        self.lastGoodSnapshot = snapshot
                    }
                    let refreshedFreshness = Self.freshness(for: snapshot)
                    if freshness != refreshedFreshness {
                        self.freshness = refreshedFreshness
                    }
                    if !isConnected {
                        self.isConnected = true
                    }
                    self.lastUpdated = dateProvider()
                    if errorMessage != nil {
                        self.errorMessage = nil
                    }
                    if let requiredRevision = minimumRequiredSnapshotRevision,
                       let snapshotRevision = snapshot.snapshotRevision,
                       snapshotRevision >= requiredRevision {
                        minimumRequiredSnapshotRevision = nil
                    }
                    resolvedResult = .success(snapshot)
                }
            case .failure(let error):
                if isConnected {
                    self.isConnected = false
                }
                let failedFreshness: BrokerRefreshFreshness = lastGoodSnapshot == nil ? .failed : .stale
                if freshness != failedFreshness {
                    self.freshness = failedFreshness
                }
                let message = "无法更新资源：\(error.localizedDescription)"
                if errorMessage != message {
                    self.errorMessage = message
                }
                resolvedResult = .failure(error)
            }
            resolvePendingEndpointRemovals(with: resolvedResult)
        }
        if pendingRefresh {
            pendingRefresh = false
            startRefresh()
        } else {
            isRefreshing = false
        }
    }

    private func startPeriodicRefreshLoop() {
        periodicRefreshTask?.cancel()
        guard refreshIntervalSeconds > 0 else { return }
        let intervalSeconds = refreshIntervalSeconds
        periodicRefreshTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: Self.secondsToNanoseconds(intervalSeconds))
                } catch {
                    break
                }
                self?.requestRefresh()
            }
        }
    }

    private func invalidateRefreshWork() {
        periodicRefreshTask?.cancel()
        periodicRefreshTask = nil
        invalidateActiveRefresh()
    }

    private func invalidateEndpointTelemetryHistoryWork() {
        endpointTelemetryHistoryTasks.values.forEach { $0.cancel() }
        endpointTelemetryHistoryTasks.removeAll()
        endpointTelemetryHistoryGenerations.removeAll()
        endpointTelemetryHistoryCache.removeAll()
        endpointTelemetryHistoryCacheRecency.removeAll()
        activeEndpointTelemetryHistoryKey = nil
        endpointTelemetryHistoryLoading.removeAll()
        endpointTelemetryHistoryErrors.removeAll()
        endpointTelemetryHistory.removeAll()
    }

    private func invalidateActiveRefresh() {
        refreshGeneration &+= 1
        pendingRefresh = false
        discardedRefreshGeneration = nil
        activeRefreshTask?.cancel()
        activeRefreshTask = nil
        isRefreshing = false
    }

    private func performMutation(
        path: String,
        payload: [String: Any],
        successMessage: String,
        completion: @escaping @MainActor @Sendable (Bool, String?) -> Void
    ) {
        performMutationWithPayload(path: path, payload: payload) { [weak self] _, error in
            guard let self else { return }
            if let error {
                completion(false, error)
                return
            }
            self.notice = successMessage
            self.errorMessage = nil
            self.reload()
            completion(true, nil)
        }
    }

    private func performMutationWithPayload(
        path: String,
        payload: [String: Any],
        completion: @escaping @MainActor @Sendable ([String: Any]?, String?) -> Void
    ) {
        guard allowsMutations else {
            let message = mutationUnavailableMessage
            errorMessage = message
            completion(nil, message)
            return
        }
        guard let url = baseURL?.appendingPathComponent(path) else {
            let message = "本机服务尚未连接。"
            errorMessage = message
            completion(nil, message)
            return
        }
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            let message = "无法编码提交内容。"
            errorMessage = message
            completion(nil, message)
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
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    let message = "提交失败：\(error.localizedDescription)"
                    self.errorMessage = message
                    completion(nil, message)
                    return
                }
                guard let response = response as? HTTPURLResponse else {
                    let message = "提交失败：未收到有效响应。"
                    self.errorMessage = message
                    completion(nil, message)
                    return
                }
                guard (200..<300).contains(response.statusCode) else {
                    let message = "提交失败：\(self.apiErrorMessage(from: data) ?? "服务拒绝了此操作。")"
                    self.errorMessage = message
                    completion(nil, message)
                    return
                }
                let payload = self.apiPayload(from: data)
                self.raiseMinimumRequiredSnapshotRevision(from: data)
                completion(payload, nil)
            }
        }.resume()
    }

    func raiseMinimumRequiredSnapshotRevision(from data: Data?) {
        guard let revision = Self.snapshotRevision(from: data) else { return }
        if let minimumRequiredSnapshotRevision {
            self.minimumRequiredSnapshotRevision = max(minimumRequiredSnapshotRevision, revision)
        } else {
            minimumRequiredSnapshotRevision = revision
        }
        if activeRefreshTask != nil {
            discardedRefreshGeneration = refreshGeneration
            pendingRefresh = true
        }
    }

    private func snapshotRevisionFloorError(for snapshot: BrokerSnapshot) -> BrokerRefreshError? {
        let required = max(
            minimumRequiredSnapshotRevision ?? 0,
            lastGoodSnapshot?.snapshotRevision ?? 0
        )
        guard let received = snapshot.snapshotRevision, received >= required else {
            return .snapshotRevisionBehind(required: required, received: snapshot.snapshotRevision)
        }
        return nil
    }

    private func apiPayload(from data: Data?) -> [String: Any]? {
        guard
            let data,
            let object = try? JSONSerialization.jsonObject(with: data),
            let payload = object as? [String: Any]
        else {
            return nil
        }
        return payload["data"] as? [String: Any] ?? payload
    }

    static func snapshotRevision(from data: Data?) -> Int? {
        guard
            let data,
            let object = try? JSONSerialization.jsonObject(with: data),
            let payload = object as? [String: Any]
        else {
            return nil
        }
        return payload.optionalInt("snapshot_revision")
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
        if let error = payload["error"] as? [String: Any] {
            if let message = error["message"] as? String {
                if let code = error["code"] as? String, !code.isEmpty {
                    return localizedAPIError(code: code, fallback: message)
                }
                return message
            }
            if let code = error["code"] as? String { return localizedAPIError(code: code, fallback: code) }
        }
        if let details = payload["details"] as? [[String: Any]], let first = details.first {
            return first.string("msg") ?? first.string("message")
        }
        return nil
    }

    private func localizedAPIError(code: String, fallback: String) -> String {
        switch code {
        case "endpoint_has_active_leases":
            return "这台服务器仍有正在使用的租约，请先到“租约”归还 GPU。"
        case "endpoint_has_queued_requests":
            return "仍有排队或待批准请求指定了这台服务器，请先取消或调整请求。"
        case "endpoint_has_lease_history":
            return "这台服务器已有租约历史，需要保留登记记录，不能直接删除。"
        case "endpoint_referenced_by_requests":
            return "仍有排队请求指定了这台服务器，请先取消或调整请求。"
        case "endpoint_referenced_by_profiles":
            return "仍有预设任务指定了这台服务器，请先停用或调整预设。"
        case "endpoint_referenced_by_reservations":
            return "仍有预约使用这台服务器，请先取消预约。"
        case "endpoint_referenced_by_maintenance":
            return "这台服务器有维护记录，需要保留登记，不能直接删除。"
        case "endpoint_delete_restricted":
            return "这台服务器仍被受保护的历史记录引用，不能直接删除。"
        case "endpoint_not_found":
            return "这台服务器已经不在本机资源池中。"
        case "idempotency_key_required":
            return "本次操作缺少防重复标识，请重试。"
        case "validation_error":
            return "提交内容不完整或格式不正确，请检查后重试。"
        default:
            return fallback
        }
    }

    func confirmEndpointRemovalAfterMutation(
        _ endpoint: EndpointRecord,
        expectedLifecycleState: String,
        completion: @escaping @MainActor @Sendable (Bool, String?) -> Void
    ) {
        guard snapshotClient != nil else {
            let message = BrokerRefreshError.missingClient.localizedDescription
            errorMessage = message
            completion(false, message)
            return
        }
            pendingEndpointRemovals.append(
                PendingEndpointRemoval(
                    endpointID: endpoint.id,
                    endpointDisplayName: endpoint.displayName,
                    expectedLifecycleState: expectedLifecycleState.uppercased(),
                    completion: completion
                )
            )
        if activeRefreshTask != nil {
            discardedRefreshGeneration = refreshGeneration
            pendingRefresh = true
        } else {
            startRefresh()
        }
    }

    private func resolvePendingEndpointRemovals(with result: Result<BrokerSnapshot, Error>) {
        guard !pendingEndpointRemovals.isEmpty else { return }
        let confirmations = pendingEndpointRemovals
        pendingEndpointRemovals.removeAll()
        switch result {
        case .success(let nextSnapshot):
            for confirmation in confirmations {
                let nextEndpoint = nextSnapshot.endpoints.first {
                    $0.id == confirmation.endpointID
                }
                let reachedExpectedState = nextEndpoint == nil
                    || nextEndpoint?.lifecycleState == confirmation.expectedLifecycleState
                    || nextEndpoint?.monitorStatus == confirmation.expectedLifecycleState
                if !reachedExpectedState {
                    let message = "本机服务尚未确认移除。请刷新状态后再查看。"
                    errorMessage = message
                    confirmation.completion(false, message)
                } else {
                    notice = "已从服务器池移除 \(confirmation.endpointDisplayName)；审计历史仍保留。"
                    errorMessage = nil
                    confirmation.completion(true, nil)
                }
            }
        case .failure(let error):
            let message = "移除后无法刷新状态：\(error.localizedDescription)"
            errorMessage = message
            for confirmation in confirmations {
                confirmation.completion(false, message)
            }
        }
    }

    private var mutationUnavailableMessage: String {
        mutationUnavailableReason
    }

    private static func freshness(for snapshot: BrokerSnapshot) -> BrokerRefreshFreshness {
        guard
            let dataAgeSeconds = snapshot.dataAgeSeconds,
            let freshnessSeconds = snapshot.freshnessSeconds,
            dataAgeSeconds > freshnessSeconds
        else {
            return .fresh
        }
        return .stale
    }

    private static func secondsToNanoseconds(_ seconds: TimeInterval) -> UInt64 {
        UInt64(max(0, seconds) * 1_000_000_000)
    }
}
