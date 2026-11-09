"""发命令与控制权（send / control / set_watch）

控制权门禁和无回显判定都在这里，是整个插件最敏感的一段。

这里用的是 mixin：方法体拿的还是 self，所以 self.state / self.loop /
self.ports 和拆之前完全一样，行为零变化。
"""

import os
import time

from ..channel import run_evidence
from ..config import load_scene
from ..errors import BridgeError
from ..ports import guess_port, resolve_port, scan_ports
from ..rules import CHECK_SOURCES, RULE_TYPES, merge_watch, normalize_check_source
from ..state import STATE_AI, STATE_HUMAN, STATE_PAUSED
from .. import io as serial_io
from .util import parse_hex as _parse_hex


class SerialMixin(object):

    # ── 动作 ────────────────────────────────────────────────────────────────
    def _wait_echo(self, timeout_s, silence_s):
        """从读线程的缓冲里凑一条回显：先等首字节，再等到静默。

        为什么不用 `transact()`：那个会 open/close 一次，而此刻口正被自家握着。
        这里只读缓冲（`loop.take()`），不动句柄。
        """
        out = b""
        deadline = time.time() + timeout_s
        while time.time() < deadline and not out:
            out += self.loop.take()
            if not out:
                time.sleep(0.01)
        if not out:
            return b""
        quiet_at = time.time() + silence_s
        while time.time() < quiet_at:
            chunk = self.loop.take()
            if chunk:
                out += chunk
                quiet_at = time.time() + silence_s
            else:
                time.sleep(0.01)
        return out

    def send(self, text, mode="text", actor="human", source="web", from_page=False,
             com="", baud=0):
        """发一条命令。

        source 表示"谁发的"：web(页面按钮) / ai(插件 serial_send 转发过来)。
        AI 发起的会在 auto_frames 里记一帧，网页回显区就能看到（Bug 8）。

        管家在跑时**交给管家发**：它已经常驻持有连接，网页就不该再去开关口
        ——否则用户在网页上点一下，AI 的速度就从 0ms 掉回 1 秒（第 4 件核心目标）。
        """
        command = str(text or "").strip()
        if not command:
            raise BridgeError("config", "命令不能为空")
        if actor == "ai":
            allowed, current = self.state.check_ai_allowed()
            if not allowed:
                # 被拒绝也要**留痕**：以前页面上一片安静，用户盯着屏幕什么都不会出现
                # （"它在动，但被权限挡住了"和"它死了"看起来一模一样）。
                self.state.append_alert({
                    "rule": "控制权拦截",
                    "blame": "AI 尝试发送「%s」被拒（控制权在 %s）"
                             % (command[:40], current.get("state")),
                })
                # 收发区也要留一行：用户盯的是那块，只有状态轨道不够（对面 1.33 判据 3）
                self.loop._push_frame(command, "", "", 0,
                                      source if source != "web" else "external")
                raise BridgeError(
                    "control_blocked",
                    "控制权是 %s，AI 不能发命令。" % current.get("state"),
                    hint=["点网页上的[交给AI]（或 [继续AI]）",
                          "也可以让 AI 调 serial_control action=ai",
                          "人类发任意命令会自动把控制权拉回 human"],
                )
        else:
            # 安全规则 1：人类命令永远优先，AI 托管立即让位。
            # 1.44：这是"临时接管"（带倒计时），不是永久 —— 时长可在偏好里改。
            self.state.note_human_command(hold_s=self._human_hold_seconds())

        proxied = self._proxy_send_to_daemon(command, mode, actor, source)
        if proxied is not None:
            return proxied

        if mode == "hex":
            payload_bytes = _parse_hex(command)
            if payload_bytes is None:
                raise BridgeError("config", "十六进制格式不对，示例：AA 55 01 00")
        else:
            payload_bytes = command.encode("utf-8")

        # 口被**自家**握着就用它写，别再 open 一次（1.31）。
        # 以前这里只判 `mode == "watch"`，而 1.8 尾巴让"轻模式定时期间也握口"，
        # 于是定时一开，AI 工具来发命令就会走 transact → 打开被自家句柄挡住 →
        # 报"被其他程序占用（串口助手）"，其实占它的是我们自己。
        if self.loop.running and self.loop.ser is not None:
            # 口被**自家**握着就用它写，别再 open 一次（1.31）：1.8 尾巴让"轻模式定时期间也握口"，
            # 而这里原来只判 `mode == "watch"`，于是定时一开，AI 工具来发命令就会去 open →
            # 被自家句柄挡住 → 报"被其他程序占用（串口助手）"，其实占它的是我们自己。
            if mode == "hex":
                payload = payload_bytes          # 原始字节：不补换行
            else:
                payload = payload_bytes + serial_io.decode_newline(
                    self.effective_cfg().get("newline"), "\\r\\n")
            # **写 + 等回显整段持锁**：定时线程也在写同一个口，字节交错就是乱码，
            # 设备收到的不是一条完整命令（实测表现为"只有 TX 没有 RX"）。
            with self.loop.port_lock:
                self.loop.take()                 # 倒掉写之前就在缓冲里的旧字节
                self.loop.ser.write(payload)
                self.logger.append("TX", command)
                # 标记"接下来这段回显是手动触发的"，读线程据此把来源标成 manual。
                # **两处都要标**：self 是 WebServer（自己的逻辑用），loop 是读线程真正读的那个。
                self._manual_until = time.time() + 2.0
                self.loop._manual_until = self._manual_until
                if source != "web" or not from_page:
                    self.loop._push_frame(command, "", "", 0,
                                          source if source != "web" else "external")
                if self.mode == "watch":
                    return {"ok": True, "accepted": True, "mode": self.mode}
                # 轻模式（定时占口）也要**同步拿回显**：只回 accepted 会让调用方按
                # no_response 判失败 —— 等于换个姿势失败。
                cfg = self.effective_cfg()
                raw = self._wait_echo(
                    max(0.8, int(cfg.get("wait_first_byte_ms") or 1500) / 1000.0),
                    max(0.02, int(cfg.get("timeout_read_ms") or 200) / 1000.0))
            text = raw.decode("utf-8", errors="replace")
            if text:
                self.logger.append("RX", text)
            return {"ok": True, "accepted": True, "mode": self.mode,
                    "port": resolved["device"],
                    "port_source": "override" if (com or self.override_com) else "auto",
                    "rx_text": text,
                    "rx_hex": " ".join("%02X" % b for b in raw),
                    "bytes": len(raw), "empty": not raw}


        # 1.45：本次调用的 com/baud 覆盖（不落盘）。
        # 转发过来的命令带着调用方的 --com；实例必须用它，而不是拿场景重新解析
        # （双 CH340 时重新解析就歧义了 —— 命令明明有口却被判"分不清"）。
        cfg = dict(self.effective_cfg())
        if com:
            cfg["com"] = str(com)
            cfg["_override_com"] = str(com)
        if baud:
            cfg["baud"] = int(baud)
        if com or baud:
            from ..ports import resolve_port as _resolve
            resolved = _resolve(cfg)
        else:
            resolved = self.current_device()
        # 从这里往下是"真的动了硬件"：失败也必须在页面上留一行（1.33 判据 3）
        # Bug 11：hex 模式必须把解析出来的原始字节传下去，
        # 否则 transact 会把 "AA 55" 当成 5 个 ASCII 字符发出去。
        # 拿不到锁就**说清楚**，不要无限等：一个卡死的发送（比如串口在但对面不说话、
        # 驱动把 open 挂住）会把 port_lock 一直握着，后面每个命令都排队到天荒地老
        # —— 用户看到的是"整页发不动"，我 11-09 现场就撞上了。
        if not self.loop.port_lock.acquire(timeout=15):
            raise BridgeError(
                "port_busy",
                "上一个命令还没结束（超过 15 秒），这条没发出去。",
                hint=["等一下再试", "如果串口换了/拔了，点[回到自动识别]重新找口",
                      "实在卡住就点[暂停并释放串口]让它放开"],
            )
        try:
            result = serial_io.transact(
                cfg, resolved["device"], command,
                raw_bytes=payload_bytes if mode == "hex" else None)
        except BridgeError:
            # 失败帧：只在"真的尝试过"之后才推（换行/占用的错误也得让用户看见）
            frame = self.loop._push_frame(
                command, "", "", 0, source if source != "web" else "external")
            if isinstance(frame, dict):
                frame["note"] = "发送失败"
            raise
        finally:
            self.loop.port_lock.release()
        self.logger.append("TX", command)
        if result["rx_text"]:
            self.logger.append("RX", result["rx_text"])
        self.state.update(port=resolved["device"], baud=int(cfg.get("baud") or 0))

        # 同步发送却一个字节都没收到 → 这是故障，不是"成功但回显为空"。
        # 理由同 cli._require_reply：空的成功结果最容易被当成"命令执行了"。
        #
        # ⚠️ 但抛出之前必须先让规则知道"这次没回"：
        # `no_response` 判据靠连续计数触发（失联告警是最常用的规则类型）。
        # 如果直接抛，判定根本跑不到，这条规则就等于**完全失效**（用户实测报过）。
        if result["empty"]:
            self.loop.no_response_streak += 1
            self._maybe_check("", "manual")
            raise serial_io.describe_no_response(resolved, cfg)

        self.loop.no_response_streak = 0
        # Bug 7：手动发的回显也要跑判定，否则"设备回了 FAULT 但 alerts 是空的"
        self._maybe_check(result["rx_text"], "manual", result.get("rx_bytes"))
        # AI 发起的命令记一帧，网页才看得到（Bug 8）
        if source != "web" or not from_page:
            self.loop._push_frame(command, result["rx_text"], result["rx_hex"],
                                  result["bytes"],
                                  source if source != "web" else
                                  ("web" if from_page else "external"))
        return {
            "ok": True,
            "accepted": True,
            "mode": self.mode,
            "port": resolved["device"],
            "port_source": "override" if (com or self.override_com) else "auto",
            "rx_text": result["rx_text"],
            "rx_hex": result["rx_hex"],
            "bytes": result["bytes"],
            "elapsed_ms": result["elapsed_ms"],
            "note": resolved["note"],
        }


    def _maybe_check(self, text, source, raw_bytes=None):
        """判定入口。**开关判断不在这里**——交给 RuleChecker 统一做。

        以前这里也抄了一份"读状态文件的 check_enabled/check_source"，
        结果和 RuleChecker 的分支不完全一样：配置里写 check_source="both" 时，
        这里按状态文件的空值判成 watch_only，手动命令永远不判定。
        两份开关逻辑必然走偏，只留一份。
        """
        try:
            self.loop._run_checks(text, source, raw_bytes)
        except Exception:
            pass


    def _human_hold_seconds(self):
        try:
            from ..uiprefs import read_prefs
            return float(read_prefs(self.paths).get("human_hold_seconds") or 10)
        except Exception:
            return 10.0

    def control(self, action, actor="human"):
        # 按"动作 + 源状态"校验：resume 的目标也是 ai，不能只看目标状态
        mapping = {
            "ai": (STATE_AI, "交给AI", {STATE_HUMAN, STATE_PAUSED}),
            "pause": (STATE_PAUSED, "暂停AI", {STATE_AI}),
            "resume": (STATE_AI, "继续AI", {STATE_PAUSED}),
            "human": (STATE_HUMAN, "人类接管", {STATE_AI, STATE_PAUSED}),
            # 1.44：页面上"人还在打字"时续期用；不是状态迁移，只重置倒计时
            "renew": (STATE_HUMAN, "续期", {STATE_HUMAN}),
        }
        if action not in mapping:
            raise BridgeError("config", "未知的控制动作：%r" % action)
        target, label, sources = mapping[action]
        # AI 不能在人看着网页时自己把控制权抢回去
        # "自授权"= 调用方是 AI 且当前没人在看网页；这一步必须留痕（见 cli.cmd_control）
        actor_id = str(actor or "human").lower()
        self_granted = bool(
            target == STATE_AI and actor_id == "ai" and not self.state.presence_alive()
        )
        if target == STATE_AI:
            self.state.assert_self_grant_allowed(actor)
        # 状态文件里的 changed_by 要记"谁真正点的"，不能一律写 web：
        # 否则 AI 自己接管和人类点按钮在审计里长得一模一样。
        changed_by = "ai" if actor_id == "ai" else "web"
        audit = " ".join([
            "⚠ 你现在是以 AI 身份自授权：当前无人在看网页，控制权由 AI 自己取得，"
            "已记入状态文件 changed_by=ai（可审计）。" if self_granted else ""
        ]).strip()
        current_state = self.state.read().get("state")
        # 1.44-b（用户 20:1x）：倒计时期间点「人类接管」**必须升级成永久**。
        # 以前这里幂等直接返回"已经是人类接管"，于是人想"钉住"却钉不住。
        if action == "human" and not self_granted:
            current = self.state.force_state(
                STATE_HUMAN, "human-explicit",
                "人类点击了[人类接管]（不自动恢复）", permanent=True)
            return {
                "ok": True, "control": STATE_HUMAN, "state": current,
                "port_action": "unchanged", "watch_running": self.loop.running,
                "self_granted": False,
                "note": "已切换为「人类接管」：不会自动恢复",
            }
        # 幂等：已经是目标状态就当作成功，不要报 400。
        # （早期版本把 human->human 当成非法迁移，导致"已经在人类手中"时
        #  再点[人类接管]会报错，同一个按钮有时好有时坏，非常迷惑。）
        if current_state == target:
            return {
                "ok": True,
                "control": current_state,
                "state": self.state.read(),
                "port_action": "unchanged",
                "watch_running": self.loop.running,
                "self_granted": self_granted,
                "note": " ".join(["已经是%s，无需切换" % label, audit]).strip(),
            }
        if current_state not in sources:
            raise BridgeError(
                "control_blocked",
                "当前是 %s，不能执行[%s]。" % (current_state, label),
                hint=["human 只能[交给AI]；ai 只能[暂停AI]/[人类接管]；"
                      "paused 可以[交给AI]/[继续AI]/[人类接管]"],
            )
        try:
            if action == "renew":
                current = self.state.renew_human_command(
                    hold_s=self._human_hold_seconds())
            elif action == "human":
                # 点按钮 = **永久**接管（判据 4）；发命令才是 10 秒临时接管
                current = self.state.force_state(
                    STATE_HUMAN, "human-explicit",
                    "人类点击了[人类接管]（不自动恢复）", permanent=True)
            elif action == "ai":
                current = self.state.force_state(STATE_AI, changed_by, label)
            else:
                current = self.state.set_state(target, changed_by, label)
        except ValueError as exc:
            raise BridgeError("control_blocked", "状态迁移被拒绝（%s）。" % exc)

        # 控制权一变，串口占用跟着变。走统一的收敛点，
        # 和看门狗、CLI、serial_control 工具用的是同一段逻辑。
        forced = self.ports.reconcile("web-control") or {}
        port_action = forced.get("action", "unchanged")

        result = {
            "ok": True,
            "control": current.get("state"),
            "state": current,
            "port_action": port_action,
            "watch_running": self.loop.running,
            "self_granted": self_granted,
        }
        if audit:
            result["note"] = audit
        return result


    def set_watch(self, body):
        current = self.state.read()
        merged = dict(current.get("watch") or {})
        was_enabled = bool(merged.get("send_enabled"))
        allowed_keys = {
            "send_enabled", "check_enabled", "check_source", "command",
            "interval_ms", "timeout_ms", "command_mode",
        }
        for key, value in (body or {}).items():
            if key not in allowed_keys:
                continue
            # 没传 / 空值 = "这次不改这一项"。直接写成空串会出事：
            # check_source="" 会被 RuleChecker 回落成 watch_only，
            # 于是判定被静默关掉，界面上还看不出来（真实踩过）。
            if value in (None, "", [], {}):
                continue
            if key == "check_source":
                normalized = normalize_check_source(value)
                if normalized not in CHECK_SOURCES:
                    raise BridgeError(
                        "config", "check_source 只能是 watch_only / manual_only / both",
                        hint=["watch_only=只判定时轮询；manual_only=只判手动命令；both=都判",
                              "（历史写法 all 等同于 both）"])
                merged[key] = normalized
                continue
            merged[key] = value
        self.state.update(watch=merged)
        # 关 → 开 = 新一轮：统计从头算。
        # 以前 sent_count 跨启停累计不清零，"已发 37 次"里混着上一轮的次数，
        # 用户根本没法用它判断这一轮到底发了多少（1.8 的尾巴）。
        # 只在"真的从关到开"时清；开着的状态下改间隔/命令不清。
        if merged.get("send_enabled") and not was_enabled:
            stats = self.autosend_stats
            stats["sent"] = 0
            stats["skipped"] = 0
            stats["avg_gap_ms"] = 0
            stats["errors"] = 0
            stats["last_error"] = ""
            stats["last_tx"] = 0.0
        return {"ok": True, "watch": merged}
