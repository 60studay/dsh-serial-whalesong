# -*- coding: utf-8 -*-
"""跨进程共享状态：控制权状态机 + 告警记录（v2 交接文档 3.5 / 3.6）。

为什么在文件里而不是内存里：网页进程和 DSH 插件进程是两个独立进程，
状态必须两边都看得见、都改得动。用 JSON 文件 + 原子替换 + 文件锁实现。

状态模型（3.5）：
    human  --交给AI-->  ai  --暂停AI-->  paused  --继续AI-->  ai
      ^                 |                 |
      +----人类接管-----+----人类接管-----+

安全规则：
    0. **默认 ai**：没人操作时 AI 可以直接发（用户 22:3x 定）；人类一发命令就抢回去
    1. 人类命令永远优先：人类发命令时自动把状态拉回 human
    2. AI 发送前必须检查 state == 'ai'
    3. 状态持久化（就是这个文件）
    4. 状态变化可见（网页轮询 /status 读这个文件）
"""

import json
import os
import threading
import time

from .errors import BridgeError

STATE_HUMAN = "human"
STATE_AI = "ai"
STATE_PAUSED = "paused"
VALID_STATES = (STATE_HUMAN, STATE_AI, STATE_PAUSED)

# 允许的状态迁移：from -> 可以进入的 to 集合
TRANSITIONS = {
    STATE_HUMAN: {STATE_AI},
    STATE_AI: {STATE_HUMAN, STATE_PAUSED},
    STATE_PAUSED: {STATE_AI, STATE_HUMAN},
}

# 默认控制权 = **ai**（用户 22:3x 拍板："没人操作的时候默认权限一定是 AI 的"）。
# 为什么不是 human：插件存在的意义就是"AI 在用串口"。默认拦着，等于每次开工都要人先点一下
# [交给AI]，而绝大多数时候根本没人守在页面前。
# 人类随时可以抢回去（发任何命令、或点[人类接管]），抢回去之后**保持** human，
# 直到人点[交给AI] —— 默认宽松，但不夺权。
DEFAULT_STATE = {
    "state": STATE_AI,
    "since": 0.0,
    "changed_by": "init",
    "port": "",
    "baud": 0,
    "scene": "",
    "watch": {},
    "note": "",
    # 页面上手动选的串口（1.18）：**用户的运行期选择，不进 config.json**。
    # 放在状态文件里，所以进程重启后仍然是它；空串 = 回到 config 的匹配规则。
    "override_com": "",
    "override_baud": 0,
    # 1.44：人接管到什么时候（epoch 秒）。
    #   None  = 永久（点了「人类接管」）
    #   数字  = 这个时刻之后自动回 ai（"人类发了条命令"的临时接管）
    "control_until": None,
}


DEFAULT_HUMAN_HOLD_S = 10.0


def _now():
    return time.time()


# 控制权文件的"活跃窗口"：网页进程每 2 秒心跳一次；
# 超过这个秒数没心跳，说明没人正在盯这一局人机协同，门禁按"无人看管"处理。
PRESENCE_TTL_S = 30

# 告警账本保留条数（1.20-b）：网页只看最近 N 条，更早的没有意义，
# 但文件如果不裁就会一直变厚。500 条约等于最近几小时的异常。
ALERT_KEEP = 500


