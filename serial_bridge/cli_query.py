# -*- coding: utf-8 -*-
"""只读类子命令：scan / info / read / status / control。

为什么单独一个文件（1.19 拆 cli.py）
------------------------------------
这五个子命令的共同点是"问一句、答一句"：不占串口、不落状态（control 除外，它只改状态机），
和 `send` 那条要开串口的三级降级链完全无关。拆出去之后 `cli.py` 只留发送与入口。

改这里要注意：`scan` / `info` 的返回字段被 index.js 的工具 schema 依赖
（`serial_list` / `serial_info`），**加字段可以，改名要同时改 schema 和自检**。
"""

import os

from .cli_base import _ok, _scene
from .config import list_scenes
from .errors import BridgeError
from .log import Logger
from .ports import resolve_port, scan_ports
from .state import SharedState
from .transport import describe_transport


def cmd_scan(paths, args):
    ports = scan_ports()
    scenes = list_scenes(paths["common_config"], paths["protocol_root"])
    # 给每个场景预解析一次，让 AI 一眼看到"这个场景现在会落到哪个口"
    for scene in scenes:
        try:
            merged = _scene(paths, scene["name"]).cfg
            resolved = resolve_port(merged)
            scene["resolved"] = {
                "device": resolved["device"],
                "source": resolved["source"],
                "note": resolved["note"],
            }
        except BridgeError as exc:
            scene["resolved"] = None
            scene["resolve_error"] = exc.to_dict()
    # 1.64：给每个场景补上**真正匹配到的**候选口。
    # 以前前端拿"所有端口"当候选，于是 match_vid_pid=1A86:7523 的场景
    # 会列出 COM3..COM10 八个（含 6 个蓝牙虚拟口），"匹配到 2 个"和候选对不上。
    try:
        from .ports import match_candidates
        for scene in scenes:
            scene["candidates"] = match_candidates(scene)
    except Exception:
        for scene in scenes:
            scene.setdefault("candidates", [])
    return _ok({"ports": ports, "scenes": scenes, "log_root": paths["log_root"]})


def cmd_info(paths, args):
    scene = _scene(paths, args.scene)
    cfg = scene.cfg
    resolved = None
    resolve_error = None
    try:
        resolved = resolve_port(cfg)
    except BridgeError as exc:
        resolve_error = exc.to_dict()
    watch = scene.watch_cfg()
    return _ok({
        "scene": scene.name,
        "com": str(cfg.get("com") or ""),
        "match": str(cfg.get("match") or ""),
        "match_vid_pid": str(cfg.get("match_vid_pid") or ""),
        "baud": int(cfg.get("baud") or 0),
        "transport": describe_transport(cfg,
                                        (resolved or {}).get("device")),
        "newline": cfg.get("newline") if cfg.get("newline") is not None else "\\r\\n",
        "protocol": scene.protocol_text(),
        "protocol_path": scene.protocol_path,
        "resolved": resolved,
        "resolve_error": resolve_error,
        "watch": {
            "send_enabled": bool(watch.get("send_enabled")),
            "check_enabled": bool(watch.get("check_enabled")),
            "check_source": str(watch.get("check_source") or "watch_only"),
            "command": watch.get("command") or "",
            "interval_ms": int(watch.get("interval_ms") or 0),
            "rules": watch.get("rules") or [],
            "allowed_auto_commands": watch.get("allowed_auto_commands") or [],
        },
    })


def cmd_read(paths, args):
    scene = _scene(paths, args.scene)
    logger = Logger(paths["log_root"], scene.name)
    text = logger.tail(args.lines)
    return _ok({
        "scene": scene.name,
        "text": text,
        "log_path": logger.path,
        "empty": text.strip() == "",
    })


def cmd_status(paths, args):
    scene = _scene(paths, args.scene)
    state = SharedState(paths["runtime_root"], scene.name)
    # 1.50：先结算"人接管到期"，再读状态 —— 否则返回的是结算前那份，
    # 用户会看到"倒计时已经到 0，status 还说 human"（对面 21:25 抓到）。
    try:
        state.expire_human_command()
    except Exception:
        pass
    current = state.read()
    # ai_may_send 必须是**真实门禁结果**，不能只看 state 字符串：
    # 没人开网页时门禁是宽松的，此时 state 仍是 human 但 AI 其实可以发。
    allowed, _ = state.check_ai_allowed()
    try:
        resolved = resolve_port(scene.cfg)
        transport_info = describe_transport(scene.cfg, resolved["device"])
    except BridgeError:
        transport_info = describe_transport(scene.cfg)
    return _ok({
        "scene": scene.name,
        "state": current,
        # 1.54：这次调用用的是哪个解释器（排查黑窗/环境问题要用）
        "python_path": os.environ.get("SERIAL_BRIDGE_PYTHON", ""),
        # 1.68：这个口现在被谁占着（别的程序占着时，AI 也能一眼看出来）
        "port_owner": _probe_owner(scene),
        "ai_may_send": bool(allowed),
        "watched": state.presence_alive(),
        "transport": transport_info,
        "alerts": state.read_alerts(args.alerts),
        "alerts_path": state.alerts_path,
    })


