# -*- coding: utf-8 -*-
"""网页服务的"遥控器"：启动 / 查询 / 关闭，给 AI 工具用。

为什么要单独一层：`bridge.py web` 是**前台阻塞**的，AI 调它会把工具调用挂住。
这里改成"派一个后台子进程 + 等它就绪 + 立刻返回 URL"，AI 拿到 URL 直接贴给用户。

三条硬要求（都是实测踩过的）：
1. 幂等：同一场景已经有实例在跑，就返回它，不再起第二个（两个进程会抢串口）
2. 端口要挑：8899 常被 DSH 自己占着，被占就往后找，返回**真实**端口
3. close 要真的释放：不能只停 HTTP，串口句柄也得还回去
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .errors import BridgeError
from .state import _safe_name

DEFAULT_PORT = 8898          # 8899 被 DSH Web GUI 占着，默认从 8898 起；被占就往后找
PORT_SEARCH_LIMIT = 20


# ── 实例文件 ────────────────────────────────────────────────────────────────
def instance_path(paths, scene):
    return os.path.join(paths["runtime_root"], "state",
                        "%s.web.json" % _safe_name(scene))


def read_instance(paths, scene):
    """返回实例信息 dict（含 url/pid/port）；没有或已死返回 None。"""
    path = instance_path(paths, scene)
    try:
        with open(path, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    url = str(info.get("url") or "")
    if not url:
        return None
    status = _get(url, "/status", timeout=1.0)
    if not status:
        return None
    info["status"] = status
    return info


def _instance_alive(info, timeout=1.0):
    """实例还活着吗：能连上 `/status` 才算。

    **不信 pid**：pid 会被系统复用，端口/HTTP 才权威。
    """
    url = str((info or {}).get("url") or "").rstrip("/")
    if not url:
        return False
    return _get(url, "/status", timeout=timeout) is not None


def reap_dead(paths):
    """给失效的网页实例"收尸"：删掉指向已死服务的 state/*.web.json。

    以前每个场景要单独 `close` 一次，用户没法一条命令清干净；
    更糟的是死文件一直躺着，`serial_web status` 看着像"还在跑"。
    返回收了哪些、留下哪些，让工具层能如实报数。
    """
    state_dir = os.path.join(paths["runtime_root"], "state")
    reaped, kept = [], []
    try:
        names = sorted(os.listdir(state_dir))
    except OSError:
        return {"reaped": reaped, "kept": kept, "dir": state_dir}

    for name in names:
        if not name.endswith(".web.json"):
            continue
        path = os.path.join(state_dir, name)
        scene = name[:-len(".web.json")]
        try:
            with open(path, encoding="utf-8") as handle:
                info = json.load(handle)
        except (OSError, ValueError):
            info = None
        if _instance_alive(info):
            kept.append(scene)
            continue
        try:
            os.remove(path)
        except OSError:
            kept.append(scene)          # 删不掉就别假装收下了
            continue
        reaped.append(scene)
    return {"reaped": reaped, "kept": kept, "dir": state_dir}


def _get(url, path, timeout=2.0):
    try:
        with urllib.request.urlopen(url + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _post(url, path, payload, timeout=5.0):
    request = urllib.request.Request(
        url + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None


# ── 端口 ────────────────────────────────────────────────────────────────────
def _port_free(port):
    """端口能不能用。

    两个坑，都在 Windows 上实测踩到：
    1. 带 SO_REUSEADDR 的 bind 在 Windows 上**能抢占已占用的端口**，
       于是"检测"永远返回可用 —— 必须不带复用地试绑。
    2. 只试绑还不够稳：先探一下有没有人在监听。
    """
    port = int(port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False                    # 已经有人在听
    except Exception:
        pass
    finally:
        probe.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))      # 不设 SO_REUSEADDR
        return True
    except OSError:
        return False
    finally:
        sock.close()


def pick_port(preferred=0):
    """从 preferred 开始找一个能绑的端口。返回 (port, tried)。"""
    start = int(preferred or DEFAULT_PORT)
    tried = []
    for offset in range(PORT_SEARCH_LIMIT):
        port = start + offset
        tried.append(port)
        if _port_free(port):
            return port, tried
    raise BridgeError(
        "config",
        "没有可用端口：从 %d 起试了 %d 个都被占用。" % (start, PORT_SEARCH_LIMIT),
        hint=["显式指定 --port", "关掉占用端口的程序后再试"],
    )


# ── 动作 ────────────────────────────────────────────────────────────────────
OPEN_DEDUPE_S = 60.0


def _claim_open_slot(paths, url):
    """同一个 URL **60 秒内只弹一次**（P3 兜底：防止标签页越开越多）。

    看客心跳已经挡住了大部分重复弹，这里只是最后一道：连续调几个工具时，
    即使窗口状态判断失误，也不会"一秒开一个标签"。
    """
    stamp_path = os.path.join(paths["runtime_root"], "last-open.json")
    now = time.time()
    data = {}
    try:
        with open(stamp_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    try:
        last = float(data.get(url) or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < OPEN_DEDUPE_S:
        return False
    data[url] = now
    # 只留最近 20 条，别让这个文件长毛
    if len(data) > 20:
        items = sorted(data.items(), key=lambda kv: float(kv[1] or 0))[-20:]
        data = dict(items)
    try:
        os.makedirs(paths["runtime_root"], exist_ok=True)
        tmp = stamp_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, stamp_path)
    except OSError:
        pass
    return True


def _open_browser(url, paths=None):
    """把 URL 丢给系统浏览器。失败不影响服务本身（只是用户需要自己点链接）。

    传了 `paths` 就走 60 秒去重；不传（内部/测试调用）照旧直接开。
    """
    if paths is not None and not _claim_open_slot(paths, url):
        return False
    try:
        import webbrowser
        return webbrowser.open(str(url))
    except Exception:
        return False


def _viewer_idle(url, timeout=1.0):
    """浏览器窗口还在吗？（网页每 **5 秒** ping 一次，断了就是窗口被关了）

    问不到就当"有人看着"——宁可不弹，也不制造噪音。
    """
    try:
        with urllib.request.urlopen(str(url).rstrip("/") + "/viewer",
                                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return False
    return not bool((data or {}).get("viewing", True))


def _mark_viewer_seen(url, timeout=1.0):
    """刚弹过一次窗口，先记成"有人在看"，否则紧接着的下一次调用会再弹一遍。"""
    try:
        req = urllib.request.Request(str(url).rstrip("/") + "/ping", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        pass


def open_web(paths, scene, port=0, mode="light", open_browser=False,
             force_window=False, reopen_if_idle=False, wait_s=12.0):
    """起一个后台网页实例并返回它的 URL。已经在跑就直接返回现有的。

    三个参数必须分清（通讯板 12:48 / 12:49，加上用户后来加的一条）：

    - `open_browser`：这次"从没有到有"起页面时，要不要顺手把窗口打开。
    - `force_window`：**页面已经在跑**时也要弹一次。只有"用户亲口说要"才允许传 True。
    - `reopen_if_idle`：自动路径用。页面还在跑、但**浏览器窗口已经被关了**
      （看客心跳断了 ≥60 秒，或收到 /bye）→ 再弹一次。用户关窗口通常只是
      "先不看了"，下次用串口就该把页面给他送回来。
    """
    open_browser = bool(open_browser)
    force_window = bool(force_window)
    reopen_if_idle = bool(reopen_if_idle)

    existing = read_instance(paths, scene)
    if existing:
        url = existing.get("url")
        should_pop = force_window
        if not should_pop and reopen_if_idle and url:
            should_pop = _viewer_idle(url)
        if should_pop and url:
            _open_browser(url, paths)
            _mark_viewer_seen(url)
        return {
            "running": True,
            "already_running": True,
            "url": existing.get("url"),
            "port": existing.get("port"),
            "pid": existing.get("pid"),
            "scene": scene,
            "mode": (existing.get("status") or {}).get("mode"),
            "note": "该场景已经有网页实例在跑，直接复用（不会再起第二个）",
        }

    chosen, tried = pick_port(port)
    env = dict(os.environ)
    vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "vendor", "pyserial-3.5")
    parts = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    if os.path.isdir(vendor):
        parts.append(vendor)
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)

    argv = [sys.executable, "-m", "serial_bridge.cli", "web",
            "--scene", scene, "--port", str(chosen), "--mode", mode]
    # 显式把决定结果传给子进程：子进程自己也会读开关，但两边各读一次万一文件正在改
    # 就可能不一致。这里一次说定。
    argv.append("--open-browser" if open_browser else "--no-browser")
    subprocess.Popen(
        argv,
        cwd=parts[0],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + max(3.0, float(wait_s))
    while time.time() < deadline:
        time.sleep(0.2)
        info = read_instance(paths, scene)
        if info:
            status = info.get("status") or {}
            return {
                "running": True,
                "already_running": False,
                "url": info.get("url"),
                "port": info.get("port"),
                "pid": info.get("pid"),
                "scene": scene,
                "mode": status.get("mode") or mode,
                "note": "网页已启动（后台进程，关掉终端也不影响）",
            }
    raise BridgeError(
        "no_device",
        "网页没起来（等了 %.0f 秒）。" % wait_s,
        hint=["手工跑一次看错误：python bridge.py web --scene %s --port %d" % (scene, chosen),
              "端口可能被别的程序抢了，换个 --port"],
    )


def status_web(paths, scene):
    info = read_instance(paths, scene)
    if not info:
        return {"running": False, "scene": scene}
    status = info.get("status") or {}
    return {
        "running": True,
        "url": info.get("url"),
        "port": info.get("port"),
        "pid": info.get("pid"),
        "scene": scene,
        "mode": status.get("mode"),
        "control": status.get("control"),
        "port_held": status.get("port_held"),
        "transport": status.get("transport"),
    }


def close_web(paths, scene, wait_s=10.0):
    """停掉网页实例，并确认端口的**真的**放开了才返回。

    只等"实例文件消失"是不够的：文件是进程退出时删的，但 HTTP 端口
    可能还在收尾（实测要 ~2.5 秒）。这段时间里如果立刻返回"已停止"，
    用户马上开串口助手仍会看到端口被占——所以这里以"端口能重新绑定"为准。
    """
    info = read_instance(paths, scene)
    if not info:
        return {"running": False, "stopped": False, "scene": scene,
                "note": "该场景没有在跑的网页"}
    url = info.get("url")
    port = int(info.get("port") or 0)
    pid = info.get("pid")

    _post(url, "/shutdown", {}, timeout=3.0)

    deadline = time.time() + max(3.0, float(wait_s))
    while time.time() < deadline:
        time.sleep(0.2)
        gone = read_instance(paths, scene) is None
        # 端口能绑定 = 真的释放了（光看文件会早退）
        freed = _port_free(port) if port else True
        if gone and freed:
            return {"running": False, "stopped": True, "scene": scene,
                    "released": True,
                    "note": "网页已停止，端口已释放；Windows 上串口句柄可能还要 1~2 秒"
                            "才彻底归还，立刻打开失败的话稍等一下再试"}

    # 兜底：进程还赖着就结束它，再确认一次
    if pid:
        try:
            os.kill(int(pid), 9)
        except Exception:
            pass
        time.sleep(0.5)
    try:
        os.remove(instance_path(paths, scene))
    except OSError:
        pass
    freed = _port_free(port) if port else True
    return {"running": False, "stopped": True, "scene": scene,
            "released": bool(freed),
            "note": "HTTP 没在时限内退出，已强制结束进程" if freed
                    else "已强制结束进程，但端口仍被占用（可能被别的程序接手）"}

