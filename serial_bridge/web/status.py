"""网页状态查询（status / poll / alerts）

网页每 700ms 轮询一次，所有只读展示都走这里。

这里用的是 mixin：方法体拿的还是 self，所以 self.state / self.loop /
self.ports 和拆之前完全一样，行为零变化。
"""

import os
import time

from ..channel import run_evidence
from ..config import load_scene
from ..errors import BridgeError
from ..ports import BAUD_CHOICES, guess_port, resolve_port, scan_ports
from ..rules import CHECK_SOURCES, RULE_TYPES, merge_watch, normalize_check_source
from ..transport import describe_transport as transport_describe
from .. import io as serial_io


class StatusMixin(object):

    # ── 状态 ────────────────────────────────────────────────────────────────
    def effective_watch(self):
        """网页看到、判定用的 watch 配置：**配置打底、状态覆盖，空值不覆盖**。

        以前 status() 只读状态文件，而状态文件里根本不存 rules
        （rules 在场景 config.json）→ 三方打架：
            config.json 5 条 / GET /rules 5 条 / GET /status 0 条
        用户点完"添加规则"看到列表还是空的，就会以为没保存、反复添加同一条。
        """
        try:
            self.scene.reload_if_changed()
        except Exception:
            pass
        return merge_watch(self.scene.watch_cfg(), self.state.read().get("watch") or {})


    def status(self):
        current = self.state.read()
        watch_cfg = self.effective_watch()
        autosend_alive = self._autosend_thread is not None and self._autosend_thread.is_alive()
        data = {
            "ok": True,
            "scene": self.scene.name,
            "mode": self.mode,
            "control": current.get("state"),
            "control_since": current.get("since"),
            "watch": watch_cfg,
            "bauds": BAUD_CHOICES,
            # watch_running 表示"后台自动循环是否在跑"：
            # 监视模式占口、或定时发送已开启，都算在跑（UI 用它显示状态）。
            "watch_running": self.loop.running or (autosend_alive and bool(watch_cfg.get("send_enabled"))),
            "port_held": self.loop.running,
            "sent_count": self.autosend_stats.get("sent", 0),
            # 定时发送的"准不准"要能被用户自己看见，而不是靠感觉：
            # skipped = 因为上一轮被收发拖住而跳过的次数（不补发），
            # avg_gap_ms = 最近 20 次的实际平均间隔（滚动）。
            "sent_skipped": self.autosend_stats.get("skipped", 0),
            "avg_gap_ms": self.autosend_stats.get("avg_gap_ms", 0),
            "autosend_error": self.autosend_stats.get("last_error", ""),
            "watch_error": self.loop.last_error,
            "alerts": self.state.read_alerts(5),
            "port": current.get("port") or "",
            "baud": current.get("baud") or int(self.effective_cfg().get("baud") or 0),
            # 1.18：页面上手动选的串口存这儿，重启后仍然生效；空串 = 走 config 的匹配规则
            "override_com": self.override_com,
            "override_baud": self.override_baud,
            "hold_released_reason": self.ports.hold_released_reason,
            "state_path": self.state.path,
            # 失效标签自救时探测哪些端口（1.27：原来写死在前端，属于"这台机器的环境知识"）
            "probe_ports": self.probe_ports(),
            # 解析不出串口时，把"现在插着哪些"和"需要人选一个"直接告诉前端 ——
            # 让页面**问人**，而不是悄悄锁在一个打不开的口上（用户 22:5x）。
            "port_choices": self.port_choices(),
            "needs_port_choice": None,          # 等解析结果出来再填（见下）
        }
        try:
            resolved = self.current_device()
            data["resolved"] = resolved
            data["port"] = resolved["device"]
            # 这个口是"我选的"还是"配置匹配出来的"——页面上要能看出来（1.18）
            data["port_source"] = "override" if self.override_com else "auto"
            # 传输方式描述：网页侧边栏据此显示"串口 COM10 @115200"
            data["transport"] = transport_describe(
                self.effective_cfg(), resolved["device"], resolved.get("baud"))
        except BridgeError as exc:
            data["resolved"] = None
            data["resolve_error"] = exc.to_dict()
            data["transport"] = transport_describe(self.effective_cfg())
            # 手动选的口不见了（拔了/换了 USB 口）：页面要明说"你锁的那个不在了"，
            # 而不是笼统报"找不到设备"——否则用户会以为整个配置坏了。
            if self.override_com and exc.kind in ("no_device", "ambiguous_device"):
                data["override_stale"] = True
            # 解析不出来 + 插着不止一个 → 该问人（用**一次**扫描的结果，别重复扫）
            data["needs_port_choice"] = len(data["port_choices"]) > 1
        data["port_owner"] = self.ports.owner()
        return data


    def poll(self, force=False, since=None):
        """把后端攒下的数据交给网页。

        自动发送（定时发送 / 规则自动处置）的每一帧都会进 `auto_frames`，
        这里**不分 mode** 都取走——否则 light 模式下定时发送在页面上看不到回显
        （Bug 4：用户说"没看到"，不是"没发送"）。

        **1.17：带 `since` 时改为按游标读、不消费**（环形缓冲 200 条）。
        以前 `take_frames()` 是取走即清空，于是"页面在轮询"和"我在测试脚本里轮询"
        会互相抢帧——用户看不到 AI 发的命令、我这边拿到 0 帧，都是这个。
        不带 `since` 时仍走老行为（取走），保证外部脚本不受影响。

        管家在跑时改从管家取（游标式增量），网页就完全不用碰串口了。
        """
        url = self._daemon_url()
        if url:
            from . import daemon as daemon_mod
            # 第一次接上管家时用 -1：只收从现在起的新数据，不倒历史
            cursor = getattr(self, "_daemon_cursor", -1)
            cursor = -1 if cursor is None else int(cursor)
            proxied = daemon_mod.request(url, "/poll?cursor=%d" % cursor, timeout=2.0)
            if proxied and proxied.get("ok"):
                self._daemon_cursor = int(proxied.get("cursor") or cursor)
                if proxied.get("bytes"):
                    return {
                        "ok": True,
                        "frames": [],
                        "rx_text": proxied.get("rx_text") or "",
                        "rx_hex": proxied.get("rx_hex") or "",
                        "bytes": int(proxied.get("bytes") or 0),
                        "via": "daemon",
                    }
                return {"ok": True, "frames": [], "rx_text": "", "rx_hex": "",
                        "bytes": 0, "via": "daemon"}
            self._daemon_cache = (0, None)      # 管家掉线：回退到本地逻辑

        if since is not None:
            # 游标模式：不消费，谁轮询都不影响别人
            frames = self.loop.read_frames(since)
            # 「读一次」也走游标模式（页面统一带 since），所以这里要额外保护：
            # 手动读一帧**不能**顺手把监视模式 buffer 里的字节取走，
            # 否则另一个标签下一次 /poll 就少了这几个字节（比抢帧更难查）。
            data = b"" if force else self.loop.take()
            cursor = max([float(f.get("ts") or 0) for f in frames] or [float(since or 0)])
            result = {
                "ok": True,
                "frames": frames,
                "cursor": cursor,
                "rx_text": data.decode("utf-8", errors="replace"),
                "rx_hex": " ".join("%02X" % b for b in data),
                "bytes": len(data),
            }
            return self._finish_poll(result, force)

        frames = self.loop.take_frames()
        data = self.loop.take()
        if frames or data:
            return {
                "ok": True,
                "frames": frames,
                "rx_text": data.decode("utf-8", errors="replace"),
                "rx_hex": " ".join("%02X" % b for b in data),
                "bytes": len(data),
            }
        # 轻模式**默认完全不碰串口**。
        # 早先这里每 700ms 临时开口读一次（每次最多等 1500ms 首字节），
        # 结果网页开着的时候串口几乎一直被占着——外部程序和 AI 都打不开（Bug 5）。
        # 现在只有用户手动点「读一次」(force) 才真正开口。
        result = {"ok": True, "rx_text": "", "rx_hex": "", "bytes": 0}
        if not force:
            result["skipped"] = "轻模式不主动读串口（点「读一次」可临时读一帧）"
        return self._finish_poll(result, force)


    def _finish_poll(self, result, force):
        """手动「读一次」的收尾：真开口 drain 一帧，**只往结果里加，不取走别人的帧**。

        1.17 的尾巴：以前 force 分支排在 `take_frames()` 之后，点一下「读一次」
        就顺手把 auto_frames 取走了，另一个标签/测试脚本再也看不到那几帧。
        现在帧永远按游标读，force 只负责"额外读一次串口"。
        """
        if not force:
            return result
        if getattr(self.loop, "running", False):
            # 定时/监视正握着口：再开一次只会报"被占用"（还是自己占的），说清楚更省事
            result["manual_read"] = False
            result["note"] = "端口正被定时发送/监视模式占着，读不到；先关掉定时再读。"
            return result
        try:
            resolved = self.current_device()
            with self.loop.port_lock:
                out = serial_io.drain(self.effective_cfg(), resolved["device"])
        except BridgeError as exc:
            return {"ok": False, "error": exc.to_dict()}
        if out["rx_text"]:
            self.logger.append("RX", out["rx_text"])
        result.update({
            "rx_text": out["rx_text"],
            "rx_hex": out["rx_hex"],
            "bytes": out["bytes"],
            "manual_read": True,
        })
        return result


    def alerts(self, limit):
        return self.state.read_alerts(limit)


    def clear_alerts(self):
        return {"ok": True, "cleared": self.state.clear_alerts()}


    # 默认探测表：8899 **跳过** —— 那是 DSH 自己的 Web GUI 端口。
    # 探活用的是 no-cors，只判断"有没有回应"，探到 DSH 也算通；跳过去比留着死页更糟。
    DEFAULT_PROBE_PORTS = [8898, 8900, 8901, 8902, 8903, 8904, 8905, 8906, 8907]

    def probe_ports(self):
        """失效标签自救时探测的端口表：场景/公共配置里有就用它，否则用默认。"""
        raw = (self.scene.cfg.get("probe_ports")
               or (self.state.read().get("probe_ports")))
        ports = []
        for item in (raw or []):
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 65535:
                ports.append(value)
        return ports or list(self.DEFAULT_PROBE_PORTS)


    def port_choices(self):
        """现在插着的串口（给"请选一个"的提示用）。

        **带 3 秒缓存**：页面每 700ms 拉一次 /status，而扫口在 Windows 上要 100~300ms
        （枚举 + 逐个查描述）。不加缓存时 /status 从 ~10ms 涨到 ~300ms，
        页面会明显发顿（用户 23:5x 报的"出来 bug 了"就是这个体感）。
        """
        now = time.time()
        cached_at, cached = getattr(self, "_choices_cache", (0.0, []))
        if now - cached_at < 3.0:
            return cached
        try:
            choices = sorted(item["device"] for item in scan_ports())
        except Exception:
            choices = []
        self._choices_cache = (now, choices)
        return choices

    def needs_port_choice(self):
        """当前没有可用串口、但机器上插着不止一个 → 该问人，而不是替用户猜。

        注意：/status 里**不调这个**（它会再解析一次，等于又扫一遍口），
        直接用那次解析的结果算，见 status() 里的 needs_port_choice 字段。
        """
        try:
            self.current_device()
            return False
        except BridgeError:
            return len(self.port_choices()) > 1
