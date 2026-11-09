# -*- coding: utf-8 -*-
"""把"发给网页/会话"这几条转发路径从 cli.py 拆出来（2026-11-10）。

为什么拆：`cli.py` 长到 710 行，破了 700 行硬上限（结构体检会红）。
这几条路径的共同点是"**本进程不碰串口**，只把命令交给别处的持有者"：
  · `_forward_to_web`   交给同场景网页（页面开着时）
  · `_forward_to_daemon` 交给管家（管家持口时）
  · `_ensure_session` / `_session_http` / `_session_alive` 交给 hold 会话
  · `_warn_page_not_seeing` 提醒"页面开着但没显示"
它们和"真正打开串口发送"是两类事，分开放反而更好找。
"""

import json
import os
import time
import urllib.error
import urllib.request

from .errors import BridgeError
from .state import _safe_name


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

