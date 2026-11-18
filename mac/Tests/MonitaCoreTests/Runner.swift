// swift-testing 入口（见 Package.swift 注释）：swift run monita-cs80-tests
import Testing

@main struct Runner {
    static func main() async { await Testing.__swiftPMEntryPoint() as Never }
}
