import Combine
import Foundation
import XCTest
@testable import ServerPilotCore

@MainActor
final class BrokerStoreTests: XCTestCase {
    func testManualTriggersCoalesceBehindSingleActiveRefresh() async throws {
        let client = DelayedSequenceClient(
            snapshots: [try Self.snapshot(named: "1"), try Self.snapshot(named: "8")],
            delayNanoseconds: 50_000_000
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        store.reload()
        store.reload()

        try await waitUntil { store.snapshot.summary.totalGPUs == 8 && !store.isRefreshing }
        let metrics = await client.metrics()
        XCTAssertEqual(metrics.callCount, 2)
        XCTAssertEqual(metrics.maxConcurrentCalls, 1)
        XCTAssertEqual(store.snapshot.summary.totalGPUs, 8)
        XCTAssertEqual(store.freshness, .fresh)
    }

    func testEquivalentRefreshDoesNotRepublishSnapshotOrLastGoodSnapshot() async throws {
        let initial = try Self.snapshot(named: "1")
        var equivalent = initial
        equivalent.serverTime = "2026-08-10T10:00:00Z"
        let client = DelayedSequenceClient(
            snapshots: [initial, equivalent],
            delayNanoseconds: 5_000_000
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        var snapshotPublicationCount = 0
        var lastGoodPublicationCount = 0
        let snapshotSubscription = store.$snapshot.dropFirst().sink { _ in
            snapshotPublicationCount += 1
        }
        let lastGoodSubscription = store.$lastGoodSnapshot.dropFirst().sink { _ in
            lastGoodPublicationCount += 1
        }
        defer {
            snapshotSubscription.cancel()
            lastGoodSubscription.cancel()
        }

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == initial.snapshotRevision && !store.isRefreshing }
        snapshotPublicationCount = 0
        lastGoodPublicationCount = 0

        store.reload()
        try await waitUntilAsync { await client.metrics().callCount == 2 }
        try await waitUntil { !store.isRefreshing }

        XCTAssertEqual(snapshotPublicationCount, 0)
        XCTAssertEqual(lastGoodPublicationCount, 0)
        XCTAssertEqual(store.snapshot.serverTime, initial.serverTime)
        XCTAssertEqual(store.lastGoodSnapshot?.serverTime, initial.serverTime)
        XCTAssertEqual(store.freshness, .fresh)
        XCTAssertTrue(store.isConnected)
        XCTAssertNil(store.errorMessage)
    }

    func testEquivalentRefreshRecoversFreshnessAndClearsError() async throws {
        let initial = try Self.snapshot(named: "1")
        var equivalent = initial
        equivalent.serverTime = "2026-08-10T10:00:00Z"
        let client = ScriptedClient(results: [
            .success(initial),
            .failure(BrokerRefreshError.invalidSnapshot),
            .success(equivalent)
        ])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == initial.snapshotRevision && !store.isRefreshing }

        store.reload()
        try await waitUntil { store.freshness == .stale && !store.isRefreshing }
        XCTAssertFalse(store.isConnected)
        XCTAssertNotNil(store.errorMessage)

        store.reload()
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        XCTAssertTrue(store.isConnected)
        XCTAssertNil(store.errorMessage)
        XCTAssertEqual(store.snapshot.serverTime, initial.serverTime)
        XCTAssertEqual(store.lastGoodSnapshot?.serverTime, initial.serverTime)
    }

    func testTimeoutDoesNotCommitCancellationIgnoringClient() async throws {
        let client = CancellationIgnoringClient(
            snapshot: try Self.snapshot(named: "8"),
            delayNanoseconds: 200_000_000
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 0.03, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)

        try await waitUntil { store.freshness == .failed }
        XCTAssertEqual(store.snapshot, .empty)
        XCTAssertFalse(store.isRefreshing)

        try await Task.sleep(nanoseconds: 260_000_000)
        XCTAssertEqual(store.snapshot, .empty)
        XCTAssertEqual(store.freshness, .failed)
        let callCount = await client.metrics()
        XCTAssertEqual(callCount, 1)
    }

    func testLastGoodSnapshotSurvivesStaleFailureAndThenRecovers() async throws {
        let client = ScriptedClient(results: [
            .success(try Self.snapshot(named: "1")),
            .failure(BrokerRefreshError.invalidSnapshot),
            .success(try Self.snapshot(named: "8"))
        ])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.summary.totalGPUs == 1 }

        store.reload()
        try await waitUntil { store.freshness == .stale }
        XCTAssertEqual(store.snapshot.summary.totalGPUs, 1)
        XCTAssertEqual(store.lastGoodSnapshot?.summary.totalGPUs, 1)
        XCTAssertNotNil(store.errorMessage)

