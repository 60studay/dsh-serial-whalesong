"""端口与运行模式（reconnect / autodetect / probe / mode / pause）

轻模式不碰串口是硬约束，所以探测必须显式（probe_port）。

这里用的是 mixin：方法体拿的还是 self，所以 self.state / self.loop /
self.ports 和拆之前完全一样，行为零变化。
"""

import os
import threading
import time

from ..channel import run_evidence
from ..config import load_scene
from ..errors import BridgeError
from ..ports import guess_port, resolve_port, scan_ports
from ..rules import CHECK_SOURCES, RULE_TYPES, merge_watch, normalize_check_source


class PortMixin(object):

    def _apply_override(self, com, baud):
        """把手动选的串口写进内存 **和状态文件**（1.18）。

        只写 `state/<场景>.json`，**绝不写 config.json**：
        用户的原话是"这只是我这次选的，凭什么给我写进配置"。
        config.json 放匹配规则（vid_pid / match_serial / match），state 放"这一次选谁"。
        """
        self.override_com = str(com or "")
        try:
            self.override_baud = int(baud or 0)
        except (TypeError, ValueError):
            self.override_baud = 0
        self.state.update(override_com=self.override_com,
                          override_baud=self.override_baud)


    def reconnect(self, com, baud):
        # 选的口号**现在不存在**就不记（用户 22:5x："选不出来的问题不该锁死，要问人"）。
        # 以前这里先写 override 再回头看能不能解析 —— 写进去就"记住"了，用户只看到
        # "串口未解析出来"，根本不知道自己被锁在哪个口上。
        if com:
            try:
                available = [item["device"] for item in scan_ports()]
            except Exception:
                available = []
            if available and com not in available:
                return {"ok": False,
                        "error": {"kind": "port_missing",
                                  "message": "现在找不到 %s。" % com,
                                  "hint": ["当前插着：%s" % "、".join(sorted(available)),
                                           "从下拉框里挑一个（或点「AI 自动选择」让程序按规则找）"]},
                        "candidates": sorted(available)}
        if com or baud:
            self._apply_override(com or self.override_com,
                                 baud or self.override_baud)
        # 真重建：watch 模式先停旧口再按新参数开
        if self.mode == "watch":
            resolved = self.current_device()
            self.loop.watch_mode = True
            self.loop.start(resolved["device"], int(self.effective_cfg().get("baud") or 115200))
        else:
            # 轻模式**不占口**，所以这里绝不能真去 open 一下做"验证"。
            # 两个理由（都是实测踩出来的）：
            #   1) 打开会撞上真正持口的那个进程（管家/定时发送线程/用户的串口助手），
            #      于是报"COM10 被其他程序占用"——但轻模式本来就不该占口，这是假警报；
            #   2) CH340 在打开瞬间会拉高 DTR/RTS，等于给板子来了一次复位。
            # 轻模式只需要确认"这个口现在存在"，扫描就够。
            resolved = self.current_device()
            self.state.update(port=resolved["device"],
                              baud=int(self.effective_cfg().get("baud") or 0))
        # 切口那一刻就对齐控制权（对面 23:35 的绕过口子）
        self._align_control_on_port()
        return {"ok": True,
                "port": self.current_device()["device"],
                "baud": self.effective_cfg().get("baud"),
                # 页面上据此显示"已锁定 COMx（重启后仍在）"
                "override_com": self.override_com,
                "override_baud": self.override_baud}


    def _align_control_on_port(self):
        """切到一个口之后，按**该口上最严格的控制权**对齐（paused > human > ai）。

        不做轮询：只在"切过去"这一下对齐，其它场景靠 `_sync_same_port` 跟随。
        """
        try:
            device = self.current_device()["device"]
        except BridgeError:
            return
        try:
            strict = self.state.strictest_state_for_port(device)
            if not strict:
                return
            current = self.state.read().get("state")
            if current != strict:
                self.state.force_state(strict, "port-align",
                                       "切口对齐：%s 上最严格的控制权是 %s" % (device, strict))
        except Exception:
            pass                      # 对齐失败不影响"切口"本身

    def auto_port(self):
        """「回到自动识别」：丢掉手动选的口，回到 config 的匹配规则（+ 猜）。

        只清 state 里的 override，**不动 config.json** —— 和 `_apply_override` 对称。
        清掉之后如果配置也找不到口（拔了/换了口），**仍然算成功**：
        用户要的是"别再用我刚选的那个"，不是"必须马上找到一个"。
        所以返回 `resolved: null` + 错误详情，由页面提示下一步该点哪个按钮。
        """
        previous = self.override_com
        self._apply_override("", 0)
        resolved, error = None, None
        try:
            resolved = self.current_device()
        except BridgeError as exc:
            error = exc.to_dict()
        if resolved:
            if self.mode == "watch":
                try:
                    self.loop.start(resolved["device"],
                                    int(self.effective_cfg().get("baud") or 115200))
                except BridgeError as exc:
                    error = exc.to_dict()
            else:
                # 轻模式不占口：只记下"现在解析到哪个口"，好让页头显示出来
                self.state.update(port=resolved["device"],
                                  baud=int(self.effective_cfg().get("baud") or 0))
        result = {
            "ok": True,
            "cleared": previous,
            "port": (resolved or {}).get("device") or "",
            "source": (resolved or {}).get("source") or "",
        }
        if error:
            result["resolved"] = None
            result["resolve_error"] = error
            result["note"] = ("已回到自动识别，但按场景配置暂时找不到串口。"
                              "插上设备后点「AI 自动选择」重新找一次。")
        return result


    def autodetect(self):
        """AI 自动选择：先按配置解析，配不上时**真的去猜**。

        以前它只做"解析"：配置写死 COM7、设备被 Windows 换成 COM10 时直接抛错，
        把正确的候选列在错误信息里却不去选。按钮叫「AI 自动选择」，
        用户期待的是"你帮我找找"——用户实测踩过（拔插 USB 换了口）。
        """
        cfg = self.scene.cfg
        reasons = {
            "match_serial": "按序列号 %r 精确匹配" % cfg.get("match_serial"),
            "match_vid_pid": "按 VID:PID %s 精确匹配" % cfg.get("match_vid_pid"),
            "match": "按描述关键字 %r 匹配" % cfg.get("match"),
            "com": "配置里指定的串口号可用",
            "only_port": "当前只有一个串口设备",
            "only_usb_port": "当前只有一个真实 USB 串口（已排除蓝牙虚拟口）",
        }
        try:
            resolved = resolve_port(cfg)
        except BridgeError as exc:
            # 只有"配置解析不出来"才去猜；端口被占用之类不能靠猜解决
            if exc.kind not in ("no_device", "ambiguous_device", "config"):
                raise
            resolved = guess_port(cfg)
            if resolved is None:
                raise exc
            story = {
                "match_serial": "按序列号",
                "match_vid_pid": "按 VID:PID",
                "match": "按描述关键字",
                "only_usb_port": "按「只有一块真实 USB 串口」",
            }.get(resolved["source"], "按规则")
            resolved["note"] = ("配置里的串口已经不可用，%s 重新找到了它；"
                                "建议把 config.json 改成 match_vid_pid 或 match_serial，"
                                "以后换 USB 口就不用管了" % story)
        result = {
            "ok": True,
            "port": resolved["device"],
            "description": resolved["description"],
            "vid_pid": resolved["vid_pid"],
            "reason": reasons.get(resolved["source"], resolved["source"]),
            "note": resolved["note"],
        }
        # 序列号一并回传：两块同型号板子（都是 CH340）时，这是唯一能分清的依据
        for port in scan_ports():
            if port["device"].lower() == resolved["device"].lower():
                result["serial_number"] = port.get("serial_number") or ""
                break
        return result


    def probe_port(self):
        """显式探测一次"这个口有没有被别的程序占着"。

        只有用户主动点「检测」才走这里：探测要真的 open 一次串口，
        而 CH340 打开瞬间会拉 DTR/RTS —— 对接着复位电路的板子等于一次硬复位。
        """
        # 1.78：和 /status 用**同一份解析结果、同样的新鲜度**。
        # 以前这里走 owner(probe=True) 的 15 秒缓存，而 /status 走 fresh=True，
        # 于是两个接口会给出相反结论（一个说"连着 COM7"，一个说"没连"）。
        # 1.78：**和 /status 走同一份解析**（`self.current_device()` 且带 override /
        # preferred 的同一套规则）。以前这里走 owner(probe=True) 的缓存，两边会打架。
        device, source, note = "", "", ""
        try:
            resolved = self.current_device()
            device = resolved["device"]
            source = resolved.get("source") or ""
            note = resolved.get("note") or ""
        except BridgeError as exc:
            # 解析不出来也要回答"这个口现在能不能用"，但把原因说清楚，
            # 不要再编一句"网页未连接串口"（那是旧文案，会误导）。
            device = str(self.override_com or "")
            note = exc.message
        value = self.ports.owner(probe=True, fresh=True)
        value = dict(value)
        value["port"] = device
        value["port_source"] = source
        if note and not value.get("detail"):
            value["detail"] = note
        value["status_port"] = device        # 供页面/自检对照：两边必须一致
        return dict({"ok": True}, **value)


    def set_mode(self, mode):
        if mode not in ("light", "watch"):
            raise BridgeError("config", "模式只能是 light 或 watch")
        if mode == self.mode:
            return {"ok": True, "mode": self.mode}

        # 管家在跑：监视模式交给它，网页仍然不拿口（否则两边抢口）
        daemon_url = self._daemon_url()
        if daemon_url:
            from . import daemon as daemon_mod
            result = daemon_mod.request(daemon_url, "/mode", {"mode": mode}, timeout=2.0)
            if result is None:
                self._daemon_cache = (0, None)      # 管家没了：退回本地逻辑
            elif not result.get("ok"):
                err = result.get("error") or {}
                raise BridgeError(err.get("kind") or "internal",
                                  err.get("message") or "管家拒绝切换模式",
                                  hint=err.get("hint"))
            else:
                self.mode = mode
                self.loop.stop()                    # 网页不再持口
                self.ports.hold_released_reason = "由管家持有（监视模式）"
                return {"ok": True, "mode": self.mode, "watch_running": True,
                        "via": "daemon"}

        if mode == "watch":
            # 切监视模式也遵守控制权：控制权不在 AI 就先不抢口
            self.mode = "watch"
            if self.ports.should_hold():
                self.ports.acquire_if_needed()
            else:
                self.ports.hold_released_reason = "控制权不在 AI，监视模式暂不占口"
        else:
            self.loop.stop()
            self.mode = "light"
            self.ports.hold_released_reason = ""
        return {"ok": True, "mode": self.mode, "watch_running": self.loop.running}


    # ── 暂停（网页留着） vs 退出（网页也关）───────────────────────────────
    def pause_port(self):
        """暂停：释放串口，但网页继续可访问。想临时用串口助手时点这个。"""
        if self.loop.running:
            self.loop.stop()
        self.ports.hold_released_reason = "已手动暂停，串口已释放"
        return {"ok": True, "paused": True, "watch_running": self.loop.running,
                "port_owner": self.ports.owner()}


    def resume_port(self):
        """从"暂停"恢复占用（只有控制权在 AI 且是 watch 模式才真的占口）。"""
        self.ports.hold_released_reason = ""
        acquired = False
        if self.ports.should_hold():
            acquired = self.ports.acquire_if_needed()
        return {"ok": True, "paused": False, "acquired": acquired,
                "watch_running": self.loop.running, "port_owner": self.ports.owner()}


    def request_shutdown(self):
        self.shutdown_event.set()
        threading.Thread(target=self._shutdown_worker, name="shutdown", daemon=True).start()
