# -*- coding: utf-8 -*-
"""命令行入口：index.js 只跟这一层说话。

约定：
- stdout 永远是一行 JSON（``{"ok": true, "data": ...}`` 或 ``{"ok": false, "error": {...}}``）
- 失败时 **不打印 Python 堆栈**，退出码 1
- 所有设备知识来自场景配置，这里只有通用逻辑

子命令（与 index.js 的工具一一对应）：
    scan                         列出串口 + 场景          → cli_query
    info   --scene S             场景参数 + 协议档案       → cli_query
    send   --scene S --command C [--as human|ai] [--auto] → 本文件（三级降级链）
    read   --scene S [--lines N] 读日志尾部               → cli_query
    status --scene S             控制权状态 + 最近告警     → cli_query
    control --scene S --action   切控制权                → cli_query
    watch  --scene S --action start|stop|status [--set k=v] → 本文件
    daemon --scene S --action start|stop|status          → 本文件
    web    --scene S [--port P] [--mode light|watch]     → cli_web
    webctl --action open|status|close|reap               → cli_web
    prefs  [--set KEY=VALUE]                             → cli_web

1.19 拆分说明：这个文件原来 751 行，塞了"输出格式 + 路径解析 + 11 个子命令 +
argparse 定义"四层。现在：
    cli_base.py   → 输出约定 / 路径规则 / 小工具（谁都要用，放这里避免循环 import）
    cli_query.py  → 五个"问一句答一句"的只读类命令
    cli_web.py    → 网页那组命令（web / webctl / prefs）
本文件只留**发送**与**入口**：`send` 的三级降级链（管家 → 网页 → 直接开串口）
是整个 CLI 里最容易出事的一段，留在入口旁边，改的时候一眼能看全。

对外引用不变：`from serial_bridge.cli import _paths / _default_scene / build_parser`
这类调用（bridge.py、daemon.py、selfcheck_*.py）照旧能用 —— 下面把名字 re-export 了。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from . import cli_query, cli_web
from . import io as serial_io
from . import session as session_mod
from .cli_base import (BASE, Logger, _coerce, _emit, _fail, _ok, _paths,
                       _scene, default_log_root)
# `_default_scene` 是 webctl 的老入口，外部（selfcheck_webctl.py）还在从 cli 导入它
from .cli_web import _default_scene
from .errors import BridgeError
from .ports import resolve_port
from .rules import RuleEngine
from .state import SharedState, _safe_name

# 兼容旧的导入路径：这些名字原来都定义在本文件里，别的地方还在用。
__all__ = ["BASE", "Logger", "build_parser", "main", "default_log_root",
           "cmd_daemon", "cmd_send", "cmd_watch", "_coerce", "_emit", "_fail",
           "_ok", "_paths", "_scene"]


def _require_reply(data, cfg):
    """同步发送却一个字节都没收到 → 按 `no_response` 报错，而不是"成功 + 一句警告"。

    为什么改成抛错：
      · 调用方（AI / 脚本）几乎总是为了拿回显才发命令，**空的成功结果是最容易
        被忽略的失败**——模型看到 ok=true 就以为命令执行了
      · `errors.py` 早就把 `no_response` 定义成一类故障，停留在警告文本里等于没用
      · 真正"发完就走、不等回显"的场合用 `--no-wait` / `wait:false`，
        那条路带 `submitted=true`，不经过这个判断
    """
    if data.get("submitted") or not data.get("empty"):
        return data
    raise serial_io.describe_no_response(
        {"device": data.get("port") or "串口"}, cfg)


def _forward_to_daemon(paths, scene_name, command, actor, no_wait=False):
    """管家在跑就把命令交给它（三级降级的第一级）。

    返回**已经组织好的 data dict**；管家没跑/不可达时返回 None，调用方继续降级。
    管家返回的业务错误（控制权/格式）照原样抛出去，不能降级——那不是"连不上"。
    """
    from . import daemon as daemon_mod
    info = daemon_mod.read_instance(paths, scene_name)
    if not info:
        return None
    result = daemon_mod.request(
        info.get("url") or "", "/send",
        {"text": command, "mode": "text", "actor": actor,
         "wait_ms": 0 if no_wait else None},
        timeout=5.0)
    if result is None:
        return None                       # 管家死了/端口不通 → 降级
    if not result.get("ok"):
        err = result.get("error") or {}
        raise BridgeError(err.get("kind") or "internal",
                          err.get("message") or "管家拒绝了这个命令",
                          hint=err.get("hint"))
    payload = result.get("data") or {}
    return {
        "scene": scene_name,
        "command": command,
        "port": payload.get("port") or "",
        "port_source": "daemon",
        "note": "已发出（不等回显）" if no_wait else "经由管家（常驻连接）",
        "response": payload.get("response") or "",
        "response_hex": payload.get("response_hex") or "",
        "bytes": int(payload.get("bytes") or 0),
        "submit_ms": int(payload.get("submit_ms") or 0),
        "elapsed_ms": int(payload.get("elapsed_ms") or 0),
        "empty": bool(payload.get("empty")),
        "submitted": bool(payload.get("submitted")),
        "via": "daemon",
        "transport": payload.get("transport"),
    }


def _forward_to_web(paths, scene_name, command, actor, com="", baud=0):
    """网页在跑时，把命令交给它发（Bug 8）。

    返回网页的 JSON 结果；网页没跑/不可达时返回 None，调用方回退到直接开串口。
    这样"网页开着 → 双方都看得见"，"网页没开 → 照常能用"。
    """
    inst = os.path.join(paths["runtime_root"], "state",
                        "%s.web.json" % _safe_name(scene_name))
    try:
        with open(inst, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    url = str(info.get("url") or "").rstrip("/")
    if not url:
        return None
    payload = json.dumps({
        "text": command,
        "mode": "text",
        "actor": actor,
        "source": "ai" if actor == "ai" else "web",
        # 1.45：`--com/--baud` 是**这一次调用**的覆盖，转发给页面时也必须带过去，
        # 否则实例拿着场景重新解析 → 双 CH340 时歧义 → 命令明明有口却发不出去。
        "com": com or "",
        "baud": int(baud or 0),
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url + "/send", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 4xx 也带 JSON 错误体（比如 control_blocked），要原样传出去
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None


def _warn_page_not_seeing(paths, scene_name):
    """网页记录在、但连不上时，明确告诉调用方"这条不会出现在页面上"。"""
    inst = os.path.join(paths["runtime_root"], "state",
                        "%s.web.json" % _safe_name(scene_name))
    if not os.path.exists(inst):
        return
    try:
        with open(inst, encoding="utf-8") as handle:
            url = str(json.load(handle).get("url") or "").rstrip("/")
    except (OSError, ValueError):
        return
    if not url:
        return
    print("[提示] 场景「%s」的网页实例（%s）连不上：这条命令只在日志里，"
          "**页面上不会显示**。想让它上页面，先重开网页："
          "python bridge.py webctl --action open --scene %s"
          % (scene_name, url, scene_name), file=sys.stderr)


def _session_http(url, path, payload=None, timeout=20):
    import urllib.request
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url + path, data=data,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _session_alive(url):
    try:
        return bool(_session_http(url, "/ping", None, timeout=3).get("ok"))
    except Exception:
        return False


def _ensure_session(paths, com, baud, idle=60.0, scene_name=""):
    """拿到（必要时拉起）这个口上的会话进程，返回它的 url。"""
    info = session_mod.read_session(paths, com)
    url = str((info or {}).get("url") or "")
    if url and _session_alive(url):
        return url
    if info:
        session_mod.clear_session(paths, com)
    import subprocess
    env = dict(os.environ)
    env["SERIAL_BRIDGE_RUNTIME_DIR"] = paths["runtime_root"]
    env["SERIAL_BRIDGE_LOG_DIR"] = paths["log_root"]
    env["SERIAL_BRIDGE_PROTO_DIR"] = paths["protocol_root"]
    # 1.52：让会话知道自己该把帧投给哪个网页（没有网页就不设，会话不打扰任何人）
    try:
        inst = os.path.join(paths["runtime_root"], "state",
                            "%s.web.json" % session_mod._safe(scene_name))
        if os.path.exists(inst):
            with open(inst, encoding="utf-8") as handle:
                env["WB_FRAME_URL"] = str(json.load(handle).get("url") or "")
    except Exception:
        pass
    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW（1.51）
        flags = 0x00000008 | 0x00000200 | 0x08000000
    subprocess.Popen(
        [sys.executable, "-m", "serial_bridge.session",
         "--com", com, "--baud", str(baud), "--idle", str(idle)],
        cwd=BASE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags)
    deadline = time.time() + 10
    while time.time() < deadline:
        info = session_mod.read_session(paths, com)
        url = str((info or {}).get("url") or "")
        if url and _session_alive(url):
            return url
        time.sleep(0.15)
    raise BridgeError("port_busy", "会话进程没起来（%s）。" % com,
                      hint=["口是否被别的程序占着", "再试一次；或先 serial_close 清掉旧会话"])


def cmd_close(paths, args):
    """释放串口会话（serial_close）。没有会话也算成功 —— 幂等。"""
    com = str(getattr(args, "com", "") or "").strip()
    if not com:
        # 不带 com：把所有会话都关掉
        closed = []
        try:
            names = os.listdir(os.path.join(paths["runtime_root"], "state"))
        except OSError:
            names = []
        for name in names:
            if name.startswith("session-") and name.endswith(".json"):
                com_x = name[len("session-"):-len(".json")].replace("-", ":")
                info = session_mod.read_session(paths, com_x)
                if info:
                    try:
                        _session_http(str(info.get("url")), "/close", {}, timeout=5)
                    except Exception:
                        pass
                session_mod.clear_session(paths, com_x)
                closed.append(com_x)
        return _ok({"closed": closed, "note": "已释放全部会话" if closed else "没有活动会话"})

    info = session_mod.read_session(paths, com)
    if info:
        try:
            _session_http(str(info.get("url") or ""), "/close", {}, timeout=5)
        except Exception:
            pass
    session_mod.clear_session(paths, com)
    return _ok({"com": com, "closed": True,
                "note": "会话已释放" if info else "这个口本来就没有会话"})


def cmd_send(paths, args):
    # 1.35：**场景可省**。只给 --com 时造一个临时场景，用完就丢 ——
    # 场景是"长期配置"，不该是"入场券"（对面 14:30 的判据）。
    adhoc_com = str(getattr(args, "com", "") or "").strip()
    adhoc_baud = int(getattr(args, "baud", 0) or 0)
    if args.scene:
        scene = _scene(paths, args.scene)
    elif adhoc_com:
        from .config import Scene
        scene = Scene("(临时)", "", {
            "com": adhoc_com, "baud": adhoc_baud or 115200,
            "newline": "\r\n", "match": "", "match_vid_pid": "",
        })
    else:
        raise BridgeError(
            "scene_missing",
            "要么给 scene（用 serial_list 看有哪些），要么给 com（直接说哪个口）。",
            hint=["serial_list 看当前插着哪些口",
                  "例如 serial_send(com=\"COM10\", text=\"AT\")"],
        )
    cfg = dict(scene.cfg)
    # 1.38：临时覆盖 com/baud，**不写回配置**
    if adhoc_com:
        cfg["com"] = adhoc_com
        cfg["_override_com"] = adhoc_com
    if adhoc_baud:
        cfg["baud"] = adhoc_baud
    state = SharedState(paths["runtime_root"], scene.name)
    logger = Logger(paths["log_root"], scene.name)

    actor = (args.actor or "human").lower()
    if actor == "ai":
        allowed, current = state.check_ai_allowed()
        if not allowed:
            reason = state.ai_block_reason() or ""
            # 同样留痕：网页没开的时候，用户事后翻「状态轨道」也能看到这次被拒
            try:
                state.append_alert({
                    "rule": "控制权拦截",
                    "blame": "AI 尝试发送「%s」被拒（控制权在 %s）"
                             % (args.command[:40], current.get("state")),
                })
            except Exception:
                pass
            raise BridgeError(
                "control_blocked",
                "AI 现在不能发命令：控制权是 %s。" % current.get("state"),
                hint=[reason,
                      "人类发任意命令会自动把控制权拉回 human",
                      "查看当前状态：serial_status"],
            )
    else:
        # 安全规则 1：人类命令永远优先，AI 托管立即让位。
        # 时长和网页走同一个偏好（否则 CLI 发命令是 10 秒、页面发是 5 秒，用户会懵）
        try:
            from .uiprefs import read_prefs
            hold = float(read_prefs(paths).get("human_hold_seconds") or 10)
        except Exception:
            hold = 10.0
        state.note_human_command(hold_s=hold)

    if args.auto:
        engine = RuleEngine(scene.watch_cfg())
        if not engine._is_allowed(args.command):
            raise BridgeError(
                "allowlist_blocked",
                "自动命令 %r 不在白名单内。" % args.command,
                hint=["在场景 config.json 的 watch.allowed_auto_commands 里加上它",
                      "白名单为空时禁止一切自动命令"],
            )

    # 0a 级：自动波特率（1.48）。对 HC-05 这类模块，扫档位是调试的第一步；
    # 让 AI 自己调 5 次不如一次调用返回"哪档有回应 + 回了什么"。
    if bool(getattr(args, "auto_baud", False)):
        resolved_device_for_baud = ""
        try:
            resolved_device_for_baud = resolve_port(cfg)["device"]
        except BridgeError:
            raise
        from .ports import BAUD_CHOICES
        from .io import transact as _transact
        tried, hit = [], None
        for baud in BAUD_CHOICES:
            cfg_try = dict(cfg)
            cfg_try["baud"] = int(baud)
            cfg_try["wait_first_byte_ms"] = 400
            cfg_try["timeout_read_ms"] = 200
            # 每个档位最多试两次：
            #   ① 打开串口会拉 DTR（多数板子=复位），板子还没起来时第一条会丢；
            #   ② 低档位的乱码可能让设备先回一句 "ERR CMD" —— 那不是"命中了"，
            #      不能拿 ERR 当证据（否则会对一个错误的档位报命中）。
            got = ""
            result = None
            for attempt in (1, 2):
                try:
                    result = _transact(cfg_try, resolved_device_for_baud, args.command)
                except BridgeError as exc:
                    if attempt == 2:
                        tried.append({"baud": baud, "kind": exc.kind})
                    result = None
                    continue
                got = (result.get("rx_text") or "").strip()
                if got and not got.upper().startswith("ERR"):
                    break
                got = ""
                time.sleep(0.2)
            if got:
                hit = {"baud": baud, "response": got,
                       "rx_hex": (result or {}).get("rx_hex") or ""}
                break
            tried.append({"baud": baud, "kind": "no_valid_response"})
        if not hit:
            raise BridgeError("no_response", "所有波特率都没有回应：%s"
                              % ", ".join(str(item["baud"]) for item in tried),
                              hint=["确认设备已上电、TX/RX 没接反",
                                    "有些模块要求先按 KEY 进 AT 模式"])
        return _ok({
            "scene": scene.name, "command": args.command,
            "port": resolved_device_for_baud, "port_source": "auto-baud",
            "baud": hit["baud"], "response": hit["response"],
            "response_hex": hit["rx_hex"], "bytes": len(hit["response"]),
            "empty": False, "tried": tried,
            "note": "自动波特率：%d 有回应（试了 %d 档）" % (hit["baud"], len(tried)),
        })

    # 0 级：要求"保持会话"（1.38）→ 交给会话进程。控制权检查在上面已经做过，
    #    会话不会绕过它（human/paused 到不了这里）。
    if bool(getattr(args, "hold", False)):
        resolved_com = ""
        try:
            # 注意：这里**不能**再写 `from .ports import resolve_port` ——
            # 函数内 import 会把 resolve_port 变成整个函数的局部名，
            # 于是下面普通分支里的 resolve_port(...) 直接 UnboundLocalError
            # （对面 18:10 抓到的 P0 就是这个）。
            resolved_com = resolve_port(cfg)["device"]
        except BridgeError:
            resolved_com = str(cfg.get("com") or "").strip()
        if not resolved_com:
            raise BridgeError("no_device", "没有可用的串口，无法建立会话。",
                              hint=["serial_list 看当前插着哪些口",
                                    "也可以直接给 com：serial_send(com=\"COM10\", hold=true)"])
        url = _ensure_session(paths, resolved_com, int(cfg.get("baud") or 115200),
                              float(getattr(args, "idle", 60) or 60),
                              scene_name=scene.name)
        result = _session_http(url, "/send",
                               {"text": args.command,
                                "wait": not bool(getattr(args, "no_wait", False))},
                               timeout=60)
        if not result.get("ok"):
            err = result.get("error") or {}
            raise BridgeError(err.get("kind") or "internal",
                              err.get("message") or "会话发送失败", hint=err.get("hint"))
        data = {
            "scene": scene.name, "command": args.command,
            "port": resolved_com, "port_source": "session",
            "response": result.get("rx_text", ""),
            "response_hex": result.get("rx_hex", ""),
            "bytes": int(result.get("bytes") or 0),
            "empty": not result.get("rx_text"),
            "session_seq": int(result.get("seq") or 0),
            "note": "会话第 %d 条（端口保持打开）" % int(result.get("seq") or 0),
        }
        if getattr(args, "no_wait", False):
            data["submitted"] = True
        return _ok(data)

    # 一级：管家在跑 → 走它（常驻连接，~几十毫秒）
    via_daemon = _forward_to_daemon(paths, scene.name, args.command, actor,
                                    no_wait=bool(getattr(args, "no_wait", False)))
    if via_daemon is not None:
        return _ok(_require_reply(via_daemon, cfg))

    # 二级：网页在跑时统一走它的 /send，这样用户能在页面上看到 AI 发了什么。
    forwarded = _forward_to_web(paths, scene.name, args.command, actor,
                                com=str(getattr(args, "com", "") or ""),
                                baud=int(getattr(args, "baud", 0) or 0))
    if forwarded is None:
        # 有网页记录但连不上 = 那个页面已经失效。这条命令只在日志里，**页面上不会出现**
        # （用户 A 方案：所有智能体用同一个场景 + 确认页面在跑；这里至少给一句明确提示，
        # 而不是让人以为"我发了它没收到"）。
        _warn_page_not_seeing(paths, scene.name)
    if forwarded is not None:
        if not forwarded.get("ok"):
            err = forwarded.get("error") or {}
            raise BridgeError(err.get("kind") or "internal",
                              err.get("message") or "网页转发失败",
                              hint=err.get("hint"))
        accepted_only = forwarded.get("mode") == "watch"
        data = {
            "scene": scene.name,
            "command": args.command,
            "port": forwarded.get("port") or "",
            # 转发路径也要如实说"这次用的是哪个口、怎么定的"：
            # 带 --com 时是 override，否则沿用实例给的（auto/override）。
            # 对面 19:30 的判据就是看这个字段（给了 --com 必须能看到 override）。
            "port_source": forwarded.get("port_source") or "web",
            "note": forwarded.get("note") or "已交给网页发送",
            "response": forwarded.get("rx_text") or "",
            "response_hex": forwarded.get("rx_hex") or "",
            "bytes": int(forwarded.get("bytes") or 0),
            "elapsed_ms": int(forwarded.get("elapsed_ms") or 0),
            "empty": not bool(forwarded.get("rx_text")),
            # 明确标记"这条是网页实例发的"：port_source 现在可能是 override（带 --com），
            # 不能再用它判断有没有走转发（自检踩过）。
            "forwarded": True,
        }
        if accepted_only:
            data["note"] = "已交给网页发送（监视模式，回显在网页上）"
        return _ok(_require_reply(data, cfg))

    resolved = resolve_port(cfg)

    # 没有管家、又要求"发完就走"：写完立刻关，回显之后用 read 取
    if getattr(args, "no_wait", False):
        sent = serial_io.send_only(cfg, resolved["device"], args.command)
        logger.append("TX", args.command)
        state.update(port=resolved["device"], baud=int(cfg.get("baud") or 0),
                     scene=scene.name)
        return _ok({
            "scene": scene.name,
            "command": args.command,
            "port": resolved["device"],
            "port_source": resolved["source"],
            "note": resolved["note"],
            "response": "",
            "response_hex": "",
            "bytes": 0,
            "submit_ms": sent["submit_ms"],
            "elapsed_ms": sent["submit_ms"],
            "empty": True,
            "submitted": True,
        })

    result = serial_io.transact(cfg, resolved["device"], args.command)
    logger.append("TX", args.command)
    if result["rx_text"]:
        logger.append("RX", result["rx_text"])
    state.update(port=resolved["device"], baud=int(cfg.get("baud") or 0), scene=scene.name)

    data = {
        "scene": scene.name,
        "command": args.command,
        "port": resolved["device"],
        "port_source": resolved["source"],
        "note": resolved["note"],
        "response": result["rx_text"],
        "response_hex": result["rx_hex"],
        "bytes": result["bytes"],
        "elapsed_ms": result["elapsed_ms"],
        "empty": result["empty"],
    }
    return _ok(_require_reply(data, cfg))


def cmd_daemon(paths, args):
    """管家：常驻持有连接的独立进程（start / stop / status）。

    start 是幂等的：已经在跑就返回现有 pid，不会起第二个。
    """
    from . import daemon as daemon_mod
    scene = _scene(paths, args.scene)
    port = int(args.port or daemon_mod.DEFAULT_PORT)
    idle = int(args.idle or daemon_mod.DEFAULT_IDLE_S)
    action = args.action

    if action == "start":
        status = daemon_mod.ensure_started(paths, scene.name, port=port, idle_s=idle)
        current = status.get("status") if isinstance(status, dict) else None
        return _ok({
            "running": True,
            "pid": (current or {}).get("pid") or status.get("pid"),
            "url": status.get("url"),
            "port": (current or status).get("port") or "",
            "control": (current or status).get("control") or "",
            "note": "管家已启动，占用 %s，期间串口助手打不开；空闲 %d 秒自动释放"
                    % ((current or status).get("port") or "串口",
                       (current or status).get("idle_timeout_s") or idle),
            "transport": (current or status).get("transport"),
        })

    if action == "stop":
        return _ok(daemon_mod.stop(paths, scene.name))

    status = daemon_mod.running(paths, scene.name)
    if not status:
        return _ok({"running": False, "scene": scene.name})
    return _ok(status)


def cmd_watch(paths, args):
    """监视/轮询的开关与运行时状态。

    **两个开关完全独立**（文档 3.6 的核心要求）：
      send_enabled  只管定时发送
      check_enabled 只管规则判定
    关掉任意一个，另一个照常工作；改动立即生效（settings 写进共享状态）。
    """
    scene = _scene(paths, args.scene)
    state = SharedState(paths["runtime_root"], scene.name)

    if args.action == "status":
        current = state.read()
        return _ok({
            "scene": scene.name,
            "watch": current.get("watch") or {},
            "state": current.get("state"),
            "alerts": state.read_alerts(10),
        })

    updates = {}
    for item in args.set or []:
        if "=" not in item:
            raise BridgeError("config", "--set 需要 key=value 形式：%r" % item)
        key, value = item.split("=", 1)
        updates[key.strip()] = _coerce(value.strip())
    if not updates:
        raise BridgeError("config", "--set 至少要给一个键，例如 send_enabled=true")
    if args.action == "stop":
        updates.setdefault("running", False)
    elif args.action == "start":
        updates.setdefault("running", True)

    current = state.read()
    merged = dict(current.get("watch") or {})
    merged.update(updates)
    state.update(watch=merged)
    return _ok({"scene": scene.name, "watch": merged, "changed": updates})


# ── 入口 ────────────────────────────────────────────────────────────────────
def build_parser():
    parser = argparse.ArgumentParser(description="DSH Whalesong（鲸歌）v2")
    sub = parser.add_subparsers(dest="action_name")

    # 只读类五个（cli_query）与网页那组（cli_web）各管自己的参数定义，
    # 这里只负责把它们挂上去 —— 加子命令只改一个文件。
    cli_query.add_parsers(sub)

    close = sub.add_parser("close")
    close.add_argument("--com", default="", help="要释放哪个口的会话；省略=全部")

    send = sub.add_parser("send")
    send.add_argument("--scene", default="",
                      help="场景名；**可省** —— 只给 --com 也能发（1.35）")
    send.add_argument("--com", default="", help="临时指定串口，不写回配置（1.38）")
    send.add_argument("--baud", type=int, default=0, help="临时指定波特率，不写回配置（1.38）")
    send.add_argument("--command", required=True)
    send.add_argument("--as", dest="actor", default="human", choices=["human", "ai"])
    send.add_argument("--auto", action="store_true", help="按自动命令处理，强制白名单检查")
    send.add_argument("--auto-baud", action="store_true",
                      help="依次试 BAUD_CHOICES，用第一条有回显的档位发这条命令")
    send.add_argument("--hold", action="store_true",
                      help="保持串口打开，供后续调用复用（会话）；空闲 60 秒自动释放")
    send.add_argument("--idle", type=float, default=60.0, help="会话空闲多少秒释放（默认 60）")
    send.add_argument("--no-wait", action="store_true",
                      help="发完立刻返回，不等回显（批量下命令用，回显之后用 read 取）")

    watch = sub.add_parser("watch")
    watch.add_argument("--scene", required=True)
    watch.add_argument("--action", default="status", choices=["start", "stop", "status"])
    watch.add_argument("--set", action="append", metavar="KEY=VALUE")

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--scene", required=True)
    daemon.add_argument("--action", default="status",
                        choices=["start", "stop", "status"])
    daemon.add_argument("--port", type=int, default=0)
    daemon.add_argument("--idle", type=int, default=0, help="空闲多少秒自动退出")

    cli_web.add_parsers(sub)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.action_name:
        parser.print_help()
        return 1
    paths = _paths()
    handler = {
        "scan": cli_query.cmd_scan,
        "info": cli_query.cmd_info,
        "send": cmd_send,
        "close": cmd_close,
        "read": cli_query.cmd_read,
        "status": cli_query.cmd_status,
        "control": cli_query.cmd_control,
        "watch": cmd_watch,
        "web": cli_web.cmd_web,
        "daemon": cmd_daemon,
        "webctl": cli_web.cmd_webctl,
        "prefs": cli_web.cmd_prefs,
    }[args.action_name]
    try:
        return handler(paths, args)
    except BridgeError as exc:
        return _fail(exc)
    except KeyboardInterrupt:
        return _fail(BridgeError("internal", "已中断"))
    except Exception as exc:  # 兜底：绝不把堆栈漏给插件层
        return _fail(BridgeError(
            "internal",
            "鲸歌内部错误。",
            hint=["把这条消息连同下面的 detail 一起反馈"],
            detail="%s: %s" % (type(exc).__name__, exc),
        ))


if __name__ == "__main__":
    import sys
    sys.exit(main())