def cmd_control(paths, args):
    """改控制权。按文档的按钮表校验"当前状态能不能点这个按钮"。

    action: ai(交给AI) / pause(暂停AI) / resume(继续AI) / human(人类接管)

    注意：不能只看目标状态。resume 的目标也是 ai，而 ai→resume 在按钮表里是
    禁用的（那是"继续"已经停掉的托管，不是重复授予）。所以这里按"动作 + 源状态"校验。
    """
    scene = _scene(paths, args.scene)
    state = SharedState(paths["runtime_root"], scene.name)
    from .state import STATE_AI, STATE_HUMAN, STATE_PAUSED

    mapping = {
        "ai": (STATE_AI, "交给AI", {STATE_HUMAN, STATE_PAUSED}),
        "pause": (STATE_PAUSED, "暂停AI", {STATE_AI}),
        "resume": (STATE_AI, "继续AI", {STATE_PAUSED}),
        "human": (STATE_HUMAN, "人类接管", {STATE_AI, STATE_PAUSED}),
    }
    if args.action not in mapping:
        raise BridgeError("config", "未知的控制动作：%r" % args.action)
    target, label, sources = mapping[args.action]

    # AI 不能在人看着网页时自己把控制权抢回去（见 state.assert_self_grant_allowed）
    requester = (getattr(args, "by", None) or "cli")
    # "自授权"= 调用方是 AI 且当前没人在看网页（有人看时上面那行会直接拒绝）。
    # 这一步要留痕：调用方自己也该看到"这次是你自己拿的"，否则等于偷偷越权。
    self_granted = bool(
        target == STATE_AI
        and str(requester).lower() == "ai"
        and not state.presence_alive()
    )
    if target == STATE_AI:
        state.assert_self_grant_allowed(requester)

    notes = []
    if self_granted:
        notes.append(
            "⚠ 你现在是以 AI 身份自授权：当前无人在看网页，控制权由 AI 自己取得，"
            "已记入状态文件 changed_by=ai（可审计）。"
        )

    current_state = state.read().get("state")
    # 1.44-b：倒计时中点[人类接管]要能升级成永久（别被"幂等"挡住）
    if args.action == "human":
        current = state.force_state(
            STATE_HUMAN, "human-explicit",
            "人类点击了[人类接管]（不自动恢复）", permanent=True)
        return _ok({"scene": scene.name, "state": current,
                    "self_granted": False,
                    "note": "已切换为「人类接管」：不会自动恢复"})
    # 幂等：已经在目标状态就直接成功（重按同一个按钮不该报错）
    if current_state == target:
        return _ok({"scene": scene.name, "state": state.read(),
                    "self_granted": self_granted,
                    "note": " ".join(["已经是%s，无需切换" % label] + notes)})
    if current_state not in sources:
        raise BridgeError(
            "control_blocked",
            "当前是 %s，不能执行[%s]。" % (current_state, label),
            hint=["human 只能[交给AI]；ai 只能[暂停AI]/[人类接管]；"
                  "paused 可以[交给AI]/[继续AI]/[人类接管]"],
        )

    try:
        # 1.44：两条人接管的路要**分开记** ——
        #   human（点按钮）  → human-explicit，control_until=None（永久）
        #   ai（交还）       → 清掉计时
        #   renew（还在打字）→ 把倒计时续回完整时长
        if args.action == "human":
            current = state.force_state(
                STATE_HUMAN, "human-explicit",
                "人类点击了接管（不自动恢复）", permanent=True)
        elif args.action == "ai":
            current = state.force_state(STATE_AI, args.by or "cli", label)
        else:
            current = state.set_state(target, args.by, label)
    except ValueError as exc:
        raise BridgeError("control_blocked", "状态迁移被拒绝（%s）。" % exc)
    data = {"scene": scene.name, "state": current, "self_granted": self_granted}
    if notes:
        data["note"] = " ".join(notes)
    return _ok(data)


def add_parsers(sub):
    """把 scan / info / read / status / control 的定义挂到子命令表上。

    留在各自模块里，是为了以后加**一个**子命令时只改一个文件：
    以前所有 parser 都堆在 `build_parser()` 里，加到第 10 个就只能靠翻。
    """
    sub.add_parser("scan")

    info = sub.add_parser("info")
    info.add_argument("--scene", required=True)

    read = sub.add_parser("read")
    read.add_argument("--scene", required=True)
    read.add_argument("--lines", type=int, default=20)

    status = sub.add_parser("status")
    status.add_argument("--scene", required=True)
    status.add_argument("--alerts", type=int, default=20)

    control = sub.add_parser("control")
    control.add_argument("--scene", required=True)
    control.add_argument("--action", required=True,
                         choices=["ai", "pause", "resume", "human", "renew"])
    control.add_argument("--by", default="cli", help="谁改的（记在状态文件里）")


def _probe_owner(scene):
    """一次性探测串口占用（1.68）。失败一律返回 unknown，不能因为"查不出去"就报错。"""
    try:
        from .ports import resolve_port
        from . import io as serial_io
        cfg = dict(scene.cfg)
        try:
            device = resolve_port(cfg)["device"]
        except BridgeError:
            # 场景匹配到多个同型号口（两块 CH340 常见）：拿第一个候选来探，
            # 只要能回答"这个口现在被占着没有"就够了。
            from .ports import match_candidates
            cands = match_candidates({
                "com": cfg.get("com"), "match": cfg.get("match"),
                "match_vid_pid": cfg.get("match_vid_pid"),
            })
            if not cands:
                raise
            device = cands[0]
            cfg["_override_com"] = device
        try:
            serial_io.open_and_check(cfg)
            return {"owner": "free", "device": device, "detail": "%s 空闲" % device}
        except BridgeError as exc:
            if exc.kind == "port_busy":
                return {"owner": "other", "device": device,
                        "detail": "被其他程序占用（串口助手 / 别的脚本）",
                        "process_name": "未知（Windows 不提供占用进程名）"}
            return {"owner": "unknown", "device": device, "detail": exc.message}
    except Exception as exc:
        return {"owner": "unknown", "device": "", "detail": str(exc)}
