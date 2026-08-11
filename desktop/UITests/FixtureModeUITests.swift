import XCTest

@MainActor
final class FixtureModeUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = "1"
        app.launchEnvironment["SERVERPILOT_CLI"] = "/usr/bin/false"
        app.launchArguments += ["-ApplePersistenceIgnoreState", "YES"]
        app.launch()
    }

    override func tearDownWithError() throws {
        app.terminate()
        app = nil
    }

    func testFixtureModeLaunchesWithoutProductionDaemon() {
        let resources = app.descendants(matching: .any)["服务器"]
        XCTAssertTrue(resources.waitForExistence(timeout: 5), "Fixture launch must reach the native resource surface")

        let refreshButton = app.buttons["更新资源数据"]
        XCTAssertTrue(refreshButton.exists, "The native refresh control must be exposed to accessibility")

        let fixtureNotice = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "正在使用桌面测试夹具")
        ).firstMatch
        XCTAssertTrue(fixtureNotice.waitForExistence(timeout: 2), "The app must identify deterministic fixture mode")

        let startupFailure = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "无法启动 ServerPilot")
        ).firstMatch
        XCTAssertFalse(
            startupFailure.exists,
            "Fixture mode must return before daemon startup; SERVERPILOT_CLI deliberately points to /usr/bin/false"
        )
    }

    func testResourceTableExposesGPUModelForScheduling() {
        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label == %@", "服务器 ssh -p 2221 gpu@127.0.0.1")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Fixture server must appear in the resource table")
        XCTAssertTrue(
            String(describing: serverRow.value).contains("GPU 配置 Fixture GPU"),
            "The resource row must expose the GPU model to visual and accessibility clients"
        )
        XCTAssertTrue(
            String(describing: serverRow.value).contains("CPU 负载"),
            "The resource row must describe normalized host load accurately"
        )

        serverRow.click()
        let gpuModel = app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", "GPU 0 · Fixture GPU")
        ).firstMatch
        XCTAssertTrue(gpuModel.waitForExistence(timeout: 2), "Server detail must keep the GPU model visible")

        let backButton = app.buttons["resource-detail-back"]
        XCTAssertTrue(backButton.waitForExistence(timeout: 2), "Server detail must expose an obvious return action")
        backButton.click()
        XCTAssertTrue(serverRow.waitForExistence(timeout: 2), "Returning from server detail must restore the resource table")
        XCTAssertFalse(backButton.exists, "The server detail sheet must be dismissed after returning")
    }

    func testResourceTableExposesProjectAndCurrentTask() {
        app.terminate()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = "resource-ownership"
        app.launch()

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "ssh -p 2222 gpu@10.20.0.21")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Owned GPU server must appear in the resource table")
        let rowValue = String(describing: serverRow.value)
        XCTAssertTrue(rowValue.contains("vision-lab"), "The resource row must expose the active project")
        XCTAssertTrue(rowValue.contains("train-resnet"), "The resource row must expose the current task")
        XCTAssertFalse(rowValue.contains("agent-trainer"), "The resource row must not expose internal Agent identity")

        serverRow.click()
        assertAgentIdentityIsNotExposed()

        let gpu = app.buttons.matching(NSPredicate(format: "label == %@", "GPU 0")).firstMatch
        XCTAssertTrue(gpu.waitForExistence(timeout: 2), "Server detail must expose the assigned GPU")
        XCTAssertTrue(String(describing: gpu.value).contains("train-resnet"), "GPU accessibility must expose the current task")
        gpu.click()
        let close = app.buttons["关闭"]
        XCTAssertTrue(close.waitForExistence(timeout: 2), "GPU detail must open before checking its exposed content")
        XCTAssertTrue(app.staticTexts["train-resnet"].exists, "GPU detail must expose the current task")
        assertAgentIdentityIsNotExposed()
    }

    func testKeepaliveIsVisibleAsNonErrorOccupancyWithoutSystemIdentity() {
        app.terminate()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = "keepalive"
        app.launch()

        let serverRow = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@", "ssh -p 2223 gpu@10.20.0.31")
        ).firstMatch
        XCTAssertTrue(serverRow.waitForExistence(timeout: 5), "Keepalive fixture server must appear in the resource table")
        let rowValue = String(describing: serverRow.value)
        XCTAssertTrue(rowValue.contains("占卡"), "Keepalive must use the user-facing occupancy term")
        XCTAssertTrue(rowValue.contains("0/2 可用"), "Keepalive GPUs must not be presented as allocatable")
        XCTAssertFalse(rowValue.contains("__serverpilot_system__"), "Internal system identity must not appear in the resource table")

        serverRow.click()
        let status = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "占卡")).firstMatch
        XCTAssertTrue(status.waitForExistence(timeout: 2), "Server detail must expose the user-facing occupancy state")
        let action = app.buttons["endpoint-keepalive-action"]
        XCTAssertTrue(action.exists, "Configured keepalive must expose exactly one endpoint action")
        XCTAssertEqual(action.label, "关闭空闲占卡")
    }

    func testResourceTableHeadersToggleSortDirection() {
        let gpuSort = app.buttons["按GPU 利用排序"]
        XCTAssertTrue(gpuSort.waitForExistence(timeout: 5), "GPU utilization header must be directly sortable")
        XCTAssertEqual(String(describing: gpuSort.value), "未选中")

        gpuSort.click()
        XCTAssertEqual(String(describing: gpuSort.value), "降序")

        gpuSort.click()
        XCTAssertEqual(String(describing: gpuSort.value), "升序")
    }

    func testAcceptedPrimarySectionsExposeAccessibleContentWithoutAgentUI() {
        relaunch(fixture: "resource-ownership", section: "resource-usage")

        let usage = app.descendants(matching: .any)["使用情况"]
        XCTAssertTrue(usage.waitForExistence(timeout: 5), "Usage must expose its accepted accessible section name")
        XCTAssertTrue(
            app.descendants(matching: .any)["按项目或任务查看使用情况"].exists,
            "Usage must expose project/task switching to accessibility clients"
        )
        assertAgentIdentityIsNotExposed()

        relaunch(fixture: "resource-ownership", section: "settings")

        let settings = app.descendants(matching: .any)["设置"]
        XCTAssertTrue(settings.waitForExistence(timeout: 5), "Settings must expose its accepted accessible section name")
        XCTAssertTrue(app.staticTexts["本机服务地址"].exists)
        XCTAssertTrue(app.staticTexts["数据更新间隔"].exists)
        XCTAssertTrue(app.staticTexts["版本"].exists)
        assertAgentIdentityIsNotExposed()
    }

    func testDeterministicEmptyAndConnectionErrorFixturesExposeTextMeaning() {
        relaunch(fixture: "0", section: "server-pool")

        let emptyState = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS %@", "暂无端点")
        ).firstMatch
        XCTAssertTrue(emptyState.waitForExistence(timeout: 5), "An empty fleet must expose a textual empty state")

        relaunch(fixture: "error", section: "server-pool")

        let failedServer = app.descendants(matching: .any).matching(
            NSPredicate(format: "value CONTAINS %@", "连接失败")
        ).firstMatch
        XCTAssertTrue(
            failedServer.waitForExistence(timeout: 5),
            "Connection failure must be conveyed through accessible text, not color alone"
        )
    }

    func testFrozenViewportsKeepAcceptedNavigationReachable() {
        for viewport in ["1024x640", "1280x800", "1440x820"] {
            relaunch(fixture: "resource-ownership", section: "server-pool", viewport: viewport)

            XCTAssertTrue(app.descendants(matching: .any)["服务器"].waitForExistence(timeout: 5))
            XCTAssertTrue(app.buttons["使用情况"].exists)
            XCTAssertTrue(app.buttons["设置"].exists)
            XCTAssertTrue(app.buttons["更新资源数据"].exists)
        }
    }

    private func relaunch(fixture: String, section: String, viewport: String? = nil) {
        app.terminate()
        app.launchEnvironment["SERVERPILOT_DESKTOP_FIXTURE"] = fixture
        app.launchEnvironment["SERVERPILOT_DESKTOP_SECTION"] = section
        if let viewport {
            app.launchEnvironment["SERVERPILOT_DESKTOP_VIEWPORT"] = viewport
        } else {
            app.launchEnvironment.removeValue(forKey: "SERVERPILOT_DESKTOP_VIEWPORT")
        }
        app.launch()
    }

    private func assertAgentIdentityIsNotExposed() {
        let disclosedIdentity = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", "agent-trainer", "agent-trainer")
        ).firstMatch
        XCTAssertFalse(disclosedIdentity.exists, "Visible, help, and accessibility content must not expose internal Agent identity")
    }
}