        store.reload()
        try await waitUntil { store.snapshot.summary.totalGPUs == 8 }
        XCTAssertEqual(store.freshness, .fresh)
        XCTAssertNil(store.errorMessage)
    }

    func testFailureBeforeAnySnapshotHasFailedFreshness() async throws {
        let client = ScriptedClient(results: [.failure(BrokerRefreshError.invalidSnapshot)])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)

        try await waitUntil { store.freshness == .failed }
        XCTAssertEqual(store.snapshot, .empty)
        XCTAssertNil(store.lastGoodSnapshot)
        XCTAssertNotNil(store.errorMessage)
    }

    func testSuccessfulSnapshotKeepsControlPlaneFreshWhenOneEndpointHasOldTelemetry() async throws {
        let client = ScriptedClient(results: [.success(try Self.snapshot(named: "stale"))])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(
            snapshotClient: client,
            baseURL: URL(string: "http://127.0.0.1:8787/")
        )

        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }
        XCTAssertTrue(store.isConnected)
        XCTAssertTrue(store.allowsMutations)
        XCTAssertEqual(store.snapshot.dataAgeSeconds, 94)
        XCTAssertEqual(store.snapshot.freshnessSeconds, 30)
        XCTAssertEqual(store.snapshot.endpoints.first?.monitorLabel, "更新中断")
        XCTAssertEqual(store.snapshot.endpoints.first?.monitorDetail, "服务器状态未按计划更新")
    }

    func testEndpointFailureExplainsARecordedObservationTimeout() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.useFixture(snapshot: try Self.snapshot(named: "error"))

        XCTAssertEqual(store.snapshot.endpoints.first?.monitorLabel, "连接失败")
        XCTAssertEqual(
            store.snapshot.endpoints.first?.monitorDetail,
            "连接或更新超时 · 检查服务器和 SSH"
        )
    }

    func testFixtureWithOldEndpointTelemetryRemainsLoadedAndReadOnly() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.useFixture(snapshot: try Self.snapshot(named: "stale"))

        XCTAssertEqual(store.freshness, .fresh)
        XCTAssertTrue(store.isConnected)
        XCTAssertFalse(store.allowsMutations)
    }

    func testFixtureCommunicatesFixedReadOnlyBehavior() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.useFixture(snapshot: try Self.snapshot(named: "8"))

        XCTAssertEqual(store.freshness, .fresh)
        XCTAssertFalse(store.allowsMutations)
        XCTAssertFalse(store.canRefresh)
        XCTAssertEqual(
            store.mutationUnavailableReason,
            "当前为只读测试夹具或尚未连接本机服务，不能执行资源变更。"
        )
    }

    func testEndpointDraftUsesStructuredSealedObservationProfile() throws {
        let draft = try EndpointDraft(
            host: "gpu.example.test",
            port: 2201,
            sshUser: "collector",
            workspacePath: "/srv/storyboard",
            observationProfile: .serverScript,
            suppliedID: ""
        )

        XCTAssertEqual(draft.id, "gpu-example-test-p2201")
        XCTAssertEqual(draft.host, "gpu.example.test")
        XCTAssertEqual(draft.workspacePath, "/srv/storyboard")
        XCTAssertEqual(draft.observationProfile, .serverScript)
        XCTAssertEqual(draft.observationProfile.label, "服务器采集脚本")
        XCTAssertFalse(draft.observationProfile.scriptInfo.contains("Docker"))
        XCTAssertThrowsError(
            try EndpointDraft(
                host: "",
                port: 0,
                sshUser: "collector",
                workspacePath: "relative/path",
                observationProfile: .linuxNvidia,
                suppliedID: "bad id"
            )
        )
    }

    func testURLSessionClientFetchesUnifiedStateRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = URLSessionBrokerSnapshotClient(
            baseURL: URL(string: "http://broker.test/")!,
            session: session
        )
        let fixtureData = try Data(contentsOf: Self.fixturesRoot.appendingPathComponent("1.json"))
        StateRouteURLProtocol.responseData = fixtureData
        defer { StateRouteURLProtocol.reset() }

        let snapshot = try await client.snapshot(actorID: "tester")

        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/state")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Actor"), "tester")
        XCTAssertEqual(snapshot.snapshotRevision, 101)
        XCTAssertEqual(snapshot.summary.totalGPUs, 1)
    }

    func testURLSessionEndpointTelemetryHistoryClientUsesSeparateOptionalRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = URLSessionEndpointTelemetryHistoryClient(
            baseURL: URL(string: "http://broker.test/")!,
            session: session
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "schema_version": "v1",
            "server_time": "2026-08-06T08:10:00Z",
            "data": [
                "endpoint_id": "gpu-node-01",
                "window_seconds": 3600,
                "point_count": 1,
                "points": [
                    [
                        "observed_at": "2026-08-06T08:00:00Z",
                        "cpu_count": 64,
                        "load_1m": 16,
                        "cpu_utilization_pct": 25,
                        "memory_total_mib": 100,
                        "memory_available_mib": 60,
                        "memory_used_pct": 40
                    ]
                ],
                "gpu_series": [
                    [
                        "gpu_id": "gpu-node-01:GPU-uuid-0",
                        "gpu_uuid": "GPU-uuid-0",
                        "gpu_index": 0,
                        "label": "GPU 0",
                        "points": [
                            [
                                "observed_at": "2026-08-06T08:00:00Z",
                                "gpu_utilization_pct": 80,
                                "memory_used_pct": 25,
                                "memory_used_mib": 20000,
                                "memory_total_mib": 80000
                            ]
                        ]
                    ]
                ]
            ]
        ])
        defer { StateRouteURLProtocol.reset() }

        let history = try await client.history(endpointID: "gpu-node-01", range: .oneHour, actorID: "tester")

        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/endpoints/gpu-node-01/history")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.query, "window_seconds=3600&points=120")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Actor"), "tester")
        XCTAssertEqual(history.endpointID, "gpu-node-01")
        XCTAssertEqual(history.range, .oneHour)
        XCTAssertEqual(history.samples.first?.cpuLoadFraction, 0.25)
        XCTAssertEqual(history.samples.first?.memoryFraction, 0.40)
        XCTAssertNil(history.samples.first?.gpuUtilizationFraction)
        XCTAssertEqual(history.gpuSeries.count, 1)
        XCTAssertEqual(history.gpuSeries.first?.id, "gpu-node-01:GPU-uuid-0")
        XCTAssertEqual(history.gpuSeries.first?.samples.first?.gpuUtilizationFraction, 0.8)
        XCTAssertEqual(history.gpuSeries.first?.samples.first?.memoryFraction, 0.25)
    }

    func testEndpointTelemetryHistoryCapabilityGateDegradesWithoutChangingSnapshot() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        let snapshot = try Self.snapshot(named: "1")

        store.useFixture(snapshot: snapshot)
        store.requestEndpointTelemetryHistory(endpointID: "fixture-1", range: .oneHour)

        XCTAssertEqual(store.snapshot, snapshot)
        XCTAssertEqual(store.endpointTelemetryHistoryErrors["fixture-1"], "当前本机服务未提供资源历史能力。")
        XCTAssertFalse(store.endpointTelemetryHistoryLoading.contains("fixture-1"))
    }

    func testEndpointTelemetryHistoryCancelsOlderRequestAndIgnoresOutOfOrderCompletion() async throws {
        let endpointID = "fixture-1"
        let snapshotClient = ScriptedClient(results: [.success(try Self.snapshot(named: "1"))])
        let historyClient = DelayedEndpointHistoryClient(
            results: [
                .success(.init(endpointID: endpointID, range: .twentyFourHours, samples: [
                    EndpointTelemetrySample(raw: [
                        "timestamp": "2026-08-06T06:00:00Z",
                        "gpu_utilization_pct": 10
                    ])!
                ], generatedAt: nil)),
                .success(.init(endpointID: endpointID, range: .oneHour, samples: [
                    EndpointTelemetrySample(raw: [
                        "timestamp": "2026-08-06T08:00:00Z",
                        "gpu_utilization_pct": 80
                    ])!
                ], generatedAt: nil))
            ],
            delaysNanoseconds: [180_000_000, 20_000_000]
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.connectForTesting(
            snapshotClient: snapshotClient,
            endpointTelemetryHistoryClient: historyClient,
            serviceInfo: ServiceInfo(
                schemaVersion: "v1",
                version: "test",
                capabilities: ["instant_claims", "endpoint_telemetry_history"]
            )
        )

        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .twentyFourHours)
        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .oneHour)

        try await waitUntil {
            store.endpointTelemetryHistory(endpointID: endpointID, range: .oneHour)?.samples.first?.gpuUtilizationFraction == 0.8
                && !store.endpointTelemetryHistoryLoading.contains(endpointID)
        }
        try await Task.sleep(nanoseconds: 220_000_000)

        XCTAssertEqual(store.endpointTelemetryHistory(endpointID: endpointID, range: .oneHour)?.samples.first?.gpuUtilizationFraction, 0.8)
        XCTAssertNil(store.endpointTelemetryHistory(endpointID: endpointID, range: .twentyFourHours))
    }

    func testEndpointTelemetryHistoryReusesFreshCachedRange() async throws {
        let endpointID = "fixture-1"
        let snapshotClient = ScriptedClient(results: [.success(try Self.snapshot(named: "1"))])
        let historyClient = CountingEndpointHistoryClient()
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.connectForTesting(
            snapshotClient: snapshotClient,
            endpointTelemetryHistoryClient: historyClient,
            serviceInfo: ServiceInfo(
                schemaVersion: "v1",
                version: "test",
                capabilities: ["endpoint_telemetry_history"]
            )
        )

        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .oneHour)
        try await waitUntil {
            store.endpointTelemetryHistory(endpointID: endpointID, range: .oneHour) != nil
                && !store.endpointTelemetryHistoryLoading.contains(endpointID)
        }
        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .oneHour)

        XCTAssertEqual(await historyClient.callCount(), 1)
        XCTAssertNil(store.endpointTelemetryHistoryErrors[endpointID])
    }

    func testEndpointTelemetryHistoryCancelsInactiveDetailRequest() async throws {
        let firstEndpointID = "fixture-1"
        let secondEndpointID = "fixture-2"
        let snapshotClient = ScriptedClient(results: [.success(try Self.snapshot(named: "1"))])
        let historyClient = CooperativeDelayedEndpointHistoryClient(
            delaysNanoseconds: [500_000_000, 5_000_000]
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.connectForTesting(
            snapshotClient: snapshotClient,
            endpointTelemetryHistoryClient: historyClient,
            serviceInfo: ServiceInfo(
                schemaVersion: "v1",
                version: "test",
                capabilities: ["endpoint_telemetry_history"]
            )
        )

        store.requestEndpointTelemetryHistory(endpointID: firstEndpointID, range: .oneHour)
        try await waitUntilAsync { await historyClient.metrics().callCount == 1 }
        XCTAssertTrue(store.endpointTelemetryHistoryLoading.contains(firstEndpointID))

        store.requestEndpointTelemetryHistory(endpointID: secondEndpointID, range: .oneHour)
        try await waitUntil {
            store.endpointTelemetryHistory(endpointID: secondEndpointID, range: .oneHour) != nil
                && !store.endpointTelemetryHistoryLoading.contains(secondEndpointID)
        }
        try await waitUntilAsync { await historyClient.metrics().cancellationCount == 1 }

        XCTAssertFalse(store.endpointTelemetryHistoryLoading.contains(firstEndpointID))
        XCTAssertNil(store.endpointTelemetryHistory(endpointID: firstEndpointID, range: .oneHour))
        XCTAssertNil(store.endpointTelemetryHistoryErrors[firstEndpointID])
    }

    func testUnifiedStateEnvelopeParsesCurrentAndHistory() throws {
        let history = [
            "summary": ["total_gpus": 1],
            "data_age_seconds": 8,
            "freshness_seconds": 30
        ] as [String: Any]
        let snapshot = BrokerSnapshot(envelope: [
            "schema_version": "v1",
            "snapshot_revision": 150,
            "server_time": "2026-08-06T08:00:00Z",
            "data": [
                "current": [
                    "summary": ["total_gpus": 8],
                    "endpoints": [],
                    "gpus": [],
                    "leases": [],
                    "requests": [],
                    "reservations": [],
                    "data_age_seconds": 2,
                    "freshness_seconds": 30,
                    "admission_boundary": "test"
                ],
                "history": [
                    "resource_plan_evaluations": [],
                    "resource_run_actuals": [
                        [
                            "id": "actual-history",
                            "actor_id": "agent-a",
                            "project_id": "project-a",
                            "task_ref": "train",
                            "quantities": ["gpu_count": 1],
                            "actual_duration_seconds": 1760
                        ]
                    ],
                    "summary_samples": [
                        history
                    ]
                ]
            ]
        ])

        XCTAssertEqual(snapshot.snapshotRevision, 150)
        XCTAssertEqual(snapshot.summary.totalGPUs, 8)
        XCTAssertEqual(snapshot.history.resourceRunActuals.count, 1)
        XCTAssertEqual(snapshot.resourceRunActuals.first?.id, "actual-history")
        XCTAssertEqual(snapshot.resourceRunActuals.first?.actualDurationSeconds, 1760)
    }

    func testMutationRevisionFloorRejectsRollbackSnapshotAndLaterCommitsRequiredRevision() async throws {
        var beforeMutation = try Self.snapshot(named: "1")
        beforeMutation.snapshotRevision = 101
        var rollback = try Self.snapshot(named: "8")
        rollback.snapshotRevision = 120
        var afterMutation = try Self.snapshot(named: "8")
        afterMutation.snapshotRevision = 150
        let client = ScriptedClient(results: [
            .success(beforeMutation),
            .success(rollback),
            .success(afterMutation)
        ])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == 101 && !store.isRefreshing }

        let mutationResponse = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 150])
        store.raiseMinimumRequiredSnapshotRevision(from: mutationResponse)
        store.reload()
        try await waitUntil { store.freshness == .stale && !store.isRefreshing }

        XCTAssertEqual(store.snapshot.snapshotRevision, 101)
        XCTAssertEqual(store.lastGoodSnapshot?.snapshotRevision, 101)
        XCTAssertEqual(
            store.errorMessage,
            "无法更新资源：\(BrokerRefreshError.snapshotRevisionBehind(required: 150, received: 120).localizedDescription)"
        )

        store.reload()
        try await waitUntil { store.snapshot.snapshotRevision == 150 && !store.isRefreshing }

        XCTAssertEqual(store.freshness, .fresh)
        XCTAssertNil(store.errorMessage)
    }

    func testOrdinaryRefreshRejectsRevisionRollbackAndPreservesLastGoodSnapshot() async throws {
        var accepted = try Self.snapshot(named: "8")
        accepted.snapshotRevision = 150
        var rollback = try Self.snapshot(named: "1")
        rollback.snapshotRevision = 120
        let client = ScriptedClient(results: [.success(accepted), .success(rollback)])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == 150 && !store.isRefreshing }

        store.reload()
        try await waitUntil { store.freshness == .stale && !store.isRefreshing }

        XCTAssertEqual(store.snapshot.snapshotRevision, 150)
        XCTAssertEqual(store.lastGoodSnapshot?.snapshotRevision, 150)
        XCTAssertEqual(
            store.errorMessage,
            "无法更新资源：\(BrokerRefreshError.snapshotRevisionBehind(required: 150, received: 120).localizedDescription)"
        )
    }

    func testEndpointLifecycleActionsUseDistinctDocumentedRoutes() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let endpoint = try XCTUnwrap(snapshot.endpoints.first)
        let capabilities: Set<String> = ["endpoint_update", "endpoint_pause_resume", "endpoint_keepalive", "endpoint_conflict_cleanup"]
        let serviceInfo = ServiceInfo(schemaVersion: "v1", capabilities: capabilities)
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 102])
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot), .success(snapshot), .success(snapshot), .success(snapshot), .success(snapshot)]),
            serviceInfo: serviceInfo,
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let updateRecorder = CompletionRecorder()
        store.updateEndpoint(
            endpoint,
            draft: try EndpointUpdateDraft(
                sshUser: "collector",
                workspacePath: "/srv/storyboard",
                observationProfile: .serverScript
            )
        ) { success, message in
            updateRecorder.success = success
            updateRecorder.message = message
        }
        try await waitUntil { updateRecorder.success != nil }
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "PATCH")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/endpoints/fixture-1")
        let updateBody = try XCTUnwrap(StateRouteURLProtocol.lastRequest?.httpBody)
        let updatePayload = try XCTUnwrap(try JSONSerialization.jsonObject(with: updateBody) as? [String: Any])
        XCTAssertEqual(updatePayload["ssh_user"] as? String, "collector")
        XCTAssertEqual(updatePayload["workspace_path"] as? String, "/srv/storyboard")
        XCTAssertEqual(updatePayload["observation_profile"] as? String, "server-script-v1")

        let pauseRecorder = CompletionRecorder()
        store.pauseEndpoint(endpoint) { success, message in
            pauseRecorder.success = success
            pauseRecorder.message = message
        }
        try await waitUntil { pauseRecorder.success != nil }
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/endpoints/fixture-1/pause")

        let resumeRecorder = CompletionRecorder()
        store.resumeEndpoint(endpoint) { success, message in
            resumeRecorder.success = success
            resumeRecorder.message = message
        }
        try await waitUntil { resumeRecorder.success != nil }
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/endpoints/fixture-1/resume")

        let keepaliveRecorder = CompletionRecorder()
        store.setEndpointKeepalive(endpoint, enabled: true) { success, message in
            keepaliveRecorder.success = success
            keepaliveRecorder.message = message
        }
        try await waitUntil { keepaliveRecorder.success != nil }
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/endpoints/fixture-1/keepalive")
        let keepaliveBody = try XCTUnwrap(StateRouteURLProtocol.lastRequest?.httpBody)
        let keepalivePayload = try XCTUnwrap(try JSONSerialization.jsonObject(with: keepaliveBody) as? [String: Any])
        XCTAssertEqual(keepalivePayload["enabled"] as? Bool, true)
        XCTAssertEqual(Set(keepalivePayload.keys), Set(["enabled"]))

        let conflictRecorder = CompletionRecorder()
        store.clearEmptyConflictedLease(
            endpointID: endpoint.id,
            leaseID: "lease-conflict"
        ) { success, message in
            conflictRecorder.success = success
            conflictRecorder.message = message
        }
        try await waitUntil { conflictRecorder.success != nil }
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(
            StateRouteURLProtocol.lastRequest?.url?.path,
            "/api/v1/endpoints/fixture-1/leases/lease-conflict/release-empty"
        )

    }

    func testHumanTaskGPUReassignmentUsesExactPatchRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let lease = try XCTUnwrap(LeaseRecord(raw: [
            "id": "lease-manual-move",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "HELD",
            "gpu_ids": ["fixture-1:GPU-old"],
        ]))
        let store = BrokerStore(
            actorID: "human",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(
            withJSONObject: ["snapshot_revision": 103]
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot), .success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: []),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let recorder = CompletionRecorder()
        store.reassignLease(lease, gpuIDs: ["fixture-1:GPU-new"]) { success, message in
            recorder.success = success
            recorder.message = message
        }

        try await waitUntil { recorder.success != nil }
        XCTAssertEqual(recorder.success, true)
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "PATCH")
        XCTAssertEqual(
            StateRouteURLProtocol.lastRequest?.url?.path,
            "/api/v1/leases/lease-manual-move/gpus"
        )
        let body = try XCTUnwrap(StateRouteURLProtocol.lastRequest?.httpBody)
        let payload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: body) as? [String: Any]
        )
        XCTAssertEqual(payload["gpu_ids"] as? [String], ["fixture-1:GPU-new"])
        XCTAssertTrue(store.notice?.contains("对应 Agent") == true)
    }

    func testHumanReleaseUsesOperatorCorrectionRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let lease = try XCTUnwrap(LeaseRecord(raw: [
            "id": "lease-owned-by-agent",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "HELD",
            "gpu_ids": ["fixture-1:GPU-old"],
        ]))
        let store = BrokerStore(
            actorID: "human",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(
            withJSONObject: ["snapshot_revision": 104]
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot), .success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: []),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let recorder = CompletionRecorder()
        store.releaseLease(lease) { success, message in
            recorder.success = success
            recorder.message = message
        }

        try await waitUntil { recorder.success != nil }
        XCTAssertEqual(recorder.success, true)
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(
            StateRouteURLProtocol.lastRequest?.url?.path,
            "/api/v1/operator/leases/lease-owned-by-agent/release"
        )
        XCTAssertEqual(
            StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Actor"),
            "human"
        )
        XCTAssertEqual(
            StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Client"),
            "desktop-app"
        )
    }

    func testCollectorIntervalReadsAndUpdatesServerSetting() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: session
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot), .success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["collector_settings"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "data": [
                "interval_seconds": 10,
                "stale_after_seconds": 30,
                "allowed_intervals": [5, 10, 30],
            ]
        ])
        store.requestCollectorSettings()
        try await waitUntil { store.collectorSettings?.intervalSeconds == 10 }
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "GET")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/settings/collector")

        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "snapshot_revision": 102,
            "settings": [
                "interval_seconds": 5,
                "stale_after_seconds": 15,
                "allowed_intervals": [5, 10, 30],
            ]
        ])
        let recorder = CompletionRecorder()
        store.updateCollectorInterval(5) { success, message in
            recorder.success = success
            recorder.message = message
        }
        try await waitUntil { recorder.success != nil }
        XCTAssertEqual(recorder.success, true)
        XCTAssertEqual(store.collectorSettings?.intervalSeconds, 5)
        XCTAssertEqual(store.collectorSettings?.staleAfterSeconds, 15)
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.httpMethod, "PATCH")
        XCTAssertEqual(StateRouteURLProtocol.lastRequest?.url?.path, "/api/v1/settings/collector")
        XCTAssertNotNil(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "Idempotency-Key"))
    }

    func testCollectorIntervalRemainsVisibleButReadOnlyInFixtureMode() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.useFixture(snapshot: try Self.snapshot(named: "1"))

        XCTAssertTrue(store.supportsCollectorSettings)
        XCTAssertFalse(store.canUpdateCollectorSettings)
        XCTAssertEqual(store.collectorSettings?.intervalSeconds, 10)
        XCTAssertEqual(store.collectorSettings?.allowedIntervals, [5, 10, 30])
    }

    func testEndpointAndControlPlaneMutationsStayAvailableWhenOneEndpointHasOldTelemetry() async throws {
        let client = ScriptedClient(results: [.success(try Self.snapshot(named: "stale"))])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(
            snapshotClient: client,
            baseURL: URL(string: "http://127.0.0.1:8787/")
        )

        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }
        XCTAssertTrue(store.allowsMutations)
        XCTAssertTrue(store.allowsEndpointLifecycleMutations)
    }

    func testEndpointLifecycleCapabilityGateAndErrorParsingRemainSafe() throws {
        let payload = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 250])
        XCTAssertEqual(BrokerStore.snapshotRevision(from: payload), 250)

        let errorPayload = try JSONSerialization.data(withJSONObject: [
            "error": ["code": "endpoint_not_found"]
        ])
        XCTAssertEqual(BrokerStore.apiErrorCode(from: errorPayload), "endpoint_not_found")
    }

    func testKeepaliveSnapshotUsesPerGPUCoverageAndRedactsInternalLease() throws {
        let fixture = try Self.snapshot(named: "keepalive")
        let endpoint = try XCTUnwrap(fixture.endpoints.first)
        XCTAssertTrue(endpoint.keepalive.configured)
        XCTAssertEqual(endpoint.keepalive.policy, "idle_keepalive")
        XCTAssertEqual(endpoint.keepalive.state, "ON")
        XCTAssertEqual(endpoint.keepalive.label, "已开启")
        XCTAssertTrue(endpoint.keepalive.hasResidualLease)
        XCTAssertEqual(endpoint.keepalive.coverageSummary(totalGPUCount: 2, taskGPUCount: 1), "已开启 · 1/2 占卡，1 卡任务中")
        XCTAssertEqual(endpoint.keepalive.coverageSummary(totalGPUCount: 0, taskGPUCount: 0), "无 GPU")
        XCTAssertEqual(fixture.gpus.map(\.state), ["KEEPALIVE", "RUNNING_MANAGED"])
        XCTAssertTrue(try XCTUnwrap(fixture.gpus.first).isPubliclyAvailable)
        XCTAssertEqual(fixture.gpus.first?.keepalive.desired, "ON")
        XCTAssertEqual(fixture.gpus.first?.keepalive.presentationLabel, "空闲占卡")
        XCTAssertEqual(fixture.gpus.last?.keepalive.reason, "managed workload is running")
        XCTAssertTrue(fixture.leases.isEmpty)

        let missingKeeper = try XCTUnwrap(GPURecord(raw: [
            "id": "fixture-1:GPU-missing-keeper",
            "endpoint_id": "fixture-1",
            "gpu_uuid": "GPU-missing-keeper",
            "gpu_index": 2,
            "name": "Fixture GPU",
            "total_vram_mib": 81920,
            "state": "AVAILABLE",
            "keepalive": [
                "configured": true,
                "policy": "idle_keepalive",
                "desired": "ON",
                "actual": "OFF",
                "state": "OFF",
                "lease_id": "keepalive-missing-lease"
            ],
        ]))
        XCTAssertFalse(missingKeeper.isTaskOccupancy)
        XCTAssertTrue(missingKeeper.isPubliclyAvailable)
        XCTAssertEqual(missingKeeper.keepalive.desired, "ON")
        XCTAssertEqual(missingKeeper.keepalive.presentationLabel, "占卡未运行")
        XCTAssertEqual(missingKeeper.keepalive.leaseID, "keepalive-missing-lease")
        XCTAssertTrue(try XCTUnwrap(fixture.gpus.last).isTaskOccupancy)

        let conflictedKeeper = try XCTUnwrap(GPURecord(raw: [
            "id": "fixture-1:GPU-conflicted-keeper",
            "endpoint_id": "fixture-1",
            "gpu_uuid": "GPU-conflicted-keeper",
            "gpu_index": 3,
            "name": "Fixture GPU",
            "total_vram_mib": 81920,
            "state": "CONFLICT",
            "publicly_available": false,
            "public_status": "任务使用中",
            "keepalive": [
                "configured": true,
                "policy": "idle_keepalive",
                "desired": "ON",
                "actual": "ERROR",
                "reason": "占卡进程身份尚未建立",
                "lease_id": "keepalive-conflicted-lease"
            ],
        ]))
        XCTAssertEqual(conflictedKeeper.publicStatus, "任务使用中")
        XCTAssertEqual(conflictedKeeper.projectedPubliclyAvailable, false)
        XCTAssertFalse(conflictedKeeper.isPubliclyAvailable)
        XCTAssertEqual(conflictedKeeper.keepalive.desired, "ON")
        XCTAssertEqual(conflictedKeeper.keepalive.state, "ERROR")

        XCTAssertNil(GPURecord(raw: [
            "id": "fixture-1:GPU-invalid-public-projection",
            "endpoint_id": "fixture-1",
            "gpu_uuid": "GPU-invalid-public-projection",
            "gpu_index": 4,
            "name": "Fixture GPU",
            "total_vram_mib": 81920,
            "state": "CONFLICT",
            "publicly_available": false,
            "public_status": "可用 · 占卡异常",
            "keepalive": [
                "configured": true,
                "policy": "idle_keepalive",
                "desired": "ON",
                "actual": "ERROR"
            ],
        ]))

        let defensiveSnapshot = BrokerSnapshot(envelope: [
            "data": [
                "summary": [:],
                "leases": [[
                    "id": "internal-keepalive",
                    "actor_id": "__serverpilot_system__",
                    "project_id": "__serverpilot_system__",
                    "kind": "keepalive",
                    "state": "ACTIVE",
                    "runtime_state": "RUNNING",
                    "gpu_ids": []
                ]]
            ]
        ])
        XCTAssertTrue(defensiveSnapshot.leases.isEmpty)
    }

    func testKeepaliveProtocolRejectsUnknownPolicyAndState() {
        XCTAssertNil(EndpointKeepaliveSummary(
            raw: ["configured": true, "policy": "unknown", "state": "OFF"],
            fallbackConfigured: true
        ))
        XCTAssertNil(EndpointKeepaliveSummary(
            raw: ["configured": true, "policy": "disabled", "state": "UNKNOWN"],
            fallbackConfigured: true
        ))
        XCTAssertNil(GPUKeepaliveStatus(
            raw: ["policy": "unknown", "state": "OFF"],
            fallbackConfigured: true,
            fallbackState: "OFF"
        ))
        XCTAssertNil(GPUKeepaliveStatus(
            raw: ["policy": "disabled", "state": "UNKNOWN"],
            fallbackConfigured: true,
            fallbackState: "OFF"
        ))
        XCTAssertNil(GPUKeepaliveStatus(
            raw: ["policy": "idle_keepalive", "desired": "UNKNOWN", "state": "OFF"],
            fallbackConfigured: true,
            fallbackState: "OFF"
        ))
    }

    func testStableSelectionFallsBackToFirstAvailableRecord() throws {
        let snapshot = try Self.snapshot(named: "queued")

        XCTAssertEqual(snapshot.stableEndpointSelection(currentID: "missing"), "fixture-queued")
        XCTAssertEqual(snapshot.stableEndpointSelection(currentID: "fixture-queued"), "fixture-queued")
        XCTAssertEqual(snapshot.stableRequestSelection(currentID: "missing"), "request-queued")
        XCTAssertEqual(BrokerSnapshot.empty.stableEndpointSelection(currentID: "missing"), "")
    }

    func testGeneralResourceMonitoringProjectionParsesAndKeepsSchedulerPendingSeparate() throws {
        let snapshot = BrokerSnapshot(envelope: [
            "schema_version": "v1",
            "snapshot_revision": 42,
            "server_time": "2026-08-04T00:00:00Z",
            "data": [
                "summary": [:],
                "resource_providers": [
                    [
                        "id": "host:fixture",
                        "provider_type": "host-capacity",
                        "display_name": "fixture host",
                        "state": "ONLINE",
                        "total": ["cpu_cores": 32, "memory_mib": 131072],
                        "committed": ["cpu_cores": 8, "memory_mib": 32768],
                        "available": ["cpu_cores": 24, "memory_mib": 98304]
                    ],
                    [
                        "id": "scheduler:scheduler-a",
                        "provider_type": "scheduler",
                        "display_name": "Example scheduler",
                        "state": "PENDING",
                        "available": ["node_count": 2, "scheduler_units": 2]
                    ]
                ],
                "allocatable_units": [
                    [
                        "id": "scheduler-target:scheduler-a",
                        "provider_id": "scheduler:scheduler-a",
                        "unit_type": "scheduler-target",
                        "state": "PENDING",
                        "quantities": ["node_count": 2, "scheduler_units": 2]
                    ]
                ],
                "scheduler_targets": [
                    [
                        "id": "scheduler-a",
                        "display_name": "Example scheduler",
                        "kind": "external-scheduler",
                        "adapter": "slurm",
                        "enabled": true,
                        "last_access": ["status": "access_required", "message": "VPN required"]
                    ]
                ],
                "scheduler_jobs": [
                    [
                        "id": "job-1",
                        "target_id": "scheduler-a",
                        "actor_id": "agent-a",
                        "project_id": "project-a",
                        "task_ref": "train",
                        "state": "pending",
                        "raw_state": "PENDING",
                        "scheduler_job_id": "12345"
                    ]
                ],
                "scheduler_transfers": [
                    [
                        "id": "transfer-1",
                        "target_id": "scheduler-a",
                        "actor_id": "agent-a",
                        "project_id": "project-a",
                        "state": "completed",
                        "remote_directory": "/scratch/project-a"
                    ]
                ],
                "resource_claims": [
                    [
                        "id": "claim-1",
                        "actor_id": "agent-a",
                        "project_id": "project-a",
                        "task_ref": "train",
                        "state": "active",
                        "runtime_state": "RUNNING",
                        "native_lease_ids": ["lease-1"],
                        "native_request_ids": ["request-1"],
                        "provider_type": "host-capacity",
                        "quantities": ["cpu_cores": 4, "memory_mib": 8192]
                    ]
                ],
                "resource_plan_evaluations": [
                    [
                        "id": "eval-1",
                        "actor_id": "agent-a",
                        "project_id": "project-a",
                        "task_ref": "train",
                        "selected_candidate_key": "small",
                        "minimum_saved_seconds": 120,
                        "minimum_saved_ratio": 0.10,
                        "candidates": [
                            [
                                "candidate_key": "small",
                                "provider_type": "host-capacity",
                                "quantities": ["cpu_cores": 4, "memory_mib": 8192],
                                "predicted_runtime_seconds": 1800,
                                "predicted_saved_seconds": 0,
                                "predicted_saved_ratio": 0,
                                "selected": true
                            ],
                            [
                                "candidate_key": "large",
                                "provider_type": "host-capacity",
                                "quantities": ["cpu_cores": 8, "memory_mib": 16384],
                                "predicted_runtime_seconds": 1720,
                                "predicted_saved_seconds": 80,
                                "predicted_saved_ratio": 0.04,
                                "rejection_reason": "below marginal benefit"
                            ]
                        ]
                    ]
                ],
                "resource_run_actuals": [
                    [
                        "id": "actual-1",
                        "evaluation_id": "eval-1",
                        "actor_id": "agent-a",
                        "project_id": "project-a",
                        "task_ref": "train",
                        "quantities": ["cpu_cores": 4, "memory_mib": 8192],
                        "predicted_duration_seconds": 1800,
                        "actual_duration_seconds": 1760
                    ]
                ],
                "data_age_seconds": 2,
                "freshness_seconds": 30,
                "admission_boundary": "test"
            ]
        ])

        XCTAssertEqual(snapshot.monitoringProviders.count, 2)
        XCTAssertEqual(snapshot.monitoringProviders.first?.available.compactLabel, "24 CPU · 96 GB RAM")
        let scheduler = try XCTUnwrap(snapshot.monitoringProviders.last)
        XCTAssertEqual(scheduler.providerType, "scheduler")
        XCTAssertEqual(scheduler.trustBoundary, "等待外部调度器确认；不计入裸机可用容量")
        XCTAssertEqual(snapshot.allocatableUnits.first?.unitType, "scheduler-target")
        XCTAssertEqual(snapshot.schedulerTargets.first?.id, "scheduler-a")
        XCTAssertEqual(snapshot.schedulerTargets.first?.accessStatus, "ACCESS_REQUIRED")
        XCTAssertEqual(snapshot.schedulerJobs.first?.targetID, "scheduler-a")
        XCTAssertEqual(snapshot.schedulerJobs.first?.state, "PENDING")
        XCTAssertEqual(snapshot.schedulerTransfers.first?.targetID, "scheduler-a")
        XCTAssertEqual(snapshot.schedulerTransfers.first?.state, "COMPLETED")
        XCTAssertEqual(snapshot.resourceClaims.first?.quantities.compactLabel, "4 CPU · 8 GB RAM")
        XCTAssertEqual(snapshot.resourceClaims.first?.state, "ACTIVE")
        XCTAssertEqual(snapshot.resourceClaims.first?.runtimeState, "RUNNING")
        XCTAssertEqual(snapshot.resourceClaims.first?.stateLabel, "运行中")
        XCTAssertEqual(snapshot.resourceClaims.first?.nativeLeaseIDs, ["lease-1"])
        XCTAssertEqual(snapshot.resourceClaims.first?.nativeRequestIDs, ["request-1"])
        XCTAssertEqual(snapshot.resourcePlanEvaluations.first?.selectedCandidate?.candidateKey, "small")
        XCTAssertEqual(snapshot.resourceRunActuals.first?.actualDurationSeconds, 1760)
    }

    func testFixturesResolveInsideDesktopFixturesAndRejectProjectState() throws {
        let fixturesRoot = Self.fixturesRoot
        let projectRoot = fixturesRoot.deletingLastPathComponent().deletingLastPathComponent()

        let fixtureURL = try FixtureSnapshots.resolve("64", fixturesRoot: fixturesRoot, projectRoot: projectRoot)
        XCTAssertEqual(try FixtureSnapshots.load(from: fixtureURL).summary.totalGPUs, 64)

        let stateURL = projectRoot.appendingPathComponent("state/live.json").path
        XCTAssertThrowsError(try FixtureSnapshots.resolve(stateURL, fixturesRoot: fixturesRoot, projectRoot: projectRoot)) { error in
            XCTAssertEqual(error as? FixtureSnapshotError, .rejectedProductionState(URL(fileURLWithPath: stateURL).standardizedFileURL))
        }
    }

    func testEndpointHistoryFixtureIsSeparateFromAllocationSnapshotFixture() throws {
        let fixturesRoot = Self.fixturesRoot
        let historyURL = try FixtureSnapshots.resolve("8-history", fixturesRoot: fixturesRoot)

        let history = try FixtureSnapshots.loadEndpointTelemetryHistory(from: historyURL)

        XCTAssertEqual(history.endpointID, "fixture-8")
        XCTAssertEqual(history.range, .oneHour)
        XCTAssertEqual(history.samples.count, 6)
        XCTAssertEqual(history.gpuSeries.count, 8)
        XCTAssertTrue(history.gpuSeries.allSatisfy { $0.samples.count == 6 })
    }

    func testFixtureSymlinkIntoProjectStateIsRejected() throws {
        let fileManager = FileManager.default
        let projectRoot = fileManager.temporaryDirectory
            .appendingPathComponent("serverpilot-fixture-test-\(UUID().uuidString)", isDirectory: true)
        let fixtureRoot = projectRoot.appendingPathComponent("desktop/Fixtures", isDirectory: true)
        let stateRoot = projectRoot.appendingPathComponent("state", isDirectory: true)
        defer { try? fileManager.removeItem(at: projectRoot) }

        try fileManager.createDirectory(at: fixtureRoot, withIntermediateDirectories: true)
        try fileManager.createDirectory(at: stateRoot, withIntermediateDirectories: true)
        let stateFixture = stateRoot.appendingPathComponent("live.json")
        try Data(contentsOf: Self.fixturesRoot.appendingPathComponent("1.json")).write(to: stateFixture)
        let fixtureSymlink = fixtureRoot.appendingPathComponent("linked.json")
        try fileManager.createSymbolicLink(at: fixtureSymlink, withDestinationURL: stateFixture)

        XCTAssertThrowsError(
            try FixtureSnapshots.resolve("linked.json", fixturesRoot: fixtureRoot, projectRoot: projectRoot)
        ) { error in
            guard
                let fixtureError = error as? FixtureSnapshotError,
                case .rejectedProductionState(let rejectedURL) = fixtureError
            else {
                return XCTFail("Expected rejectedProductionState, got \(error)")
            }
            XCTAssertEqual(rejectedURL, stateFixture.resolvingSymlinksInPath())
        }
    }

    private static var fixturesRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures", isDirectory: true)
    }

    private static func snapshot(named name: String) throws -> BrokerSnapshot {
        let url = try FixtureSnapshots.resolve(name, fixturesRoot: fixturesRoot)
        return try FixtureSnapshots.load(from: url)
    }

    private func waitUntil(
        timeoutNanoseconds: UInt64 = 1_000_000_000,
        _ predicate: @escaping @MainActor () -> Bool
    ) async throws {
        let started = DispatchTime.now().uptimeNanoseconds
        while !predicate() {
            if DispatchTime.now().uptimeNanoseconds - started > timeoutNanoseconds {
                XCTFail("Timed out waiting for condition")
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }

    private func waitUntilAsync(
        timeoutNanoseconds: UInt64 = 1_000_000_000,
        _ predicate: @escaping () async -> Bool
    ) async throws {
        let started = DispatchTime.now().uptimeNanoseconds
        while !(await predicate()) {
            if DispatchTime.now().uptimeNanoseconds - started > timeoutNanoseconds {
                XCTFail("Timed out waiting for asynchronous condition")
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }
}

@MainActor
private final class CompletionRecorder {
    var success: Bool?
    var message: String?
}

private actor DelayedSequenceClient: BrokerSnapshotClient {
    struct Metrics: Sendable {
        let callCount: Int
        let maxConcurrentCalls: Int
        let activeCalls: Int
    }

    private let snapshots: [BrokerSnapshot]
    private let delaysNanoseconds: [UInt64]
    private var nextIndex = 0
    private var activeCalls = 0
    private var callCount = 0
    private var maxConcurrentCalls = 0

    init(snapshots: [BrokerSnapshot], delayNanoseconds: UInt64) {
        self.snapshots = snapshots
        self.delaysNanoseconds = Array(repeating: delayNanoseconds, count: snapshots.count)
    }

    init(snapshots: [BrokerSnapshot], delaysNanoseconds: [UInt64]) {
        self.snapshots = snapshots
        self.delaysNanoseconds = delaysNanoseconds
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        callCount += 1
        activeCalls += 1
        maxConcurrentCalls = max(maxConcurrentCalls, activeCalls)
        let index = min(nextIndex, snapshots.count - 1)
        nextIndex += 1
        let delay = delaysNanoseconds[min(index, delaysNanoseconds.count - 1)]
        do {
            try await Task.sleep(nanoseconds: delay)
        } catch {
            activeCalls -= 1
            throw error
        }
        activeCalls -= 1
        return snapshots[index]
    }

    func metrics() -> Metrics {
        Metrics(callCount: callCount, maxConcurrentCalls: maxConcurrentCalls, activeCalls: activeCalls)
    }
}

private actor CancellationIgnoringClient: BrokerSnapshotClient {
    private let snapshotValue: BrokerSnapshot
    private let delayNanoseconds: UInt64
    private var callCount = 0

    init(snapshot: BrokerSnapshot, delayNanoseconds: UInt64) {
        self.snapshotValue = snapshot
        self.delayNanoseconds = delayNanoseconds
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        callCount += 1
        do {
            try await Task.sleep(nanoseconds: delayNanoseconds)
        } catch {
            try? await Task.sleep(nanoseconds: delayNanoseconds)
        }
        return snapshotValue
    }

    func metrics() -> Int { callCount }
}

private actor ScriptedClient: BrokerSnapshotClient {
    private var results: [Result<BrokerSnapshot, BrokerRefreshError>]

    init(results: [Result<BrokerSnapshot, BrokerRefreshError>]) {
        self.results = results
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        let result = results.isEmpty ? .failure(BrokerRefreshError.invalidSnapshot) : results.removeFirst()
        return try result.get()
    }
}

private actor DelayedEndpointHistoryClient: BrokerEndpointTelemetryHistoryClient {
    private var results: [Result<EndpointTelemetryHistory, BrokerRefreshError>]
    private let delaysNanoseconds: [UInt64]
    private var nextIndex = 0

    init(results: [Result<EndpointTelemetryHistory, BrokerRefreshError>], delaysNanoseconds: [UInt64]) {
        self.results = results
        self.delaysNanoseconds = delaysNanoseconds
    }

    func history(endpointID: String, range: EndpointTelemetryRange, actorID: String) async throws -> EndpointTelemetryHistory {
        let index = nextIndex
        nextIndex += 1
        let delay = delaysNanoseconds[min(index, delaysNanoseconds.count - 1)]
        let result = results.isEmpty
            ? Result<EndpointTelemetryHistory, BrokerRefreshError>.failure(BrokerRefreshError.invalidSnapshot)
            : results[min(index, results.count - 1)]
        do {
            try await Task.sleep(nanoseconds: delay)
        } catch {
            try? await Task.sleep(nanoseconds: delay)
        }
        return try result.get()
    }
}

private actor CountingEndpointHistoryClient: BrokerEndpointTelemetryHistoryClient {
    private var calls = 0

    func history(endpointID: String, range: EndpointTelemetryRange, actorID: String) async throws -> EndpointTelemetryHistory {
        calls += 1
        return .empty(endpointID: endpointID, range: range)
    }

    func callCount() -> Int { calls }
}

private actor CooperativeDelayedEndpointHistoryClient: BrokerEndpointTelemetryHistoryClient {
    struct Metrics: Sendable {
        let callCount: Int
        let cancellationCount: Int
    }

    private let delaysNanoseconds: [UInt64]
    private var nextIndex = 0
    private var calls = 0
    private var cancellations = 0

    init(delaysNanoseconds: [UInt64]) {
        self.delaysNanoseconds = delaysNanoseconds
    }

    func history(endpointID: String, range: EndpointTelemetryRange, actorID: String) async throws -> EndpointTelemetryHistory {
        let index = nextIndex
        nextIndex += 1
        calls += 1
        let delay = delaysNanoseconds[min(index, delaysNanoseconds.count - 1)]
        do {
            try await Task.sleep(nanoseconds: delay)
        } catch {
            cancellations += 1
            throw error
        }
        return .empty(endpointID: endpointID, range: range)
    }

    func metrics() -> Metrics {
        Metrics(callCount: calls, cancellationCount: cancellations)
    }
}

private final class StateRouteURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var responseData: Data?
    nonisolated(unsafe) static var lastRequest: URLRequest?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.lastRequest = request
        let data = Self.responseData ?? Data("{}".utf8)
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    static func reset() {
        responseData = nil
        lastRequest = nil
    }
}
