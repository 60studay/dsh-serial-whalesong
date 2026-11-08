# -*- coding: utf-8 -*-
"""按「物理端口」的跨进程互斥锁。

## 为什么需要它

两个 AI（或网页 + 管家 + CLI）同时用同一个串口时，**不是"慢一点"，是串包**：

    A 发 ?  → 拿到 'ERR CMD (H for help)'   ← 那是 B 的 P 的回复
    B 发 P  → 回包是空的

`io.py` 原来的 `_open_with_retry` 只是"打不开就重试 3 次"，
解决不了"另一个进程已经打开、正在读"的情况。
`watch_loop.port_lock` 是**进程内**锁，跨进程无效。

## 锁挂在物理端口上，不是场景上

    蓝牙AT 场景   → match CH340 → COM7
    Modbus 场景   → match CH340 → COM7     ← 两个场景抢同一把锁才叫互斥

所以 key 是**解析后的设备名**（COM7 / /dev/ttyUSB0），不是场景名。

## 三个必须处理的坑

1. **跨平台**：用"原子创建文件"（O_CREAT|O_EXCL），Windows/POSIX 行为一致，
   不依赖 msvcrt/fcntl。
2. **陈旧锁自愈**：进程被 kill 时锁文件会留下。
   靠**时间**判死（默认 30 秒），**不用 `os.kill(pid, 0)`**——Windows 上那是"杀进程"。
3. **释放可靠**：一律走上下文管理器，`finally` 里删锁。
"""

import json
import os
import socket
import time

from .errors import BridgeError

# 等多久放弃（命令最长也就几秒）
LOCK_TIMEOUT_S = 5.0
# 锁文件超过这么久一定认为是死的（崩溃残留）
LOCK_STALE_S = 30.0
# 轮询间隔
POLL_S = 0.05


def _default_runtime_root():
    """和 cli.default_log_root 保持同一套约定：<插件>/logs/.runtime。"""
    env = os.environ.get("SERIAL_BRIDGE_RUNTIME_DIR")
    if env:
        return env
    log_root = os.environ.get("SERIAL_BRIDGE_LOG_DIR")
    if not log_root:
        log_root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "logs")
    return os.path.join(log_root, ".runtime")


def _safe_name(text):
    out = []
    for ch in str(text or ""):
        out.append(ch if (ch.isalnum() or ch in "-_.") else "_")
    return "".join(out) or "unknown"


def lock_path(device, runtime_root=None):
    root = runtime_root or _default_runtime_root()
    return os.path.join(root, "locks", "%s.lock" % _safe_name(device))


def read_lock(device, runtime_root=None):
    """读锁文件内容（拿不到就当没有）。返回 dict 或 None。"""
    path = lock_path(device, runtime_root)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return None


def _lock_age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 0.0


def is_stale(path):
    """锁是不是死的。

    只看时间：进程被 kill、断电、蓝屏都会留下锁文件，
    而"pid 还在不在"在 Windows 上没法安全地问（os.kill 会真的杀进程）。
    30 秒足够长——最长的一次 transact 也就 2~3 秒。
    """
    return _lock_age(path) > LOCK_STALE_S


def _try_create(path, payload):
    """原子创建锁文件。成功返回 fd，已存在抛 FileExistsError。"""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except Exception:
        os.close(fd)
        raise
    return fd


def acquire(device, runtime_root=None, timeout_s=None, stale_s=None):
    """拿锁。成功返回 (path, fd)；拿不到抛 BridgeError(port_busy)。"""
    timeout_s = LOCK_TIMEOUT_S if timeout_s is None else float(timeout_s)
    stale_s = LOCK_STALE_S if stale_s is None else float(stale_s)
    path = lock_path(device, runtime_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    payload = {
        "device": device,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "ts": time.time(),
    }
    started = time.time()
    stole = False
    while True:
        try:
            return path, _try_create(path, payload)
        except FileExistsError:
            # 有锁：先看是不是死的
            if _lock_age(path) > stale_s:
                try:
                    os.remove(path)
                    stole = True
                except OSError:
                    pass
                continue
            if time.time() - started >= timeout_s:
                holder = read_lock(device, runtime_root) or {}
                who = holder.get("pid")
                hint = [
                    "这是两个 AI 同时在用同一个串口（或网页/管家正在用）",
                    "等几秒再试，或者让另一头先用完",
                ]
                if who:
                    hint.append("占用者：pid %s @ %s" % (who, holder.get("host") or "?"))
                hint.append("如果是崩溃残留，删掉 %s 再试" % path)
                raise BridgeError(
                    "port_busy",
                    "%s 正被另一个调用占用（已等 %.1f 秒）" % (device, timeout_s),
                    hint=hint,
                )
            time.sleep(POLL_S)


def release(path, fd, runtime_root=None):
    """释放：关 fd + 删文件。删不掉也不抛（崩溃自愈靠超时）。"""
    try:
        if fd is not None:
            os.close(fd)
    except OSError:
        pass
    try:
        os.remove(path)
    except OSError:
        pass


class port_lock(object):
    """上下文管理器：`with port_lock(cfg, device): ...`

    用法上强制"无论成功失败都释放"，不靠调用方记得。
    """

    def __init__(self, cfg, device, timeout_s=None):
        self.cfg = cfg or {}
        self.device = device
        self.timeout_s = timeout_s
        self.path = None
        self.fd = None

    def __enter__(self):
        root = (self.cfg.get("runtime_root")
                or os.environ.get("SERIAL_BRIDGE_RUNTIME_DIR")
                or None)
        self.path, self.fd = acquire(self.device, root, self.timeout_s)
        return self

    def __exit__(self, exc_type, exc, tb):
        release(self.path, self.fd)
        return False
