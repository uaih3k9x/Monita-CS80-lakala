// fw v10：主机走了 / 休眠（docs/protocol.md「SLEEP」）在上位机这边的两条规矩，加一个不堵调用方的后台刷新。
//   * 固件 5 s 没有效帧就回开机画面：上位机隔了 staleAfterS（默认 3.5 s）才发下一条时先重新 HELLO 再推整帧，
//     不然会在开机画面上只补几块差分（Mac 进程被挂起、睡醒但 USB 没断时都会这样）。
//   * Mac 睡眠 / 关显示器时发 SLEEP，之后一个字节都不能再发（设备收到任何一帧就醒）；醒来强制重新 HELLO + 整帧。
import Foundation

public struct LinkKeepalive {
    public let staleAfterS: Double?
    private var lastSentS: Double?
    private var forced = false
    public private(set) var suspended = false

    /// staleAfterS = nil：不按间隔重新 HELLO（对拍脚本要自己控制每一帧）
    public init(staleAfterS: Double? = 3.5) { self.staleAfterS = staleAfterS }

    /// 发出去了一帧（包括 HELLO 本身）
    public mutating func sent(nowS: Double) { lastSentS = nowS; forced = false }

    /// 下一次发之前要不要先 HELLO
    public func needsHello(nowS: Double) -> Bool {
        if forced { return true }
        guard let staleAfterS, let lastSentS else { return false }
        return nowS - lastSentS >= staleAfterS
    }

    public var canSend: Bool { !suspended }
    /// 发完 SLEEP：停发
    public mutating func suspend() { suspended = true }
    /// 醒来：可以发了，第一条之前先 HELLO
    public mutating func resume() { suspended = false; forced = true }
}

/// 把工作扔给 schedule（通常是一个后台队列）跑；上一份还没跑完时新的请求直接丢掉。request 从不在调用方线程里跑工作。
public final class Coalescer {
    private let lock = NSLock()
    private var busy = false
    private let schedule: (@escaping () -> Void) -> Void

    public init(schedule: @escaping (@escaping () -> Void) -> Void) { self.schedule = schedule }

    /// 返回 true = 排上了
    @discardableResult
    public func request(_ work: @escaping () -> Void) -> Bool {
        lock.lock()
        if busy { lock.unlock(); return false }
        busy = true
        lock.unlock()
        schedule { [self] in
            work()
            lock.lock(); busy = false; lock.unlock()
        }
        return true
    }
}
