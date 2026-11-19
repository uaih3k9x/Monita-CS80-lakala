"""前面板按键 → 动作、状态灯（照 mac/Sources/MonitaCore/Controls.swift）。

+ / − 按下立即出动作；≡ / 查询 在抬起时判短按（中间出现过 LONG 就不算，固件对 PWRKEY 长按会补一个 UP）。
"""
import monita_link as ml

QUERY, MENU, PLUS, MINUS = 1, 2, 3, 4


class KeyMapper:
    def __init__(self):
        self.armed = set()          # 收到过 DOWN、还没 LONG 的键

    def feed(self, k):
        key, ev = k["key"], k["ev"]
        if ev == ml.KEY_DOWN:
            self.armed.add(key)
            return {PLUS: "vol+", MINUS: "vol-"}.get(key)
        if ev == ml.KEY_LONG:
            self.armed.discard(key)
            return None
        if ev == ml.KEY_UP and key in self.armed:
            self.armed.discard(key)
            return {MENU: "page", QUERY: "play_pause"}.get(key)
        return None


def led_for(playing):
    """(mask, mode, period_ms, on_ms)：播放中绿灯常亮，否则灭。Linux 没有 agent 数据源，蓝灯那一档不做"""
    return (ml.LED_G, ml.LED_ON, 0, 0) if playing else (0, ml.LED_OFF, 0, 0)
