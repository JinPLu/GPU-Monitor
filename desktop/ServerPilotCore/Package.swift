// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ServerPilotCore",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "ServerPilotCore", targets: ["ServerPilotCore"])
    ],
    targets: [
        .target(name: "ServerPilotCore"),
        .testTarget(
            name: "ServerPilotCoreTests",
            dependencies: ["ServerPilotCore"]
        )
    ]
)
