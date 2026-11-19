"""命令行入口：python3 -m monita_linux [--device usb|usb:N|/dev/ttyUSBn] [--player NAME] ...（DESIGN.md §5）"""
import argparse
import json
import logging
import signal
import sys
import time

from . import _paths  # noqa: F401

log = logging.getLogger("monita")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="monita-cs80-linux", description="Monita-CS80 副屏 Linux 上位机")
    ap.add_argument("--device", default="usb", help="usb | usb:N | 串口路径（默认 usb = 接口 7）")
    ap.add_argument("--player", help="优先的 MPRIS 播放器（总线名后缀前缀，如 spotify、chromium）")
    ap.add_argument("--page", choices=("lyrics", "clock"), default="lyrics", help="启动页")
    ap.add_argument("--tick", type=float, default=0.2, help="渲染节拍（秒）")
    ap.add_argument("--font-sans")
    ap.add_argument("--font-mono")
    ap.add_argument("--font-cjk")
    ap.add_argument("--png", help="离线出图到这个文件，不连设备")
    ap.add_argument("--sample", help="配 --png：用这份 JSON 样例（mac/Samples/*.json 的格式）")
    ap.add_argument("--at", type=float, help="配 --sample：进度（秒）")
    ap.add_argument("--clock", action="store_true", help="配 --png：画时钟页")
    ap.add_argument("--no-lz4", action="store_true", help="不发 RECT_LZ4（fw v11 的压缩推帧；没装 python3-lz4 时本来就不发）")
    ap.add_argument("--bench-device", nargs="?", const=8, type=int, metavar="N",
                    help="设备侧 LZ4 自测（DIAG 33，fw v11）：内嵌测试条带解码 / 落帧各 N 次（默认 8），打印后退出；屏上不动")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def fonts_from(args):
    from .render import Fonts
    return Fonts(sans=args.font_sans, mono=args.font_mono, cjk=args.font_cjk)


def write_png(args, now=None):
    from .render import Renderer
    now = time.time() if now is None else now
    data = None
    if args.sample:
        with open(args.sample, encoding="utf-8") as f:
            data = json.load(f)
        data["at"] = now * 1000
        if args.at is not None:
            data["pos"] = args.at
    img, _ = Renderer(fonts_from(args)).render("clock" if args.clock else "lyrics",
                                               dict(data=data, now=now, panel=dict(cpu=12, bat=87, load=0.42, player="spotify")))
    img.save(args.png)
    print("wrote %s" % args.png)


class NoSource:
    """会话总线 / jeepney 不可用时：永远"未在播放\""""
    estimated = False
    art_url = None

    def get(self):
        return None

    def play_pause(self):
        pass


def build(args):
    from .app import App
    from .render import Renderer
    from .session import Session
    from .sources.mpris import MprisLyrics, MprisSource
    from .system import stats, volume
    from .transport import open_device

    renderer = Renderer(fonts_from(args))
    try:
        from .system.dbus import JeepneyBus
        source = MprisSource(JeepneyBus("SESSION"), prefer=args.player)
    except Exception as e:                              # 没装 jeepney / 没有会话总线（linger、ssh）
        log.warning("MPRIS 不可用（%s）：只显示时钟 / 未在播放", e)
        source = NoSource()
    session = Session(lambda: open_device(args.device), lz4=not args.no_lz4)
    app = App(session, MprisLyrics(source), renderer, player=source, volume=volume.detect(), stats=stats.Stats("/"),
              page=args.page, tick=args.tick)
    try:
        from .system.dbus import JeepneyBus
        from .system.power import Logind
        lg = Logind(JeepneyBus("SYSTEM", enable_fds=True), listen_bus=lambda: JeepneyBus("SYSTEM"))
        if lg.take_inhibitor():
            app.power = lg
            lg.start(lambda going: app.post(("sleep", going)))
    except Exception as e:
        log.warning("logind 不可用（%s）：不处理休眠", e)
    return app


def bench_device(args, open_dev=None):
    """--bench-device：HELLO 之后发一条 DIAG 33，打印结果（格式同 cs80.py / Mac 的 --bench-device）。返回退出码"""
    import monita_link as ml
    from .transport import open_device
    port = (open_dev or open_device)(args.device)
    if port is None:
        print("找不到设备（--device %s）" % args.device, file=sys.stderr)
        return 1
    link = ml.Link(port)
    try:
        link.hello()
        _, d = link.bench_device(args.bench_device)
    finally:
        link.close()
    print(ml.format_lz4bench(d) if d else ml.LZ4BENCH_UNSUP)
    return 0 if d else 1


def main(argv=None):
    args = parse_args(argv)
    if args.bench_device is not None:
        return bench_device(args)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from .render.fonts import FontError
    try:
        if args.png:
            write_png(args)
            return 0
        app = build(args)
    except FontError as e:
        print(e, file=sys.stderr)
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: app.post(("quit",)))
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
