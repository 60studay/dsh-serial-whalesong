# -*- coding: utf-8 -*-
"""从**磁盘日志**补一段历史帧（1.80）。

## 为什么需要

帧流只在内存环里（200 条），页面没开的时候就丢了。用户实测：
"刚才没有在网页版看到你配置的步骤" —— 那几条是 CLI 发的，页面当时没开。

内存环是**实时**用的，磁盘日志才是**留档**的。所以：

    · 实时 → 环（页面开着才看得到）
    · 补历史 → `logs/<场景>/log.txt` 的尾部（页面关着也发生过的事都在这）

## 行格式（与 v1 兼容，别改）

```
[2026-11-10 19:47:03] TX P
[2026-11-10 19:47:03] RX OK PONG
[2026-11-10 19:47:05] WATCH 定时期间保持 COM7 打开（…）
```

只把 TX / RX 两类变成帧；WATCH/EVENT 这类**不冒充收发**（它们不是数据帧）。
一条 TX 后面紧跟的 RX 视为同一条命令的回显（日志本来就是这么写的）。
"""

import os
import re

_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(TX|RX|WATCH|EVENT|ALERT)\s?(.*)$")


def _epoch(stamp):
    """'2026-11-10 19:47:03' → epoch 秒（本地时区）。"""
    import time as _time
    try:
        return _time.mktime(_time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def tail_frames(log_path, lines=50):
    """读日志尾部，折成帧列表（按时间正序）。

    每条 TX 开一帧；紧跟其后的 RX 并进同一帧（和实时帧的形状一致），
    这样前端渲染逻辑不用分两套。
    """
    if not os.path.isfile(log_path):
        return []
    try:
        size = os.path.getsize(log_path)
        want = max(1, int(lines)) * 6           # 每条帧最多占两行，再多留点余量
        with open(log_path, "rb") as handle:
            if size > want * 200:
                handle.seek(max(0, size - want * 200))
            raw = handle.read()
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return []

    entries = []
    rx_pending = None
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        stamp, kind, body = match.group(1), match.group(2), match.group(3).strip()
        port = ""
        if kind == "TX" and body.startswith("@"):
            # 1.82：`[ts] TX @COM7 命令` —— 口号是这一帧自己的，别拿场景当前口去猜
            parts = body.split(None, 1)
            port = parts[0][1:]
            body = parts[1] if len(parts) > 1 else ""
        if kind == "TX" and not body:
            # 1.83：空命令行别单独成帧（它会渲染成"有时间、没内容"的怪行）
            continue
        if kind == "TX":
            rx_pending = {"tx": body, "rx": "", "ts": _epoch(stamp),
                          "source": "log", "port": port, "from_log": True}
            entries.append(rx_pending)
        elif kind == "RX":
            if rx_pending is not None and not rx_pending["rx"]:
                rx_pending["rx"] = body
            elif entries:
                # 1.83：孤立回显并进上一条（别产出 tx 为空的行）
                prev = entries[-1]
                prev["rx"] = (prev.get("rx") or "") + body
            else:
                entries.append({"tx": "(仅回显)", "rx": body, "ts": _epoch(stamp),
                                "source": "log", "port": "", "from_log": True})
            rx_pending = None
    return entries[-int(lines):]


def scene_log_path(paths, scene_name):
    return os.path.join(paths["log_root"], scene_name, "log.txt")
