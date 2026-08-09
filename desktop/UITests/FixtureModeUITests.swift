import XCTest

@MainActor
final class FixtureModeUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment["GPU_BROKER_DESKTOP_FIXTURE"] = "1"
        app.launchEnvironment["GPU_BROKER_CLI"] = "/usr/bin/false"
        app.launchArguments += ["-ApplePersistenceIgnoreState", "YES"]
        app.launch()
    }

    override func tearDownWithError() throws {
        app.terminate()
        app = nil
    }

    func testFixtureModeLaunchesWithoutProductionDaemon() {
        let resources = app.descendants(matching: .any)["资源"]
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
            "Fixture mode must return before daemon startup; GPU_BROKER_CLI deliberately points to /usr/bin/false"
        )
    }
}