class SharedState(object):
    """一个场景一份状态文件：runtime/state/<场景名>.json。

    文件锁用"独占创建 .lock 文件"实现，跨进程可用，不需要额外依赖。
    """

    def __init__(self, runtime_dir, scene):
        self.scene = scene
        self.dir = os.path.join(runtime_dir, "state")
        self.path = os.path.join(self.dir, "%s.json" % _safe_name(scene))
        self.lock_path = self.path + ".lock"
        self.alerts_path = os.path.join(runtime_dir, "alerts.jsonl")

    # ── 基础读写 ────────────────────────────────────────────────────────────
    def _ensure_dir(self):
        if not os.path.isdir(self.dir):
            os.makedirs(self.dir, exist_ok=True)

    def _acquire(self, timeout=1.0):
        self._ensure_dir()
        deadline = _now() + timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                return True
            except FileExistsError:
                # 陈旧锁：持有者进程没了就清掉
                if _now() > deadline:
                    self._break_stale_lock()
                    if _now() > deadline + 0.5:
                        return False
                time.sleep(0.01)
            except OSError:
                return False

    def _release(self):
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def _break_stale_lock(self):
        try:
            age = _now() - os.path.getmtime(self.lock_path)
            if age > 5:
                os.remove(self.lock_path)
        except OSError:
            pass

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("bad state")
        except (OSError, ValueError):
            data = {}
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        if merged.get("state") not in VALID_STATES:
            merged["state"] = STATE_AI
        return merged

    def _write(self, data):
        self._ensure_dir()
        tmp = self.path + ".tmp%d.%d" % (os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        # Windows 上 os.replace 会失败，如果目标文件此刻正被别的读者打开
        # （网页看门狗每秒读一次，自测里读得更密）。重试几次比让写操作失败安全。
        last_error = None
        for attempt in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.01 * (attempt + 1))
            except OSError as exc:
                last_error = exc
                time.sleep(0.01)
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise last_error if last_error else RuntimeError("state write failed")

    # ── 状态机 ──────────────────────────────────────────────────────────────
    def set_state(self, new_state, changed_by="unknown", note=""):
        if new_state not in VALID_STATES:
            raise ValueError("unknown state: %s" % new_state)
        if not self._acquire():
            raise RuntimeError("state file is locked")
        try:
            current = self.read()
            if new_state == current.get("state"):
                return current
            allowed = TRANSITIONS.get(current.get("state"), set())
            if new_state not in allowed:
                # 迁移非法：按文档的按钮表，非法按钮本来就该是禁用的。
                # 这里不抛错，而是明确拒绝并回原状态，前端据此高亮。
                raise ValueError("illegal transition: %s -> %s"
                                 % (current.get("state"), new_state))
            current["state"] = new_state
            current["since"] = _now()
            current["changed_by"] = changed_by
            current["note"] = note
            # 1.44：走迁移表进 human 的，一律按"临时接管"处理（带倒计时）；
            # 显式点击走 force_state(permanent=True)，不经过这里。
            if new_state != STATE_HUMAN:
                current["control_until"] = None
            else:
                current["control_until"] = _now() + DEFAULT_HUMAN_HOLD_S
            self._write(current)
            # 同一个物理口只有**一套**控制权（用户 23:1x 拍板 A 方案）
            self._sync_same_port(current)
            return current
        finally:
            self._release()

    def force_state(self, new_state, changed_by="unknown", note="",
                    until=None, permanent=False):
        """绕过迁移表（用于"人类命令永远优先"这类安全规则）。"""
        if new_state not in VALID_STATES:
            raise ValueError("unknown state: %s" % new_state)
        if not self._acquire():
            raise RuntimeError("state file is locked")
        try:
            current = self.read()
            current["state"] = new_state
            current["since"] = _now()
            current["changed_by"] = changed_by
            current["note"] = note
            # 1.44：人接管分两类 —— 显式点击（永久）/ 发了一条命令（到点自动归还）
            if new_state != STATE_HUMAN:
                current["control_until"] = None
            elif permanent:
                current["control_until"] = None
            elif until is not None:
                current["control_until"] = float(until)
            self._write(current)
            self._sync_same_port(current)
            return current
        finally:
            self._release()

    # 严格程度：paused（AI 被叫停）> human（人在控）> ai（交给 AI）
    STRICT_ORDER = {"ai": 0, "human": 1, "paused": 2}

    def strictest_state_for_port(self, device):
        """读**同一个物理口**上其它场景的控制权，返回最严格的那个。

        为什么需要（对面 23:35 抓到的绕过口子）：
            A 在 COM10 上 human → 同口所有场景变 human
            把 B 从 COM7 切到 COM10 → B 仍保留它自己的 ai
            AI 走 B 发命令 → 看到 ai → 放行 ⇒ 人的接管被绕过去了
        所以"切口那一刻"就要对齐：切过去时读新口上已有的控制权，取最严格者。
        """
        device = str(device or "").strip()
        if not device:
            return None
        best = None
        best_rank = -1
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return None
        for name in names:
            if not name.endswith(".json") or name.endswith(".web.json"):
                continue
            path = os.path.join(self.dir, name)
            if os.path.abspath(path) == os.path.abspath(self.path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            if str(data.get("port") or "").strip() != device:
                continue
            state = data.get("state")
            rank = self.STRICT_ORDER.get(state, -1)
            if rank > best_rank:
                best_rank, best = rank, state
        return best

    def _sync_same_port(self, current):
        """把控制权同步给**同一个物理口号**的其它场景。

        为什么：控制权原来按场景存，而一个口经常被多个场景引用
        （STC8G测试板 / 定时测试 / 蓝牙AT 都可能挂在 COM10 上）。
        用户在 A 场景点[交给AI]，AI 的工具却走 B 场景 → B 那份还是 human →
        用户看到"我明明交了权，它还说没权限"（真实反馈）。

        规则：**同一个口只有一套控制权**。谁改了，同口的场景一起跟。
        不碰别的字段（端口/波特率/定时都是各场景自己的）。
        """
        device = str(current.get("port") or "").strip()
        if not device:
            return
        new_state = current.get("state")
        changed_by = current.get("changed_by") or "unknown"
        until = current.get("control_until")
        # 1.46：永久接管改写成临时接管 = 降级；同口同步时也不许这么做
        if changed_by == "human-command" and new_state == STATE_HUMAN:
            for name in sorted(os.listdir(self.dir)) if os.path.isdir(self.dir) else []:
                if not name.endswith(".json") or name.endswith(".web.json"):
                    continue
                path = os.path.join(self.dir, name)
                if os.path.abspath(path) == os.path.abspath(self.path):
                    continue
                try:
                    with open(path, encoding="utf-8") as handle:
                        other = json.load(handle)
                except (OSError, ValueError):
                    continue
                if (other.get("state") == STATE_HUMAN
                        and other.get("changed_by") == "human-explicit"
                        and str(other.get("port") or "").strip() == device):
                    # 同口有"永久接管"的场景 → 本次不覆盖它的 until，直接把 until 抬成 None
                    until = None
        note = current.get("note") or ""
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return
        for name in names:
            if not name.endswith(".json") or name.endswith(".web.json"):
                continue
            path = os.path.join(self.dir, name)
            if os.path.abspath(path) == os.path.abspath(self.path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            if str(data.get("port") or "").strip() != device:
                continue
            # 1.44：状态一样也要对齐**倒计时**（否则同口另一个场景还是永久/旧的 until，
            # 判据 6 就挂了：三个场景必须共用一个 control_until）。
            if (data.get("state") == new_state
                    and data.get("control_until") == until
                    and data.get("changed_by") == changed_by):
                continue
            data["state"] = new_state
            data["since"] = _now()
            data["changed_by"] = changed_by
            data["control_until"] = until
            data["note"] = (note + "（同口 %s 同步）" % device).strip()
            tmp = path + ".tmp%d.%d" % (os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def update(self, **fields):
        """更新端口/波特率/监视配置等附带信息，不动 state 本身。"""
        if not self._acquire():
            raise RuntimeError("state file is locked")
        try:
            current = self.read()
            for key, value in fields.items():
                current[key] = value
            self._write(current)
            return current
        finally:
            self._release()

    def assert_self_grant_allowed(self, actor):
        """AI 想**自己**把控制权设成 ai 时，必须没人在看网页（headless）。

        为什么要有这条：`human -> ai` 在按钮表里是合法的（人类点[交给AI]），
        而 AI 也能调 `serial_control action=ai`。于是出现一个漏洞：
        **人在网页上明确点了[人类接管]，AI 却能一行命令把自己又设回 ai**，
        等于人类的选择被静默推翻。

        规则：
          · 有人在看网页（心跳在）→ 人类在场，只有人类能授权，AI 自授权一律拒绝
          · 没人看网页（headless）  → 允许 AI 自主接管，这正是"把任务交给 AI"的场景

        actor 传调用方身份（"ai" / "human" / "web" / "cli"）。非 ai 直接放行。
        """
        if str(actor or "").lower() != "ai":
            return
        if not self.presence_alive():
            return
        raise BridgeError(
            "control_blocked",
            "有人正在看网页，AI 不能自己接管控制权。",
            hint=[
                "请人在网页上点[交给AI]（或 [继续AI]）",
                "或者先关掉网页——没有人在看时 AI 可以自主接管",
                "这条规则是为了防止 AI 悄悄推翻人类刚做的选择",
            ],
        )

    def check_ai_allowed(self):
        """AI 发送前的门禁。返回 (allowed, state_dict)。

        只有 state == 'ai' 才放行；human / paused 一律拒绝。

        （早期版本为了照顾"插件装好但没开网页"的场景做过两段式宽松门禁：
        没人在看网页时直接放行。实测表明这会让控制权形同虚设——用户切到
        human 后 AI 照样能发，所以取消了。要单人使用就直接把控制权设为 ai，
        页面和 serial_control 都能改。）
        """
        current = self.read()
        # 1.44：每次门禁前先结算"人接管是否到期"，不依赖后台线程
        if self.expire_human_command():
            current = self.read()
        return current.get("state") == STATE_AI, current

    def ai_block_reason(self):
        """AI 不能发时，给出可操作的下一步（给工具层直接用）。"""
        current = self.read()
        state = current.get("state")
        if state == STATE_AI:
            return None
        if state == STATE_PAUSED:
            return ("AI 当前被暂停（paused）。"
                    "请在网页点[继续AI]，或用 serial_control action=resume 恢复。")
        return ("控制权在人类手上（human）。"
                "请在网页点[交给AI]，或用 serial_control action=ai 交给 AI。")

    def presence_alive(self):
        """网页/监视进程最近是否在心跳（说明有人正在看着这一局）。"""
        beat = self.read().get("presence") or 0
        try:
            beat = float(beat)
        except (TypeError, ValueError):
            return False
        return (_now() - beat) < PRESENCE_TTL_S

    def heartbeat(self, source="web"):
        """网页进程周期性调用，证明"有人在场、会看告警"。"""
        self.update(presence=_now(), presence_by=source)

    def note_human_command(self, hold_s=None):
        """人类命令永远优先：把状态拉回 human，并**带上倒计时**（1.44）。

        与「点人类接管」的区别：
          · 发命令   → `changed_by=human-command`，`control_until=now+hold`，到点自动回 ai
          · 点接管   → `changed_by=human-explicit`，`control_until=None`，只有点[交给AI]才回去
        倒计时里再发命令 = **重置**为完整时长（不是累加）。
        """
        current = self.read()
        if current.get("state") == STATE_PAUSED:
            return current                      # 暂停是明确意图，不受计时影响
        # 1.46：点了「人类接管」是**永久**，发命令不能把它降级成 10 秒倒计时
        # （用户实测："我都切回人类常驻模式了，发消息却进入了倒计时模式。"）
        if (current.get("state") == STATE_HUMAN
                and current.get("changed_by") == "human-explicit"):
            return current
        seconds = DEFAULT_HUMAN_HOLD_S if hold_s is None else float(hold_s)
        return self.force_state(
            STATE_HUMAN, "human-command",
            "人类发了命令，AI 托管已自动让位（%d 秒后归还）" % int(seconds),
            until=_now() + seconds)

    def renew_human_command(self, hold_s=None):
        """倒计时中人类还在打字：把倒计时续回去（前端在组合输入时调用）。"""
        current = self.read()
        if current.get("state") != STATE_HUMAN:
            return current
        if current.get("changed_by") != "human-command":
            return current                      # 显式接管是永久的，不续
        seconds = DEFAULT_HUMAN_HOLD_S if hold_s is None else float(hold_s)
        return self.force_state(STATE_HUMAN, "human-command",
                                "人类还在输入，接管时间已重置",
                                until=_now() + seconds)

    def expire_human_command(self):
        """临时接管到点了：回 ai 并留痕。返回是否真的发生了。"""
        current = self.read()
        if current.get("state") != STATE_HUMAN:
            return False
        until = current.get("control_until")
        if until is None:
            return False                        # 永久接管
        try:
            until = float(until)
        except (TypeError, ValueError):
            return False
        if _now() < until:
            return False
        # 到期回落是**常规行为**（像门禁自动落锁），不是故障：
        # 只改状态 + 一句中性 note，**不写告警列表**（那会在页面顶部弹红条）。
        self.force_state(STATE_AI, "human-expired",
                         "人接管已到期 → AI")
        return True

    # ── 告警 ────────────────────────────────────────────────────────────────
    def append_alert(self, alert):
        self._ensure_dir()
        record = dict(alert)
        record.setdefault("ts", _now())
        record.setdefault("scene", self.scene)
        record.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(self.alerts_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.trim_alerts()
        return record

    def trim_alerts(self):
        """告警账本只留最近 ALERT_KEEP 条（1.20-b），返回 True 表示真的裁了。

        这是网页"告警"列表的数据源。以前**只追加、从不裁剪**，
        和通讯板、日志一样属于"只进不出"的累加。
        做法照抄 `channel.py`（那里早就有 2000 条上限）：
        超了就重写成最近 N 条，**条目内容与时间戳一个字不改**，
        所以 `/alerts` 看到的还是原来那几条，只是更早的没了。
        """
        try:
            with open(self.alerts_path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return False
        if len(lines) <= ALERT_KEEP:
            return False
        tmp = self.alerts_path + ".tmp%d" % os.getpid()
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.writelines(lines[-ALERT_KEEP:])
            # Windows 上 os.replace 可能因为别的进程正在读而失败，重试几次；
            # 真失败就放弃裁剪（下次追加还会再试），不能把告警本身搞丢。
            for attempt in range(10):
                try:
                    os.replace(tmp, self.alerts_path)
                    return True
                except OSError:
                    time.sleep(0.01 * (attempt + 1))
        except OSError:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    def clear_alerts(self):
        """清空告警记录，返回清掉了多少条。

        为什么要有这个：测试时规则一直触发，告警条关了又弹、关了又弹；
        用户要的是一个"从这儿开始重新算"的动作（见通讯板 14:35 需求 3）。
        """
        try:
            with open(self.alerts_path, encoding="utf-8") as handle:
                count = sum(1 for line in handle if line.strip())
        except OSError:
            return 0
        try:
            with open(self.alerts_path, "w", encoding="utf-8"):
                pass
        except OSError:
            return 0
        return count

    def read_alerts(self, limit=20):
        try:
            with open(self.alerts_path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-max(1, int(limit)):]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _safe_name(name):
    keep = []
    for ch in str(name):
        keep.append(ch if (ch.isalnum() or ch in "-_") else "_")
    return "".join(keep) or "scene"


    def remember_port(self, device):
        """记住这个场景上次真正发成功过的口（1.71）。

        两块同型号 CH340 同时插着时，"按 match CH340"永远歧义 → 场景整个不可用
        （gcSTM32 那边就是被这个卡住的）。`last_port` 是上一次**真的发成功**的口，
        歧义时优先它不是猜，是"上次就这个口通的"。
        """
        try:
            self.update(last_port=str(device or ""))
        except Exception:
            pass
