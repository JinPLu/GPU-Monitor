import Foundation
import XCTest
@testable import GPUBrokerCore

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

    func testSuccessfulStaleSnapshotIsConnectedButFailsClosed() async throws {
        let client = ScriptedClient(results: [.success(try Self.snapshot(named: "stale"))])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)

        try await waitUntil { store.freshness == .stale && !store.isRefreshing }
        XCTAssertTrue(store.isConnected)
        XCTAssertFalse(store.allowsMutations)
        XCTAssertEqual(store.snapshot.dataAgeSeconds, 94)
        XCTAssertEqual(store.snapshot.freshnessSeconds, 30)
    }

    func testFixtureOlderThanFreshnessThresholdIsStaleAndReadOnly() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.useFixture(snapshot: try Self.snapshot(named: "stale"))

        XCTAssertEqual(store.freshness, .stale)
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

    func testEndpointRemovalRefreshDiscardsPreMutationResponseWithoutOverlapOrRollback() async throws {
        let beforeDeletion = try Self.snapshot(named: "1")
        let afterDeletion = BrokerSnapshot.empty
        let client = DelayedSequenceClient(
            snapshots: [beforeDeletion, afterDeletion],
            delaysNanoseconds: [80_000_000, 80_000_000]
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        let recorder = CompletionRecorder()

        store.connectForTesting(snapshotClient: client)
        try await waitUntilAsync { await client.metrics().activeCalls == 1 }
        let endpoint = try XCTUnwrap(beforeDeletion.endpoints.first)

        store.confirmEndpointRemovalAfterMutation(endpoint) { success, message in
            recorder.success = success
            recorder.message = message
        }

        try await waitUntilAsync { await client.metrics().callCount == 2 }
        XCTAssertEqual(store.snapshot, .empty, "The delayed pre-delete snapshot must not be committed")
        try await waitUntil { recorder.success != nil && !store.isRefreshing }

        let metrics = await client.metrics()
        XCTAssertEqual(metrics.callCount, 2)
        XCTAssertEqual(metrics.maxConcurrentCalls, 1)
        XCTAssertEqual(store.snapshot, afterDeletion)
        XCTAssertEqual(recorder.success, true)
        XCTAssertNil(recorder.message)
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
                        "id": "scheduler:hanhai22",
                        "provider_type": "scheduler",
                        "display_name": "Hanhai22",
                        "state": "PENDING",
                        "available": ["node_count": 2, "scheduler_units": 2]
                    ]
                ],
                "allocatable_units": [
                    [
                        "id": "scheduler-target:hanhai22",
                        "provider_id": "scheduler:hanhai22",
                        "unit_type": "scheduler-target",
                        "state": "PENDING",
                        "quantities": ["node_count": 2, "scheduler_units": 2]
                    ]
                ],
                "resource_claims": [
                    [
                        "id": "claim-1",
                        "actor_id": "agent-a",
                        "project_id": "project-a",
                        "task_ref": "train",
                        "state": "QUEUED",
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
        XCTAssertEqual(snapshot.resourceClaims.first?.quantities.compactLabel, "4 CPU · 8 GB RAM")
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

    func testFixtureSymlinkIntoProjectStateIsRejected() throws {
        let fileManager = FileManager.default
        let projectRoot = fileManager.temporaryDirectory
            .appendingPathComponent("gpu-broker-fixture-test-\(UUID().uuidString)", isDirectory: true)
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
