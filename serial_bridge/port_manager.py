# -*- coding: utf-8 -*-
"""端口占用管理：谁该拿着串口、什么时候放开、有没有别人占着。

从 WebServer 里抽出来的，原因只有一个：这块逻辑和"网页"没关系。
控制权存在共享文件里，改它的路径有三条（网页按钮 / CLI 工具 / 人类手工），
所以释放与获取必须挂在"状态变了"上，而不是"谁点了按钮"——这个收敛逻辑
放在网页类里既难读，也容易被新功能绕过去。

对外只有 6 个动作，PortManager 不碰 HTTP、不碰界面。
"""

import time

from . import io as serial_io
from .errors import BridgeError
from .locking import port_lock
from .state import STATE_AI


class PortManager(object):
    def __init__(self, state, loop, current_device, effective_cfg,
                 mode_getter, shutdown_event=None, daemon_url=None):
        """
        state           共享状态（控制权）
        loop            持口的循环（WatchLoop），需要 .running / .start() / .stop()
        current_device  返回解析出的端口 dict，失败抛 BridgeError
        effective_cfg   返回当前生效配置
        mode_getter     返回 "light" / "watch"
        daemon_url      返回管家地址或 None（管家在跑时网页不许再拿口）
        """
        self.state = state
        self.loop = loop
        self.current_device = current_device
        self.effective_cfg = effective_cfg
        self.mode_getter = mode_getter
        self.shutdown_event = shutdown_event
        self.daemon_url = daemon_url or (lambda: None)

        self.hold_released_reason = ""
        self.last_reconcile = None
        self._last_control = None
        self._port_owner_cache = {}
        self._owner_since = 0.0        # 1.68：第一次发现“被别人占”的时刻
        self._owner_last = {}          # 1.69：上一次“被占”的结论（释放时要用它算时长）

    # ── 判断 ────────────────────────────────────────────────────────────────
    def should_hold(self, control=None):
        # 管家在跑时绝不由网页再拿一次口——两个持有者必然抢口
        if self.daemon_url():
            return False
        state = (control or self.state.read().get("state") or "ai")
        return self.mode_getter() == "watch" and state == STATE_AI

    # ── 释放 / 获取 ─────────────────────────────────────────────────────────
    def release_if_idle(self, reason=""):
        """按当前控制权决定是否放开串口。返回是否真的放开了。"""
        if self.loop.running and not self.should_hold():
            self.loop.stop()
            self.hold_released_reason = reason or "控制权不在 AI"
            return True
        return False

    def acquire_if_needed(self):
        """控制权回到 ai 时重新拿口（watch 模式才需要）。"""
        if self.mode_getter() != "watch" or self.loop.running:
            return False
        resolved = self.current_device()
        self.loop.start(resolved["device"],
                        int(self.effective_cfg().get("baud") or 115200))
        self.hold_released_reason = ""
        return True

    # ── 收敛 ────────────────────────────────────────────────────────────────
    def reconcile(self, trigger="change"):
        """让串口占用对齐当前控制权——**唯一的收敛点**。

        为什么需要它：控制权存在共享文件里，改它的路径有三条
          1. 网页按钮 POST /control
          2. serial_control 工具（CLI 子进程）
          3. 人类在别处跑 bridge.py control
        只有第 1 条会顺手调 release/acquire。如果只在按钮里做，
        CLI 把状态改成 human 时网页仍攥着口——这正是实测反馈的场景。

        所以释放/获取不挂在"谁改了状态"上，而是挂在"状态变了"这件事上：
        看门狗每秒比对一次，发现控制权变了就收敛一次。
        """
        try:
            current = self.state.read().get("state") or "ai"
        except Exception:
            return None
        previous = self._last_control
        if previous == current and trigger != "force":
            return None
        self._last_control = current

        action = "unchanged"
        try:
            if current == STATE_AI:
                if self.acquire_if_needed():
                    action = "acquired"
            else:
                label = "控制权切到 %s" % current
                if self.release_if_idle(label):
                    action = "released"
        except BridgeError as exc:
            action = "failed: %s" % exc.message
            self.loop.last_error = exc.message
        self.last_reconcile = {"action": action, "control": current, "trigger": trigger}
        return self.last_reconcile

    def watchdog(self):
        """每秒看一次控制权是否被别人改了；变了就收敛串口占用。"""
        while self.shutdown_event is not None and not self.shutdown_event.is_set():
            try:
                self.reconcile("watchdog")
            except Exception:
                pass
            self.shutdown_event.wait(1.0)

    # ── 谁占着 ──────────────────────────────────────────────────────────────
    def owner(self, probe=False, fresh=False):
        """谁占着这个口：ours / other / free / unknown。用于网页状态栏。

        **默认不探测**（probe=False）：
        判断"空闲还是被占"唯一的办法是真的 open 一次，而打开 CH340 会拉一下
        DTR/RTS —— 很多板子这两个脚接的是复位电路，等于**每 5 秒给板子一次硬复位**；
        而且探测那 50~200ms 会挡住串口助手，跨进程时还会和管家/CLI 自己撞。

        所以：只有本网页真的持着口才是 ours，其它一律 unknown；
        要真答案得显式调 probe=True（网页「检测占用」按钮 = POST /port/probe）。
        """
        now = time.time()
        device = ""
        try:
            device = self.current_device()["device"]
        except BridgeError:
            device = ""

        if self.loop.running and getattr(self.loop, "ser", None) is not None:
            return {"owner": "ours", "device": device,
                    "detail": "本网页的监视模式正持有 %s" % device}

        if not probe:
            if not device:
                return {"owner": "unknown", "device": "", "detail": "串口未解析出来"}
            return {"owner": "unknown", "device": device,
                    "detail": "轻模式不碰串口（探测会拉 DTR/RTS，可能复位板子）；"
                              "点「检测占用」可以主动试一次"}

        # 1.47：**刚有人用过这个口就别探测**。
        # 现象：用户说"明明没有 AI 在跑，我输入却报串口被占用"——是我们自己的探测
        # （每 5 秒真 open 一次）和他的发送撞在同一瞬间。
        # 状态文件的 mtime 就是"最近一次收发"的可靠代理（每次发送都会 update）。
        # 1.68：改用**真实的收发时刻**（loop.last_activity_at），不再看状态文件 mtime ——
        # 状态文件会被 /status 自己的结算写脏，导致探测永远被"刚有人用过"挡掉。
        try:
            last = float(getattr(self.loop, "last_activity_at", 0.0) or 0.0)
            age = (now - last) if last else 999.0
        except Exception:
            age = 999.0
        if age < 3.0:
            return {"owner": "unknown", "device": device,
                    "detail": "%.0f 秒前刚有人用过这个口，先不探测（避免抢口）" % age}

        cached = self._port_owner_cache.get("value")
        # 1.68：`fresh=True` 时不复用缓存 —— 页面轮询要能及时看到"刚刚被别人占了"，
        # 15 秒的缓存会让它一直是"可用"。
        if not fresh and cached is not None and (now - self._port_owner_cache.get("at", 0)) < 15.0:
            return cached
        if not device:
            value = {"owner": "unknown", "device": "", "detail": "串口未解析出来"}
            self._port_owner_cache = {"at": now, "value": value}
            return value
        # 1.93：**探测也必须拿跨进程锁**。
        # 以前这里只拿进程内的 loop.port_lock，和 CLI/会话/管家的发送**不互斥**：
        # 页面在跑时，探测正好在两次发送之间 open 一次口 → 线上多出几个垃圾字节 →
        # 板子把第一条命令当成乱码（实测第 1 条 `ERR CMD <0xE0>`，第 2 条才对）。
        # 拿不到锁就**跳过这一轮**，绝不阻塞发送。
        lock = port_lock(self.effective_cfg(), device, timeout_s=0.15)
        try:
            lock.__enter__()
        except BridgeError:
            value = {"owner": "unknown", "device": device,
                     "detail": "有一个正在进行的收发占着锁，本轮不探测"}
            self._port_owner_cache = {"at": now, "value": value}
            return value
        try:
            try:
                serial_io.open_and_check(self.effective_cfg())
                value = {"owner": "free", "device": device,
                         "detail": "%s 空闲，可用" % device}
            except BridgeError as exc:
                if exc.kind == "port_busy":
                    # 1.68：把"被占用"变成可读状态：从何时起、多久了、是不是有人绕过插件
                    now2 = time.time()
                    if not self._owner_since:
                        self._owner_since = now2
                    seconds = int(now2 - self._owner_since)
                    last = float(getattr(self.loop, "last_activity_at", 0.0) or 0.0)
                    self._owner_last = {
                        "device": device,
                        "bypass_suspect": bool(last < self._owner_since),
                    }
                    value = {
                        "owner": "other",
                        "device": device,
                        "detail": "被其他程序占用（串口助手 / 别的脚本 / 另一个终端工具）",
                        "since": int(self._owner_since),
                        "seconds": seconds,
                        "process_name": "未知（Windows 不提供占用进程名）",
                        "bypass_suspect": bool(last < self._owner_since),
                    }
                else:
                    value = {"owner": "unknown", "device": device, "detail": exc.message}
        finally:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass
        if value.get("owner") != "other":
            # 1.69：刚放开时把"占了多久 / 有没有人绕过插件"留在结果里
            if self._owner_since:
                value["released_after"] = int(time.time() - self._owner_since)
                value["released_bypass"] = bool(self._owner_last.get("bypass_suspect"))
                value["released_device"] = self._owner_last.get("device") or device
            self._owner_since = 0.0
        self._port_owner_cache = {"at": now, "value": value}
        return value
