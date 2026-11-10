# -*- coding: utf-8 -*-
"""TX/RX 日志。

单独成模块的原因：CLI 和管家（daemon）都要写同一份日志，
如果留在 cli.py 里，daemon 就得 import cli，容易绕成循环依赖。
"""

import os
import time

# 单个日志文件的上限（1.20-a）。到这儿就轮转，不留机会让它长到几 MB。
MAX_BYTES = 5 * 1024 * 1024
# 历史份数：log.1 / log.2 / log.3 → 目录里最多 4 个文件。
KEEP = 3


class Logger(object):
    """按场景写 TX/RX 日志。行格式与 v1 兼容，老的日志文件继续可读。

    1.20-a：这个文件以前只追加、从不轮转。定时跑一夜就是几 MB，没人清就一直长。
    现在的规则是"**轮转不删除**"：
        log.txt → log.1.txt → log.2.txt → log.3.txt（最老的才删）
    为什么不直接清空：日志是用来追因的，清空等于把证据删了；
    用户排查"三天前那条命令"时，历史文件必须还在。
    """

    def __init__(self, log_root, scene, max_bytes=MAX_BYTES, keep=KEEP):
        self.dir = os.path.join(log_root, scene)
        self.path = os.path.join(self.dir, "log.txt")
        # 允许注入一个很小的上限（自检用），生产路径用默认值
        self.max_bytes = int(max_bytes or MAX_BYTES)
        self.keep = max(1, int(keep or KEEP))

    def _ensure(self):
        os.makedirs(self.dir, exist_ok=True)

    def rotated_path(self, index):
        """第 N 份历史日志（1 = 刚轮转出去的那份）。"""
        return os.path.join(self.dir, "log.%d.txt" % int(index))

    def rotate_if_needed(self):
        """超过上限就轮转。返回 True 表示真的转了。

        **轮转失败绝不能影响收发**：串口那边正忙着，日志这里宁可继续往
        大文件里写，也不要抛异常把一次发送搞失败。

        轮转发生在**写之前**，所以文件可能最多超出一个上限"一次写入的量"
        （一批 TX/RX 行）。这不影响判断：它不会累积到几 MB 去。
        """
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return False
        except OSError:
            return False          # 文件还不存在 / 读不到大小：不用转
        try:
            oldest = self.rotated_path(self.keep)
            if os.path.exists(oldest):
                os.remove(oldest)  # 超过保留份数才真的删，只删最老那份
            for index in range(self.keep - 1, 0, -1):
                src = self.rotated_path(index)
                if os.path.exists(src):
                    os.replace(src, self.rotated_path(index + 1))
            os.replace(self.path, self.rotated_path(1))
            return True
        except OSError:
            return False

    def append(self, direction, text, port=""):
        """写一行日志。

        1.82：TX 行要带上**这次用的是哪个口**（`@COM7`）。以前日志只有
        `[时间] TX 命令`，于是"载入历史"只能拿场景的当前口去补 ——
        `--com COM10` 发的命令在历史里全变成 COM7，等于编数据。
        `@` 前缀是为了不与命令本身混淆（`AT+UART=115200,0,0` 这种没有空格，
        但用户也可能发带空格的文本）。
        """
        self._ensure()
        self.rotate_if_needed()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        tag = (" @" + str(port)) if (port and str(direction).upper() == "TX") else ""
        with open(self.path, "a", encoding="utf-8") as handle:
            for line in str(text).splitlines() or [""]:
                handle.write("[%s] %s%s %s\n" % (stamp, direction, tag, line))

    def tail(self, lines):
        """只读**当前** log.txt。

        不要把轮转出去的历史文件也读进来：`serial_read(lines=50)` 会变成
        几百行，用户在对话里看到一大坨，还以为工具出错了。
        """
        try:
            with open(self.path, encoding="utf-8", errors="replace") as handle:
                data = handle.readlines()
        except OSError:
            data = []
        return "".join(data[-max(1, int(lines)):]).rstrip("\n")
