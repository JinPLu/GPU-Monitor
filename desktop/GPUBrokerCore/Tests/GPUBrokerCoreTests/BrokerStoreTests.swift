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
