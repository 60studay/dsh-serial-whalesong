# -*- coding: utf-8 -*-
"""自检：探测（占用检测）不能和发送抢串口（1.93）。

## 为什么会有这条

页面实例在跑时，`owner(probe=True)` 会**直接 open 一次串口**来查"有没有被占"。
它当时只拿了进程内的 `loop.port_lock`，和 CLI / 会话 / 管家的发送**不互斥** ——
于是探测正好插在两次发送之间 open，线上多出几个垃圾字节，板子把第一条命令
当成乱码（实测：第 1 条 `ERR CMD <0xE0>`，第 2 条才 `OK VER`）。

用户早先那句"发好几个 V 才回一下"，根子就是这个。

## 查什么

1. `port_manager.owner()` 的探测路径里**必须**出现 `port_lock`（跨进程锁）
2. 拿不到锁时**返回 unknown 并跳过**，不许阻塞、不许抛给调用方
3. 行为验证：先由别的持有者拿住锁 → 探测应立即返回"本轮不探测"，而不是等满 15 秒

第 3 条不需要真串口，用临时 runtime 目录 + 假设备名即可。
"""

import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge.errors import BridgeError            # noqa: E402
from serial_bridge.locking import acquire, release       # noqa: E402

PORT_MANAGER = os.path.join(HERE, "serial_bridge", "port_manager.py")


def check_source():
    """静态：探测路径里必须有跨进程锁。"""
    with io.open(PORT_MANAGER, encoding="utf-8") as handle:
        text = handle.read()
    problems = []
    if "port_lock(" not in text:
        problems.append("owner() 的探测没有用跨进程 port_lock —— 会和发送抢口")
    if "本轮不探测" not in text:
        problems.append("拿不到锁时没有「跳过本轮」的分支")
    return problems


def check_behaviour():
    """动态：锁被别人拿着时，探测要立刻跳过而不是干等。"""
    from serial_bridge.port_manager import PortManager  # 延迟导入，避免副作用

    root = tempfile.mkdtemp(prefix="probe-lock-")
    device = "COM_FAKE_93"
    path, fd = acquire(device, root, timeout_s=0.2)
    try:
        class FakeCfg(object):
            def __init__(self):
                self.loop = type("L", (), {"ser": None, "running": False,
                                           "last_activity_at": 0.0,
                                           "port_lock": __import__("threading").Lock()})()
                self._port_owner_cache = {}
                self._owner_since = 0.0
                self._owner_last = {}

            def effective_cfg(self):
                return {"runtime_root": root, "com": device}

            def current_device(self):
                return {"device": device}

            owner = PortManager.owner

        fake = FakeCfg()
        started = __import__("time").time()
        value = fake.owner(probe=True, fresh=True)
        spent = __import__("time").time() - started
        problems = []
        if value.get("owner") != "unknown":
            problems.append("锁被占时探测应返回 unknown，实际 %r" % value.get("owner"))
        if spent > 2.0:
            problems.append("探测等了 %.1f 秒（应当立刻跳过）" % spent)
        return problems
    finally:
        release(path, fd)


def main():
    print("=" * 70)
    print("自检：探测不与发送抢串口（1.93）")
    print("=" * 70)
    fails = 0

    problems = check_source()
    if problems:
        for item in problems:
            print("  FAIL %s" % item)
            fails += 1
    else:
        print("  PASS 探测路径用了跨进程锁，且有「拿不到就跳过」的分支")

    try:
        problems = check_behaviour()
    except Exception as exc:            # noqa: BLE001
        problems = ["动态检查跑不起来：%s" % exc]
    if problems:
        for item in problems:
            print("  FAIL %s" % item)
            fails += 1
    else:
        print("  PASS 锁被占时探测立刻返回 unknown（不阻塞发送）")

    print("=" * 70)
    print("PASS %d / FAIL %d" % (2 - fails, fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
