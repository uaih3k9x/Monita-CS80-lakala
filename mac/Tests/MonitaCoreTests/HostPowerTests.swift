// fw v10：SLEEP（0x32）/ caps bit7、上位机这边"多久没发就该重新 HELLO"、Mac 睡眠时停发、壁纸刷新不堵渲染线程
import Testing
import MonitaCore

@Test func sleepTypeAndCap() {
    #expect(LinkType.sleep == 0x32)
    let h = HelloInfo([0, 0, 10, 0, 0x20, 0x03, 0x00, 0x05, 1, 72, 0xFF, 0, 0x00, 0x80, 0, 0])!
    #expect(h.fw == 10 && h.hasSleep)
    #expect(!HelloInfo([0, 0, 9, 0, 0x20, 0x03, 0x00, 0x05, 1, 72, 0x7F, 0, 0x00, 0x80, 0, 0])!.hasSleep)
}

@Test func keepaliveRehelloAfterQuietGap() {
    // 固件 5 s 没帧就回开机画面；上位机隔了 ≥ 3.5 s 才发下一条时先重新 HELLO（之后推整帧），免得在开机画面上补小块
    var k = LinkKeepalive(staleAfterS: 3.5)
    #expect(!k.needsHello(nowS: 100))                    // 还没发过（连上时本来就 HELLO）
    k.sent(nowS: 100)
    #expect(!k.needsHello(nowS: 103.4))
    #expect(k.needsHello(nowS: 103.6))
    k.sent(nowS: 103.6)                                  // 重新 HELLO 也是一次发送
    #expect(!k.needsHello(nowS: 105))
    var off = LinkKeepalive(staleAfterS: nil)            // 对拍（--conformance）关掉
    off.sent(nowS: 0)
    #expect(!off.needsHello(nowS: 1000))
}

@Test func keepaliveSuspendHoldsTrafficAndResumeForcesHello() {
    // Mac 睡眠：发 SLEEP 后一个字节都不能再发（任何一帧都会叫醒设备）；醒来强制重新 HELLO + 整帧
    var k = LinkKeepalive(staleAfterS: 3.5)
    k.sent(nowS: 10)
    #expect(k.canSend)
    k.suspend()
    #expect(!k.canSend)
    k.resume()
    #expect(k.canSend)
    #expect(k.needsHello(nowS: 10.1))                    // 不管隔了多久
    k.sent(nowS: 10.1)
    #expect(!k.needsHello(nowS: 10.2))
}

@Test func coalescerNeverRunsWorkInlineAndSkipsWhileBusy() {
    // 锁屏几小时后解锁 31 s 才切回歌词：渲染线程在锁屏页每分钟同步问一次主线程拿壁纸（NSWorkspace），
    // 主线程那边卡住时渲染线程跟着卡。改成扔到后台、正在跑就不再排，渲染线程从不等它
    var jobs: [() -> Void] = []
    let c = Coalescer(schedule: { jobs.append($0) })
    var runs = 0
    #expect(c.request { runs += 1 })
    #expect(runs == 0 && jobs.count == 1)                // 只排队，不在调用方线程里跑
    #expect(!c.request { runs += 1 })                    // 还在跑：不再排
    #expect(jobs.count == 1)
    jobs.removeFirst()()
    #expect(runs == 1)
    #expect(c.request { runs += 1 })                     // 跑完了：可以再排
    #expect(jobs.count == 1)
}
