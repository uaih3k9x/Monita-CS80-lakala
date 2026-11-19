"""packaging 示例的几条硬约束（R7 / R8）"""
import os
import re
import unittest

from util import LINUX

PK = os.path.join(LINUX, "packaging")


def read(name):
    with open(os.path.join(PK, name), encoding="utf-8") as f:
        return f.read()


def active(text):
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


class Packaging(unittest.TestCase):
    def test_service_default_target(self):
        s = "\n".join(active(read("monita-cs80.service")))
        self.assertIn("WantedBy=default.target", s)
        self.assertNotIn("graphical-session.target", s)
        self.assertRegex(s, r"ExecStart=\S*monita-cs80-linux --device usb")
        self.assertIn("Restart=on-failure", s)
        self.assertIn("RestartPreventExitStatus=2", s.splitlines())   # 缺字体等永久错误（exit 2）不要每 3 s 重启

    def test_udev(self):
        name = [n for n in os.listdir(PK) if n.endswith(".rules")][0]
        self.assertLess(int(re.match(r"(\d+)-", name).group(1)), 73)
        rules = active(read(name))
        self.assertTrue(any("ID_MM_DEVICE_IGNORE" in r and "2c7c" in r for r in rules))
        usb = [r for r in rules if 'SUBSYSTEM=="usb"' in r]
        self.assertTrue(usb and all('ENV{DEVTYPE}=="usb_device"' in r and "uaccess" in r for r in usb))
        self.assertTrue(any('SUBSYSTEM=="tty"' in r and "SYMLINK" in r for r in rules))

    def test_launcher_executable(self):
        self.assertTrue(os.access(os.path.join(LINUX, "monita-cs80-linux"), os.X_OK))


if __name__ == "__main__":
    unittest.main()
