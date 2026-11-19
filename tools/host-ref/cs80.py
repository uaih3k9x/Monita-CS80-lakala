#!/usr/bin/env python3
"""Monita-CS80 参考上位机 (协议 v0, docs/protocol.md)。需要 pyserial; 推 PNG 需要 Pillow (可选)。

  python cs80.py PORT hello                 握手, 打印屏参数
  python cs80.py PORT ping                  打印统计
  python cs80.py PORT pattern               推一张测试图案 (整帧) 并回读 CRC 校验
  python cs80.py PORT png FILE              推一张 PNG (缩放到 800x1280)
  python cs80.py PORT bench [N] [PNG...]    整帧吞吐: 连推 N 帧 (默认 10) 交替两张图 (给了 PNG 就轮流推这些)
  python cs80.py PORT diffdemo [秒]         tile 差分: 一个小方块在屏上移动, 打印每帧 rect 数和耗时
  python cs80.py PORT seg TEXT              段码屏主区显示数字串, 例如 123456 / --:-- 不支持冒号
  python cs80.py PORT backlight 0|1
  python cs80.py PORT sleep              # fw v10: 关背光 / 段码屏 / 灯, 下一条命令 (任何一帧) 叫醒
  python cs80.py PORT teartest              撕裂 A/B 对比 (要人看/拍慢动作), 各阶段会打印提示
  python cs80.py PORT scrollbench [N]       歌词区 COPY 滚动测速 (同 Mac --bench 的 COPY 段): 两种翻页模式各 N 帧 + FBCRC
  python cs80.py PORT tri [N]               多缓冲状态 (fw v6); 给 N (1..3, 0 = 自动) 则先切块数
  python cs80.py PORT e0                    零拷贝实验 E0: 0x24 是不是扫描行号 (只读, 不动画面)
  python cs80.py PORT e1 [秒]               零拷贝实验 E1: 帧中途改扫描基址是否立即生效 (要人看/拍照)
  python cs80.py PORT zc line|ruler [tint]|split FRAMES T1 T2 TOP|arm 0|1|peek OFF|poke OFF VAL [HOLD_MS]
                                            DIAG 20..25 单条命令 (fw v3; 数字可写 0x..)
  --no-lz4 (放在任意位置)                   不发 RECT_LZ4 (fw v11), 和默认的压缩推帧做 A/B 对比; 没装 python lz4 时本来就不发
PORT: Windows 上是 "Quectel USB Modem" 对应的 COMx, macOS 上是 /dev/cu.usbmodem*。
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 嵌入式 Python 不自动加脚本目录
import monita_link as ml  # noqa: E402


def pattern(kind=0):
    """整帧 RGB565: kind 0 = 色条 + 渐变 + 网格, 1 = 反相"""
    out = bytearray(ml.W * ml.H * 2)
    bars = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (0, 0, 0)]
    for y in range(ml.H):
        row = bytearray()
        for x in range(ml.W):
            if y < 320:
                r, g, b = bars[x * 8 // ml.W]
            elif y < 640:
                v = x * 255 // (ml.W - 1); r, g, b = v, v, v
            else:
                r, g, b = (255, 255, 255) if (x % 64 == 0 or y % 64 == 0) else (20, 24, 40)
            if kind:
                r, g, b = 255 - r, 255 - g, 255 - b
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            row += struct.pack("<H", v)
        out[y * ml.W * 2:(y + 1) * ml.W * 2] = row
    return bytes(out)


def load_png(path):
    from PIL import Image
    im = Image.open(path).convert("RGB").resize((ml.W, ml.H))
    return ml.rgb888_to_rgb565(im.tobytes())


def push_full(link, fb, fid=0):
    t0 = time.perf_counter()
    a = link.push(fb, ml.full_rects(max_payload=link.info["max_payload"]), fid)
    return time.perf_counter() - t0, a


def verify(link, fb):
    want = ml.crc32(fb)
    got = link.fbcrc(0, 0, ml.W, ml.H)
    print("回读 CRC: 设备 %08x  主机 %08x  %s" % (got or 0, want, "一致" if got == want else "不一致!"))
    return got == want


def teartest(link):
    """1/2: 整帧交替推送, 单缓冲 vs 双缓冲; 3/4: 两块缓冲直接切扫描基址, 不同步 vs 等帧事件。"""
    fbs = [pattern(0), pattern(1)]
    def stage(title, secs):
        print("\n== %s (%.0f 秒)" % (title, secs)); sys.stdout.flush(); time.sleep(1)
    st, w = link.diag(ml.DG_BUFS)
    if st != ml.OK:
        print("设备没有第二块 fb, 只能看单缓冲"); return
    stage("阶段 1: 单缓冲, 整帧交替 ×6 —— 预期能看到自上而下的刷屏", 6)
    link.diag(ml.DG_DBUF, b"\x00")
    for i in range(6): push_full(link, fbs[i & 1], i); time.sleep(0.5)
    stage("阶段 2: 双缓冲, 整帧交替 ×6 —— 预期整屏一下子换掉", 6)
    link.diag(ml.DG_DBUF, b"\x01")
    for i in range(6): push_full(link, fbs[i & 1], i); time.sleep(0.5)
    # 3/4: 深蓝 / 灰 两块, 140 ms 切一次 (不是帧周期 26.6 ms 的整数倍)
    link.diag(ml.DG_FILL, struct.pack("<BHH", 0, 0x0010, 0x0010))
    link.diag(ml.DG_FILL, struct.pack("<BHH", 1, 0x8410, 0x8410))
    for mode, title in ((0, "阶段 3: 不同步切基址 —— 若基址不在帧头锁存, 慢动作里会看到上下两色的分界"),
                        (2, "阶段 4: 等帧事件后切基址 —— 预期每一帧都是整屏单色")):
        stage(title, 10)
        st, w = link.diag(ml.DG_FLIP, struct.pack("<HIB", 72, 140000, mode), timeout=30)
        print("   翻页 72 次, 总 %.1f s, 单次 write_screen 最长 %d us" % (w[0] / 1e6, w[1]))
    print("\n== 结束, 推回测试图案"); link.diag(ml.DG_DBUF, b"\x01"); push_full(link, fbs[0])


REGION = (43, 337, 750, 514)       # 歌词区, 同 Bench.swift


def scrollbench(link, n=60):
    """每帧: 歌词区上移 8 行 (COPY) + 底部 8 行新条带 (RECT) + PRESENT。主机镜像同样操作, 每组结束 FBCRC 整屏比对。"""
    if not link.info["caps"] & ml.CAP_COPY:
        print("固件 caps=%#x 不支持 COPY (要 fw v2)" % link.info["caps"]); return False
    x, y, w, h = REGION
    fb = bytearray(pattern(0))
    push_full(link, bytes(fb), 200)
    ok = True
    for flip in (0, 1):
        link.diag(ml.DG_FLIPMODE, bytes([flip]))
        times = []
        for i in range(n):
            c = ml.scroll_copy(x, y, w, h, -8)
            ml.apply_copy(fb, *c)
            col = struct.pack("<H", 0xF800 if i % 2 == 0 else 0x001F) * w
            for r in range(y + h - 8, y + h):
                o = (r * ml.W + x) * 2
                fb[o:o + w * 2] = col
            t0 = time.perf_counter()
            link.push(bytes(fb), [(x, y + h - 8, w, 8)], 300 + i, copies=[c])
            times.append(time.perf_counter() - t0)
        per = sum(times) / n * 1000
        good = link.fbcrc(0, 0, ml.W, ml.H) == ml.crc32(bytes(fb))
        ok &= good
        print("翻页=%s: %.1f ms/帧 (%.1f fps)  最慢 %.1f ms  FBCRC %s" % (
            "立即" if flip else "等帧", per, 1000 / per,
            max(times) * 1000, "一致" if good else "不一致!"))
    link.diag(ml.DG_FLIPMODE, b"\x01")
    print("全部一致" if ok else "有 FBCRC 不一致")
    return ok


def need_v3(link):
    if link.info["fw"] < 3:
        print("固件是 v%d, 零拷贝实验要 fw v3" % link.info["fw"]); sys.exit(1)


def zc_cmd(link, args):
    need_v3(link)
    num = lambda x: int(x, 0)
    sub = args[0]
    if sub == "line":
        st, w = link.diag(ml.DG_ZC_LINE, bytes([15])); print(st, ml.zc_line_decode(w))
    elif sub == "ruler":
        st, w = link.diag(ml.DG_ZC_RULER, struct.pack("<H", num(args[1]) if len(args) > 1 else 0x07E0), timeout=15)
        print(st, w)
    elif sub == "split":
        f, t1, t2, top = map(num, args[1:5])
        st, w = link.diag(ml.DG_ZC_SPLIT, ml.zc_split_args(f, t1, t2, top), timeout=f * 0.03 + 10)
        print("status %d  帧 %d  t1 最晚迟到 %d us  t2 最晚迟到 %d us  超时帧 %d  基址 %#x" % ((st,) + tuple(w + [0] * 5)[:5]))
    elif sub == "arm":
        print(link.diag(ml.DG_ZC_ARM, bytes([num(args[1])])))
    elif sub == "peek":
        print(link.diag(ml.DG_ZC_PEEK, struct.pack("<I", num(args[1]))))
    elif sub == "poke":
        hold = num(args[3]) if len(args) > 3 else 0
        print(link.diag(ml.DG_ZC_POKE, ml.zc_poke_args(num(args[1]), num(args[2]), hold), timeout=hold / 1000 + 10))
    else:
        print(__doc__); sys.exit(2)


def e0(link):
    """0x24 采样 3 轮: 值随时间涨 ~1/20.3us 就是扫描行号"""
    need_v3(link)
    for r in range(3):
        st, w = link.diag(ml.DG_ZC_LINE, bytes([15]))
        pts = ml.zc_line_decode(w)
        print("轮 %d status %d: %s" % (r, st, " ".join("%d:%#x" % p for p in pts)))
    vals = {v for _, v in pts}
    print("结论: " + ("0x24 在变 -> 可能是行号, 把上面的 (us:值) 发回分析" if len(vals) > 1 else "0x24 恒为 %#x -> 不是行号" % vals.pop()))


def e1(link, secs=6.0):
    """帧中途写扫描基址: 判定 立即生效(ABS/REL) / 帧头锁存。每阶段 secs 秒, 要人看屏。"""
    need_v3(link)
    frames = min(300, int(secs * 37.5))
    print("准备: 推一帧已知图案 (色条/灰阶/网格) 当前台, 在后台缓冲画尺子 (左边 11 块 = 行号二进制, 每 100 行一条红线)")
    push_full(link, pattern(0))
    st, w = link.diag(ml.DG_ZC_RULER, struct.pack("<H", 0x07E0), timeout=15)
    print("  ruler status %d 耗时 %s us 缓冲 %s" % (st, w[:1], [hex(x) for x in w[1:2]]))
    stages = [
        ("A 帧中 13.0 ms 切到尺子 top=0, 不切回", 13000, 0, 0,
         "预期(立即生效)分屏线约在屏幕第 %d 行。看: 整屏原图 / 整屏尺子 / 下半尺子? 下半尺子时, 分屏线下第一行尺子的行号是多少 (≈%d 还是 ≈0)?"
         % (ml.zc_expected_line(13000), ml.zc_expected_line(13000))),
        ("B 同 A, top=100", 13000, 0, 100, "和 A 比, 分屏线下的尺子行号是否整体 +100?"),
        ("C 中间带: 7.13 ms 切入, 17.6 ms 切回", ml.zc_time_for_line(337), ml.zc_time_for_line(851), 0,
         "是否出现稳定的中间尺子带 (约屏幕 337..851 行)? 上下边界抖不抖, 抖几行?"),
    ]
    for title, t1, t2, top, ask in stages:
        print("\n== %s  (%d 帧 ≈ %.0f 秒)\n   %s" % (title, frames, frames / 37.5, ask)); sys.stdout.flush()
        time.sleep(1.5)
        st, w = link.diag(ml.DG_ZC_SPLIT, ml.zc_split_args(frames, t1, t2, top), timeout=frames * 0.03 + 10)
        w = w + [0] * 5
        print("   status %d  帧 %d  t1 最晚迟到 %d us  t2 最晚迟到 %d us  超时帧 %d  基址 %#x" % (st, w[0], w[1], w[2], w[3], w[4]))
    print("\n== 结束, 推回测试图案"); push_full(link, pattern(0)); verify(link, pattern(0))


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    argv = [a for a in sys.argv if a != "--no-lz4"]
    link = ml.Link(argv[1], lz4=len(argv) == len(sys.argv))
    cmd, args = argv[2], argv[3:]
    info = link.hello()
    if cmd == "hello":
        print(info)
    elif cmd == "ping":
        print(link.ping())
    elif cmd in ("pattern", "png"):
        fb = load_png(args[0]) if cmd == "png" else pattern(int(args[0]) if args else 0)
        dt, a = push_full(link, fb)
        print("整帧 %.1f ms, %.2f MB/s, 线上 %d B (RECT_LZ4 %d/%d 条), ACK %s"
              % (dt * 1000, len(fb) / dt / 1e6, link.last_push["bytes"], link.last_push["lz4"], link.last_push["rects"], a))
        verify(link, fb)
    elif cmd == "bench":
        n = int(args[0]) if args else 10
        fbs = [load_png(p) for p in args[1:]] or [pattern(0), pattern(1)]
        times, wire, nlz = [], 0, 0
        for i in range(n):
            dt, a = push_full(link, fbs[i % len(fbs)], i)
            times.append(dt)
            wire += link.last_push["bytes"] + 10
            nlz += link.last_push["lz4"]
        print("整帧 x%d: 平均 %.1f ms  最快 %.1f ms  最慢 %.1f ms  线上平均 %d B/帧  %.2f MB/s  RECT_LZ4 %d 条 (%s)"
              % (n, sum(times) / n * 1000, min(times) * 1000, max(times) * 1000, wire // n, wire / sum(times) / 1e6, nlz,
                 "已启用" if link.use_lz4() else "未启用: --no-lz4 / 固件没有 caps bit8 / 没装 python lz4"))
        print("PRESENT clean 耗时 %d us, 统计 %s" % (a["extra"], link.ping()))
        verify(link, fbs[(n - 1) % len(fbs)])
    elif cmd == "diffdemo":
        secs = float(args[0]) if args else 5
        bg = bytearray(pattern(0))
        prev = bytes(bg)
        link.push(prev, ml.full_rects())
        t_end = time.monotonic() + secs
        k = 0; stats = []
        while time.monotonic() < t_end:
            cur = bytearray(bg)
            bx, by = (k * 7) % (ml.W - 100), (k * 11) % (ml.H - 100)
            blk = struct.pack("<H", 0xFFE0) * 100
            for r in range(100):
                o = ((by + r) * ml.W + bx) * 2
                cur[o:o + 200] = blk
            cur = bytes(cur)
            rects = ml.dirty_rects(prev, cur)
            t0 = time.perf_counter()
            link.push(cur, rects, k)
            stats.append((len(rects), time.perf_counter() - t0))
            prev = cur; k += 1
        print("%d 帧, 平均 %.1f 个 rect, 推送平均 %.1f ms (不含主机差分)" %
              (k, sum(s[0] for s in stats) / k, sum(s[1] for s in stats) / k * 1000))
        verify(link, prev)
    elif cmd == "teartest":
        teartest(link)
    elif cmd == "scrollbench":
        scrollbench(link, int(args[0]) if args else 60)
    elif cmd == "tri":
        if args:
            print("块数 ->", link.diag(ml.DG_NBUF, bytes([int(args[0])])))
        st, w = link.diag(ml.DG_TRI)
        print(dict(zip(ml.TRI_FIELDS, w)) if st == ml.OK else "固件不支持 (要 fw v6)")
    elif cmd == "e0":
        e0(link)
    elif cmd == "e1":
        e1(link, float(args[0]) if args else 6.0)
    elif cmd == "zc":
        zc_cmd(link, args)
    elif cmd == "seg":
        print(link.seg(ml.seg_number(args[0])))
    elif cmd == "backlight":
        print(link.backlight(args[0] != "0"))
    elif cmd == "sleep":
        print(link.sleep())
    else:
        print(__doc__); sys.exit(2)
    link.close()


if __name__ == "__main__":
    main()
