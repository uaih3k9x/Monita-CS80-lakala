// Front Panel 的实时数据：Agent 会话数、CPU、防睡眠断言 → 按钮状态（纯函数）
import Foundation

// MARK: 防睡眠（IOPMCopyAssertionsByProcess 的断言字典 → Lock 按钮）

public struct SleepGuard: Equatable {
    public var awake: Bool
    public var holder: String?   // 显示在按钮上的进程名
    public var others: Int       // 其余挡睡眠的进程数（去重后）
    public init(awake: Bool, holder: String?, others: Int) { self.awake = awake; self.holder = holder; self.others = others }
}

/// 只算「挡系统睡眠」的断言；只挡显示器睡眠的（PreventUserIdleDisplaySleep 等）不算。
let systemSleepTypes: Set<String> = ["PreventUserIdleSystemSleep", "PreventSystemSleep", "NoIdleSleepAssertion"]
/// 系统常驻持有者：powerd 在屏幕亮着时总挂着一条，熄屏就放掉，算进去就永远是 Awake
let ignoredHolders: Set<String> = ["powerd"]
/// 系统守护进程排在 App 后面（coreaudiod 是放歌时替播放器挂的）
let daemonHolders: Set<String> = ["coreaudiod", "WindowServer", "bluetoothd", "sharingd", "backupd"]

public func sleepGuard(_ assertions: [[String: Any]]) -> SleepGuard {
    var names: [String] = []
    for a in assertions {
        guard let type = a["AssertType"] as? String, systemSleepTypes.contains(type) else { continue }
        if let level = (a["AssertLevel"] as? NSNumber)?.intValue ?? (a["AssertLevel"] as? Int), level == 0 { continue }
        let name = (a["Process Name"] as? String) ?? "?"
        if ignoredHolders.contains(name) || names.contains(name) { continue }
        names.append(name)
    }
    guard !names.isEmpty else { return SleepGuard(awake: false, holder: nil, others: 0) }
    func rank(_ n: String) -> Int { n == "caffeinate" ? 0 : daemonHolders.contains(n) ? 2 : 1 }
    let best = names.enumerated().min { (rank($0.element), $0.offset) < (rank($1.element), $1.offset) }!.element
    return SleepGuard(awake: true, holder: best, others: names.count - 1)
}

// MARK: Agent 会话数（旧链路 host 的 /api/data/agents + /api/data/agent；以后换成本地扫会话目录，见 PLAN.md）

public struct AgentCounts: Equatable {
    public var claude: Int   // Claude Code 运行中（state == running）
    public var waiting: Int  // 所有 agent 等待输入（state == waiting），外加 state.json 的 waiting
    public var dsh: Int      // DeepSeek 类 agent 活着的会话（running/waiting）
    public init(claude: Int, waiting: Int, dsh: Int) { self.claude = claude; self.waiting = waiting; self.dsh = dsh }

    public static func parse(_ agents: [UInt8], agentState: [UInt8]?) -> AgentCounts? {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(agents)) as? [String: Any],
              let items = obj["items"] as? [[String: Any]] else { return nil }
        var c = AgentCounts(claude: 0, waiting: 0, dsh: 0)
        for it in items {
            let agent = it["agent"] as? String ?? "", state = it["state"] as? String ?? ""
            if agent == "claude" && state == "running" { c.claude += 1 }
            if state == "waiting" { c.waiting += 1 }
            if agent == "deepseek" && (state == "running" || state == "waiting") { c.dsh += 1 }
        }
        if let s = agentState, let o = try? JSONSerialization.jsonObject(with: Data(s)) as? [String: Any],
           o["state"] as? String == "waiting" { c.waiting += 1 }
        return c
    }
}

/// 状态翻转限速：屏是单缓冲，频繁反白会撕裂 → 两次切换至少隔 minInterval 毫秒
public struct Debounced: Equatable {
    public private(set) var value: Bool
    var changedAt: Double?
    let minInterval: Double
    public init(_ v: Bool, minInterval: Double) { value = v; self.minInterval = minInterval }

    public mutating func update(_ v: Bool, nowMs: Double) -> Bool {
        if v != value && (changedAt == nil || nowMs - changedAt! >= minInterval) {
            value = v
            changedAt = nowMs
        }
        return value
    }
}

/// CPU 占用（0…1）：两次 HOST_CPU_LOAD_INFO 采样（user, system, idle, nice）的差分
public func cpuFraction(prev: [UInt32], cur: [UInt32]) -> Double? {
    guard prev.count == 4, cur.count == 4 else { return nil }
    let d = zip(prev, cur).map { Double($1 &- $0) }
    let total = d.reduce(0, +)
    guard total > 0 else { return nil }
    return max(0, min(1, 1 - d[2] / total))
}
