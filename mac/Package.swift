// swift-tools-version: 5.10
// Monita-CS80 Mac 端：CoreGraphics/CoreText 画 800×1280 → BGR565 → 32×16 tile 差分 → 推给 CS80
import PackageDescription

// 只装了 Command Line Tools（没有 Xcode）时 XCTest 不存在；swift-testing 的 Testing.framework
// 在 CLT 里，但 `swift test` 在 CLT 下会静默跑 0 个用例。所以测试做成可执行目标，
// 直接调 swift-testing 的入口：`swift run monita-cs80-tests`。
let cltFrameworks = "/Library/Developer/CommandLineTools/Library/Developer/Frameworks"

let package = Package(
    name: "MonitaCS80",
    platforms: [.macOS(.v13)],
    targets: [
        // 纯逻辑（无 CoreGraphics）：歌词进度/定位/折行、像素转换、tile diff、patch 编码、推帧决策
        .target(name: "MonitaCore"),
        .executableTarget(name: "monita-cs80", dependencies: ["MonitaCore"]),
        .executableTarget(
            name: "monita-cs80-tests", dependencies: ["MonitaCore"], path: "Tests/MonitaCoreTests",
            swiftSettings: [.unsafeFlags(["-F", cltFrameworks])],
            linkerSettings: [.unsafeFlags(["-F", cltFrameworks, "-Xlinker", "-rpath", "-Xlinker", cltFrameworks])]
        ),
    ]
)
