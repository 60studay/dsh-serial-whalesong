# -*- coding: utf-8 -*-
"""把"页面之外发出去的帧"送进用户正在看的那个网页（1.55）。

为什么需要这个模块：
    1.33 只接了"转发给网页"那条路；会话（1.52）、管家（1.53）、`--com` 直发（1.55）
    都发生在页面进程之外。用户开着页面却什么都看不到，等于"我们说他干了，但没给你看"。

它怎么找页面：
    ① 有场景名 → 先试该场景自己的 `.web.json`
    ② 再按 mtime 从新到旧，试所有还活着的网页实例
    ③ 谁是"最近活跃"就投给谁 —— 用户通常就开着那一个

边界：
    · 投递失败**绝不抛异常**：页面没开、端口变了、被关了，都只是"没投出去"，
      不能连累这一次串口调用。
    · 只投 `tx / rx / source`，不改任何状态文件。
"""

import json
import os
import time
import urllib.request


def _state_dir(paths):
    return os.path.join(paths["runtime_root"], "state")


def _live_pages(paths):
    """所有记录在案的网页实例，按"最近活跃"排序。"""
    directory = _state_dir(paths)
    items = []
    try:
        names = os.listdir(directory)
    except OSError:
        return items
    for name in names:
        if not name.endswith(".web.json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            url = str(data.get("url") or "").strip()
            if not url:
                continue
            items.append({
                "url": url,
                "scene": str(data.get("scene") or ""),
                "mtime": os.path.getmtime(path),
            })
        except (OSError, ValueError):
            continue
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def _post(url, tx, rx, source, timeout=2.0):
    payload = json.dumps({"tx": tx, "rx": rx, "source": source},
                         ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/frames", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def push_frame(paths, scene_name="", tx="", rx="", source="external"):
    """投一帧给"用户正在看的页面"。返回投给了哪个 url（没投出去返回空串）。"""
    pages = _live_pages(paths)
    if not pages:
        return ""
    ordered = []
    if scene_name:
        ordered += [p for p in pages if p["scene"] == scene_name]
    ordered += [p for p in pages if not scene_name or p["scene"] != scene_name]
    for page in ordered:
        try:
            result = _post(page["url"], tx, rx, source)
            if result.get("ok"):
                return page["url"]
        except Exception:
            continue
    return ""


def push_source(actor):
    """把"谁发的"翻成页面徽章认的来源。"""
    return "ai" if str(actor or "").lower() == "ai" else "session"
