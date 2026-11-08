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
import urllib.error
import urllib.request

from . import cli_query, cli_web
from . import io as serial_io
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


def _forward_to_web(paths, scene_name, command, actor):
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


def cmd_send(paths, args):
    scene = _scene(paths, args.scene)
    cfg = scene.cfg
    state = SharedState(paths["runtime_root"], scene.name)
    logger = Logger(paths["log_root"], scene.name)

    actor = (args.actor or "human").lower()
    if actor == "ai":
        allowed, current = state.check_ai_allowed()
        if not allowed:
            reason = state.ai_block_reason() or ""
            raise BridgeError(
                "control_blocked",
                "AI 现在不能发命令：控制权是 %s。" % current.get("state"),
                hint=[reason,
                      "人类发任意命令会自动把控制权拉回 human",
                      "查看当前状态：serial_status"],
            )
    else:
        # 安全规则 1：人类命令永远优先，AI 托管立即让位
        state.note_human_command()

    if args.auto:
        engine = RuleEngine(scene.watch_cfg())
        if not engine._is_allowed(args.command):
            raise BridgeError(
                "allowlist_blocked",
                "自动命令 %r 不在白名单内。" % args.command,
                hint=["在场景 config.json 的 watch.allowed_auto_commands 里加上它",
                      "白名单为空时禁止一切自动命令"],
            )

    # 一级：管家在跑 → 走它（常驻连接，~几十毫秒）
    via_daemon = _forward_to_daemon(paths, scene.name, args.command, actor,
                                    no_wait=bool(getattr(args, "no_wait", False)))
    if via_daemon is not None:
        return _ok(_require_reply(via_daemon, cfg))

    # 二级：网页在跑时统一走它的 /send，这样用户能在页面上看到 AI 发了什么。
    forwarded = _forward_to_web(paths, scene.name, args.command, actor)
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
            "port_source": "web",
            "note": forwarded.get("note") or "已交给网页发送",
            "response": forwarded.get("rx_text") or "",
            "response_hex": forwarded.get("rx_hex") or "",
            "bytes": int(forwarded.get("bytes") or 0),
            "elapsed_ms": int(forwarded.get("elapsed_ms") or 0),
            "empty": not bool(forwarded.get("rx_text")),
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

    send = sub.add_parser("send")
    send.add_argument("--scene", required=True)
    send.add_argument("--command", required=True)
    send.add_argument("--as", dest="actor", default="human", choices=["human", "ai"])
    send.add_argument("--auto", action="store_true", help="按自动命令处理，强制白名单检查")
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
