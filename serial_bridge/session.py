# -*- coding: utf-8 -*-
"""会话持有者（1.38）：把串口"开着"跨多次工具调用复用。

为什么需要：工具每次调用都是一个独立的 CLI 进程，"发一条 → 关掉"。
那个智能体要干的事（`AT+PAIR=…,20` 二十秒配对窗口、`AT+INIT`→`AT+INQ`→`AT+PAIR` 连发）
都要求"口一直开着"。没有会话就得每条命令重新 open，DTR/RTS 还会复位板子。

它是什么：**一个只服务一个口的小 HTTP 进程**，持着串口，收到 POST /send 就写+读。
CLI 的 `send --hold` 会懒启动它；`close --com` 或 60 秒空闲让它退出。

三条硬约束：
  1. **不许绕过控制权**：CLI 在把命令交给会话之前，已经做过 AI 门禁检查
     （human / paused 一律拒），会话本身不重复判断、也不放宽。
  2. **不许把口占死**：任何请求都会续命，但 60 秒没人用就自己退出并删掉状态文件。
  3. **不许静默死**：进程退出（含被 kill）前尽量删状态文件；CLI 发现连不上就
     视为"会话已失效"，重新起一个，不让调用方看到"口被占着"这种假象。
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import io as serial_io
from .errors import BridgeError
from .log import Logger
from .cli_base import _paths as _resolve_paths



def _paths_for_push():
    """给跨进程投帧用的 paths（会话进程启动时已经解析过一次，直接用）。"""
    try:
        return _resolve_paths()
    except Exception:
        return {"runtime_root": os.path.join(os.getcwd(), "logs", ".runtime")}


def _post_frame(com, tx, rx):
    """把这一条投进同场景网页的帧流（1.52）。网页没开就算了，不许影响收发。"""
    # 会话自己的状态文件只有 url/pid，没有"场景网页"的地址；
    # 所以由 CLI 在拉起会话时把网页地址塞进环境变量 WB_FRAME_URL（没有就不投）。
    url = os.environ.get("WB_FRAME_URL") or ""
    if not url:
        # 1.55：没有显式地址时用"最近活跃的页面"兜底（用户开着哪个就投给哪个）
        try:
            from . import push as _push
            _push.push_frame(_paths_for_push(), "", tx, rx, "session", com)
        except Exception:
            pass
        return
    try:
        import urllib.request
        payload = json.dumps({"tx": tx, "rx": rx, "source": "session", "port": com},
                             ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url.rstrip("/") + "/frames", data=payload,
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        urllib.request.urlopen(request, timeout=2).close()
    except Exception:
        pass


def _safe(device):
    return "".join(ch if ch.isalnum() else "-" for ch in str(device or ""))


def state_path(paths, com):
    return os.path.join(paths["runtime_root"], "state", "session-%s.json" % _safe(com))


def read_session(paths, com):
    """读会话状态；连不上/已死就当作没有（并顺手清理）。"""
    path = state_path(paths, com)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def clear_session(paths, com):
    path = state_path(paths, com)
    try:
        os.unlink(path)
    except OSError:
        pass


class Session(object):
    def __init__(self, paths, com, baud, idle_s=60.0, port=0):
        self.paths = paths
        self.com = str(com or "").strip()
        self.baud = int(baud or 115200)
        self.idle_s = float(idle_s or 60.0)
        self.port = int(port or 0)
        self.logger = Logger(paths["log_root"], "会话")
        self.ser = None
        self.lock = threading.Lock()
        self.last_use = time.time()
        self.seq = 0
        self.stop_event = threading.Event()
        self.httpd = None

    # ── 端口 ────────────────────────────────────────────────────────────
    def open(self):
        cfg = {"baud": self.baud, "newline": "\r\n",
               "timeout_read_ms": 300, "wait_first_byte_ms": 1200}
        self.ser = serial_io.open_port(cfg, self.com, self.baud)
        self.logger.append("SESSION", "打开端口 %s @ %d（保持会话）" % (self.com, self.baud))

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        clear_session(self.paths, self.com)
        self.logger.append("SESSION", "释放端口 %s（会话结束）" % self.com)

    # ── 收发 ────────────────────────────────────────────────────────────
    def send(self, text, wait=True):
        command = str(text or "")
        with self.lock:
            self.last_use = time.time()
            self.seq += 1
            n = self.seq
            if self.ser is None:
                raise BridgeError("port_busy", "会话已释放，请重新发起一次 hold 调用。")
            try:
                self.ser.write((command + "\r\n").encode("utf-8"))
            except Exception as exc:
                # 拔线/驱动掉：会话必须自己收场（判据 6）
                self.logger.append("SESSION", "写入失败，会话关闭：%s" % exc)
                self.stop_event.set()
                raise BridgeError("port_busy", "会话写入失败（设备可能被拔掉了）：%s" % exc)
            self.logger.append("TX", command)
            if not wait:
                return {"ok": True, "submitted": True, "seq": n}
            raw = b""
            deadline = time.time() + 1.2
            while time.time() < deadline and not raw:
                chunk = self.ser.read(256)
                if chunk:
                    raw += chunk
                else:
                    time.sleep(0.01)
            if raw:
                quiet = time.time() + 0.25
                while time.time() < quiet:
                    chunk = self.ser.read(256)
                    if chunk:
                        raw += chunk
                        quiet = time.time() + 0.25
                    else:
                        time.sleep(0.01)
            text_out = raw.decode("utf-8", errors="replace")
            if text_out:
                self.logger.append("RX", text_out)
            _post_frame(self.com, command, text_out)
            self.last_use = time.time()
            return {
                "ok": True,
                "seq": n,
                "rx_text": text_out,
                "rx_hex": " ".join("%02X" % b for b in raw),
                "bytes": len(raw),
            }

    # ── HTTP ────────────────────────────────────────────────────────────
    def serve(self):
        self.open()
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        payload = {"url": url, "port": self.httpd.server_address[1],
                   "pid": os.getpid(), "com": self.com, "baud": self.baud,
                   "started": time.time(), "idle_s": self.idle_s}
        path = state_path(self.paths, self.com)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        threading.Thread(target=self._watchdog, name="session-idle", daemon=True).start()
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self.close()

    def _watchdog(self):
        while not self.stop_event.is_set():
            time.sleep(0.5)
            if time.time() - self.last_use > self.idle_s:
                self.logger.append("SESSION", "空闲 %.0f 秒，自动释放 %s" % (self.idle_s, self.com))
                self.stop_event.set()
                try:
                    self.httpd.shutdown()
                except Exception:
                    pass
                return

    def _make_handler(self):
        session = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):     # 不刷屏
                pass

            def _json(self, code, data):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/ping"):
                    self._json(200, {"ok": True, "seq": session.seq, "com": session.com})
                else:
                    self._json(404, {"ok": False})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    self._json(400, {"ok": False, "error": {"kind": "config", "message": "bad json"}})
                    return
                if self.path.startswith("/close"):
                    self._json(200, {"ok": True, "closed": True})
                    threading.Thread(target=self._stop, daemon=True).start()
                    return
                if self.path.startswith("/send"):
                    try:
                        result = session.send(body.get("text") or "", body.get("wait", True))
                    except BridgeError as exc:
                        self._json(400, {"ok": False, "error": exc.to_dict()})
                        return
                    self._json(200, result)
                    return
                self._json(404, {"ok": False})

            def _stop(self):
                time.sleep(0.05)
                session.stop_event.set()
                try:
                    session.httpd.shutdown()
                except Exception:
                    pass

        return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="鲸歌串口会话（内部用）")
    parser.add_argument("--com", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--idle", type=float, default=60.0)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--entry", default="bridge.py", help="仅用于路径解析")
    args = parser.parse_args(argv)

    paths = _resolve_paths()
    session = Session(paths, args.com, args.baud, args.idle, args.port)
    try:
        session.serve()
    except KeyboardInterrupt:
        pass
    except BridgeError as exc:
        clear_session(paths, args.com)
        sys.stderr.write("会话启动失败：%s\n" % exc.message)
        return 1
    finally:
        clear_session(paths, args.com)
    return 0


if __name__ == "__main__":
    sys.exit(main())
