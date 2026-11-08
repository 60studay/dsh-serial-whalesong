# -*- coding: utf-8 -*-
"""HTTP 小工具（给 `selfcheck_web_api.py` 用）。

为什么单独拆：那个脚本已经 700 行，逼近 `selfcheck_size.py` 的倒计时线。
这些函数是"怎么发一个请求 / 怎么读错"，和"测哪些接口"是两件事，拆开也更好读。

文件名以 `_` 开头：**它不是一个自检脚本**，不会被 `selfcheck_*.py` 统计进去
（统计只认 `selfcheck_` 前缀）。
"""

import json
import time
import urllib.error
import urllib.request


def http_get(base, path, timeout=8):
    """返回 (status, body_text)。连接失败时 status=None。"""
    try:
        req = urllib.request.Request(base + path)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, str(exc)


def http_post(base, path, payload=None, timeout=15):
    try:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, str(exc)


def as_json(text):
    """尽力解析 JSON。解析不了返回 None —— 这就说明接口返回的不是 JSON。"""
    try:
        return json.loads(text)
    except Exception:
        return None


def err_kind(data):
    """从错误体里取 kind（拿不到就回空串）。"""
    if not isinstance(data, dict):
        return ""
    err = data.get("error") or {}
    return str(err.get("kind") or "")


def wait_ready(base, seconds=20.0):
    """等临时实例真的能应答（进程起来到能服务之间有一小段空窗）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _ = http_get(base, "/status", timeout=1.0)
        if status == 200:
            return True
        time.sleep(0.2)
    return False
