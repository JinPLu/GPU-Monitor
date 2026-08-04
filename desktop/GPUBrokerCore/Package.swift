// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "GPUBrokerCore",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "GPUBrokerCore", targets: ["GPUBrokerCore"])
    ],
    targets: [
        .target(name: "GPUBrokerCore"),
        .testTarget(
            name: "GPUBrokerCoreTests",
            dependencies: ["GPUBrokerCore"]
        )
    ]
)
