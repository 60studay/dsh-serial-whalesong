# -*- coding: utf-8 -*-
"""网页的 HTTP 路由（从 supervisor.py 抽出来的）。

只做"把 URL 映射到 WebServer 的方法"这一件事：
解析请求体、调回调、把结果序列化成 JSON。
业务判断（控制权、端口、管家）全在 WebServer 里，这里不碰。
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .errors import BridgeError
from .ports import BAUD_CHOICES, scan_ports


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "SerialBridge/2.0"

    # ── 基础设施 ────────────────────────────────────────────────────────────
    @property
    def ctx(self):
        return self.server.context

    def log_message(self, *args):
        pass  # 静音，日志我们自己写

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def _error(self, exc, status=400):
        data = exc.to_dict() if isinstance(exc, BridgeError) else {
            "kind": "internal", "message": str(exc)}
        self._json({"ok": False, "error": data}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}

    # ── 路由 ────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                html = self.ctx["page"]
                return self._send(200, html, "text/html; charset=utf-8")
            if path in ("/app.css", "/app.js", "/brand.png", "/custom.css") or (
                    path.startswith("/js/") and path.endswith(".js")) or (
                    path.startswith("/fonts/") and path.endswith(".woff2")):
                # R2/R2b 把样式和逻辑拆成了独立文件，必须真的能把它们发出去，
                # 否则页面一加载就是白屏（而且只在浏览器控制台报 404，很难查）。
                asset = self.ctx["asset"](path)
                if asset is None:
                    return self._json({"ok": False, "error": {
                        "kind": "not_found", "message": path}}, 404)
                body, content_type = asset
                return self._send(200, body, content_type)
            if path == "/status":
                return self._json(self.ctx["status"]())
            # 外观（2.2）：背景图 / 不透明度 / 主色。运行期状态，存在 .runtime/ui-theme.json
            if path == "/theme":
                # tokens：公开的变量清单，写皮肤的人照着改（也是"接口"的一部分）
                return self._json({"ok": True, "theme": self.ctx["get_theme"](),
                                   "tokens": self.ctx["theme_tokens"]()})
            if path == "/theme/custom.css":
                # 运行期目录里的 theme/custom.css：想完全自定义的人把 CSS 放这儿
                found = self.ctx["theme_custom_css"]()
                if not found:
                    return self._json({"ok": False, "error": {
                        "kind": "not_found", "message": path}}, 404)
                return self._send(200, found, "text/css; charset=utf-8")
            # 本地图片（用户从电脑里选的）：从 .runtime/theme/ 里取，名字是审计过的
            if path.startswith("/theme/image/"):
                found = self.ctx["theme_image"](path.rsplit("/", 1)[-1])
                if not found:
                    return self._json({"ok": False, "error": {
                        "kind": "not_found", "message": path}}, 404)
                body, content_type = found
                return self._send(200, body, content_type)
            # 看客心跳：网页每 5 秒 ping 一次，关窗口时补一个 /bye。
            # /viewer 只读，**不刷新时间戳**（否则 open_web 一问就变成"有人看着"）。
            if path == "/ping":
                return self._json(self.ctx["ping"]())
            if path == "/bye":
                return self._json(self.ctx["bye"]())
            if path == "/viewer":
                return self._json(self.ctx["viewer"]())
            if path == "/poll":
                force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")
                # since 存在 = 按游标读、不消费（1.17）；不传 = 老行为（取走），兼容外部脚本
                raw_since = (query.get("since") or [None])[0]
                since = None
                if raw_since not in (None, ""):
                    try:
                        since = float(raw_since)
                    except ValueError:
                        since = None
                return self._json(self.ctx["poll"](force, since))
            if path == "/alerts":
                limit = int((query.get("limit") or ["20"])[0])
                return self._json({"ok": True, "alerts": self.ctx["alerts"](limit)})
            if path == "/ports":
                return self._json({"ok": True, "ports": scan_ports(),
                                   "bauds": BAUD_CHOICES})
            if path == "/rules":
                return self._json(self.ctx["get_rules"]())
            if path == "/channel":
                since = int((query.get("since") or ["0"])[0])
                limit = int((query.get("limit") or ["100"])[0])
                return self._json(self.ctx["channel_list"](since, limit))
            if path == "/favicon.ico":
                # 标签页图标也用同一只鲸鱼：用户一眼能在标签栏认出"这是鲸歌"，
                # 配合"[已失效]"前缀，失效标签更好认。
                asset = self.ctx["asset"]("/brand.png")
                if asset is None:
                    return self._send(204, b"")
                body, content_type = asset
                return self._send(200, body, content_type)
            return self._json({"ok": False, "error": {"kind": "not_found",
                                                      "message": path}}, 404)
        except BridgeError as exc:
            return self._error(exc)
        except Exception as exc:
            return self._error(exc, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            # 图片上传走**原始字节**，必须在 `_body()` 之前处理：
            # _body() 会把 Content-Length 这么多字节当 JSON 读走，再读一次就永远等不到数据。
            if path == "/theme/image":
                name = (query.get("name") or [""])[0]
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    raise BridgeError("config", "没有收到图片数据。")
                if length > 8 * 1024 * 1024 + 1024:
                    raise BridgeError("config", "图片太大了（上限 8 MB）。")
                raw = self.rfile.read(length)
                return self._json(self.ctx["save_theme_image"](raw, name))

            body = self._body()
            if path == "/send":
                return self._json(self.ctx["send"](
                    str(body.get("text") or ""),
                    str(body.get("mode") or "text"),
                    str(body.get("actor") or "human"),
                    str(body.get("source") or "web"),
                    # 页面自己发的带 client=page；别的调用方（脚本/另一个智能体/curl）
                    # 不带 → 这条会在页面上留一行「外部」，看得见谁动过手
                    from_page=(str(body.get("client") or "") == "page"),
                ))
            if path == "/control":
                # 缺字段/未知动作都用 400，但必须说清楚是哪一种
                action = str(body.get("action") or "").strip()
                if not action:
                    raise BridgeError(
                        "config",
                        "/control 需要一个 action 字段。",
                        hint=['请求体示例：{"action": "human"}',
                              "可选值：ai / pause / resume / human"],
                    )
                # actor 缺省当人类（网页按钮就是人类点的）；
                # 显式传 ai 时，会受"有人看着网页不许自授权"的规则约束
                return self._json(self.ctx["control"](action, str(body.get("actor") or "human")))
            if path == "/watch":
                return self._json(self.ctx["set_watch"](body))
            if path == "/theme":
                return self._json(self.ctx["set_theme"](body))
            if path == "/ping":
                return self._json(self.ctx["ping"]())
            if path == "/bye":
                # sendBeacon 只会发 POST，所以两个方法都要认
                return self._json(self.ctx["bye"]())
            if path == "/rules":
                return self._json(self.ctx["set_rules"](body))
            if path == "/channel":
                # AI ⇄ AI 通道：body 就是一条消息（from/type/claim/evidence/not_verified）
                return self._json(self.ctx["channel_send"](body))
            if path == "/channel/verify":
                # 一键复验：把这条消息的 evidence.cmd 重跑一遍，结果贴回通道
                return self._json(self.ctx["channel_verify"](
                    body.get("seq"), str(body.get("actor") or "human")))
            if path == "/port/pause":
                return self._json(self.ctx["pause_port"]())
            if path == "/port/probe":
                return self._json(self.ctx["probe_port"]())
            if path == "/alerts/clear":
                return self._json(self.ctx["clear_alerts"]())
            if path == "/port/resume":
                return self._json(self.ctx["resume_port"]())
            if path == "/reconnect":
                return self._json(self.ctx["reconnect"](
                    str(body.get("com") or ""), int(body.get("baud") or 0)))
            if path == "/port/auto":
                # 「回到自动识别」：清掉页面手动选的口，回到 config 的匹配规则（1.18）
                return self._json(self.ctx["auto_port"]())
            if path == "/autodetect":
                return self._json(self.ctx["autodetect"]())
            if path == "/mode":
                return self._json(self.ctx["set_mode"](str(body.get("mode") or "light")))
            if path == "/shutdown":
                self._json({"ok": True, "shutdown": True})
                self.ctx["request_shutdown"]()
                return None
            return self._json({"ok": False, "error": {"kind": "not_found",
                                                      "message": path}}, 404)
        except BridgeError as exc:
            return self._error(exc)
        except Exception as exc:
            return self._error(exc, 500)
