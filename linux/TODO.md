# Linux 上位机：还没做的

完整性检查时发现、但不适合顺手补的缺口。小缺口已经直接补了（见 DESIGN.md 末尾"实现记录"）。

## 需要维护者动手 / 拍板

- [ ] **真设备冒烟（pyusb 直连）。** 本机是 Mac，要先插上设备、临时停掉 Mac 上位机（LaunchAgent），再跑
      `linux/monita-cs80-linux --device usb -v`，看歌词页、按键（+/−、≡、查询键）、灯、段码屏、Ctrl-C 灭灯；
      跑完记得恢复 LaunchAgent。到目前为止 USB 这条路只对着假 pyusb 设备测过，协议和会话只对着 fwsim 测过。
- [ ] **Linux 实机验证**：清单在 README"待 Linux 实机验证"，没人在真机上跑过（MPRIS 播放器、logind 挂起唤醒、
      wpctl / pactl、udev 规则和 ModemManager 忽略、内核驱动 detach、sway / i3 下的服务自启）。
- [ ] **CI（可选）。** 公开仓库可以加一个 GitHub Actions 的 ubuntu 任务：装 python3-pil / python3-jeepney / dbus /
      fonts-liberation / fonts-noto-cjk 跑 `python3 -m unittest discover -s linux/tests`。这样 `test_dbus_live`、
      真字体渲染和 fwsim 对拍都能在真 Linux 上跑一遍，替代一部分实机验证。要不要开 CI 由维护者定。

## 小但要真 Linux 才好验证的

- [ ] 没有 `fc-match` 时字体按常见路径回落，`NotoSansCJK-*.ttc` 取的是第 0 个字体（日文字形），应该取简体中文那个
      （Noto 合集里一般是第 2 个）。要在真机上确认序号后再改 `render/fonts.py` 的 `CANDIDATES`。有 fc-match（几乎所有桌面）时不受影响。
- [ ] `__main__.build()` 的装配（没有 jeepney → `NoSource`；没有系统总线 → 不处理休眠）只做过手工冒烟（Mac 上两种情况的日志都对），
      没有自动测试。

## 明确不做（DESIGN §9），以后有人要再说

- 关屏（DPMS）联动、锁屏页、450 ms 换句滚动（`Session.push` 的 `copies` 参数已经留好）、沉浸模式。
- 第二个歌词来源（LRCLIB 的使用条款还没核实）；目前只有网易云非官方接口，随时可能失效。
- 自动探测 USB 接口号、多台设备、配置文件、pip / deb / AUR 打包。
- jeepney 0.6（Debian 11）下的休眠：logind inhibitor 要传 fd，0.6 不支持；只能升级 jeepney。
