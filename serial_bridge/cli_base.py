# -*- coding: utf-8 -*-
"""CLI 的公共底座：输出格式、路径解析、参数聚合。

为什么单独一个文件（1.19 拆 cli.py）
------------------------------------
`cli.py` 曾经一个文件 751 行，里面塞着"怎么打印 JSON"、"怎么找路径"、
"每个子命令的业务逻辑"、"argparse 定义"四层东西。拆的时候问题在于：
子命令分成两个文件之后，两边都要 `_ok()` 和 `_paths()`，
如果它们还留在 `cli.py`，`cli_web` / `cli_query` 就得反过来 import `cli`，
而 `cli` 又要 import 它们来填 dispatch 表 —— **循环 import**。

所以把这些"谁都要用、且不依赖业务"的东西放这里：
    BASE / default_log_root / _paths   路径规则（必须和 index.js 同一套）
    _emit / _ok / _fail                一行 JSON 的输出约定
    _scene / _coerce                   两个纯小工具
    Logger                             兼容 shim（真实现在 log.py）

`cli.py` 仍然 re-export 这些名字：`from serial_bridge.cli import _paths` 这类
外部引用（bridge.py、selfcheck_*.py、daemon.py）一个都不用改。
"""

import json
import os
import sys

from .config import load_scene, read_json
from .errors import BridgeError

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认 GBK，输出中文会崩；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def default_log_root():
    """默认日志/状态目录。

    **必须和 index.js 的 DEFAULT_LOG_ROOT 是同一个值**，否则会出现
    "两份 state、控制权不同步"（Bug 3：网页点了[交给AI]，工具却还报 human）。

    当前约定：跟着插件走，`<插件目录>/logs`。
    index.js 那边是 `path.join(HERE, 'logs')`，这里对应 `BASE/logs`。
    改一边必须同时改另一边。
    """
    return os.path.join(BASE, "logs")


def _paths():
    """解析运行目录。

    注意：**手动启动网页时没有插件注入的环境变量**，所以不能只靠 env。
    早期版本这里把 log_root 默认成插件目录下的 logs/，而插件进程用的是配置里的路径，
    于是两边各写一份 state 文件——网页点了[交给AI]，serial_status 却还显示 human（Bug 3）。

    现在统一：env > config.json 的 log_root > 与 index.js 完全相同的默认值。
    默认值必须是"同一个表达式"，否则开源版把 log_root 留空后又会分裂。
    """
    common_path = os.path.join(BASE, "config.json")
    common = read_json(common_path, {}) or {}
    protocol_root = (os.environ.get("SERIAL_BRIDGE_PROTO_DIR")
                     or common.get("protocol_root")
                     or os.path.join(BASE, "protocols"))
    log_root = (os.environ.get("SERIAL_BRIDGE_LOG_DIR")
                or common.get("log_root")
                or default_log_root())
    runtime_root = (os.environ.get("SERIAL_BRIDGE_RUNTIME_DIR")
                    or common.get("runtime_root")
                    or os.path.join(log_root, ".runtime"))
    return {
        "protocol_root": protocol_root,
        "common_config": common_path,
        "log_root": log_root,
        "runtime_root": runtime_root,
    }


class Logger(object):
    """兼容旧引用：实现已移到 log.py（daemon 也要用，避免循环 import）。"""

    def __new__(cls, log_root, scene):
        from .log import Logger as _Logger
        return _Logger(log_root, scene)


def _emit(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(data):
    _emit({"ok": True, "data": data})
    return 0


def _fail(error):
    payload = error.to_dict() if isinstance(error, BridgeError) else {
        "kind": "internal", "message": str(error)}
    _emit({"ok": False, "error": payload})
    return 1


def _scene(paths, name):
    return load_scene(paths["common_config"], paths["protocol_root"], name)


def _coerce(text):
    lowered = text.lower()
    if lowered in ("true", "yes", "on", "1"):
        return True
    if lowered in ("false", "no", "off", "0"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text
