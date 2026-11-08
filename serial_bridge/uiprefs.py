# -*- coding: utf-8 -*-
"""UI 偏好：设置页里那些"用户自己选"的开关（目前只有一个：自动打开网页）。

三条规矩，改之前先看懂：

① **单写者**。只有设置页的 POST（`prefs --set`）会写这个文件；工具层、网页、CLI
   一律只读。以前 state 文件两边各写一份，吃完 Bug 3 的亏，这里从第一天就定死。

② **原子写**。临时文件 + os.replace()。Python 侧任何时刻读到的要么是旧值、
   要么是新值，不会读到半个 JSON。

③ **别写进 config.json**。那是 git 跟踪的文件，每动一次开关就弄脏工作区，
   而两个 AI 共用一个仓库。放 `<runtime_root>/ui-prefs.json`（`.runtime/` 已 ignore）。

位置由 `cli._paths()` 的 runtime_root 决定：env > config.json > `<log_root>/.runtime`。
"""

import json
import os

from .errors import BridgeError

PREFS_NAME = "ui-prefs.json"

# 未知键直接报错，不静默吞掉：拼错了要立刻看得见（bili-watch 吃过静默丢 patch 的亏）。
DEFAULTS = {"auto_open_browser": False}
_BOOL_KEYS = frozenset(["auto_open_browser"])


def prefs_path(paths):
    """偏好文件绝对路径。目录不存在也没关系，写的时候会建。"""
    return os.path.join(paths["runtime_root"], PREFS_NAME)


def read_prefs(paths):
    """读偏好。文件没写过 / 坏了 / 不是对象，一律回默认值，不抛异常。

    为什么要容错：这是 UI 的开关，读失败不该让整个 `serial_web` 报错——
    退化成"默认关"总比"网页起不来"好。
    """
    data = dict(DEFAULTS)
    path = prefs_path(paths)
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:
            raw = json.load(fp)
    except (IOError, OSError):
        return data
    except ValueError:
        # 半个文件 / 手改坏了：忽略内容，回默认
        return data
    if not isinstance(raw, dict):
        return data
    for key in DEFAULTS:
        if key in raw:
            data[key] = _as_bool(raw[key], DEFAULTS[key])
    return data


def write_prefs(paths, patch):
    """把 patch 合并进偏好文件并原子落盘，返回合并后的完整值。

    只接受白名单键，且值必须是 bool（或 "true"/"false"/1/0）。别的键直接抛
    `config` 错误——让设置页能把原因显示出来，而不是存了个没人认识的字段。
    """
    if not isinstance(patch, dict) or not patch:
        raise BridgeError("config", "没给要改的字段。",
                          hint=["用 --set auto_open_browser=true 这种形式"])
    unknown = [key for key in patch if key not in DEFAULTS]
    if unknown:
        raise BridgeError(
            "config",
            "不认识的偏好项：%s" % "、".join(sorted(unknown)),
            hint=["可用的键：%s" % "、".join(sorted(DEFAULTS))],
        )

    merged = read_prefs(paths)
    for key, value in patch.items():
        parsed = _coerce_bool(value)
        if parsed is None:
            # 写的时候不猜：认不出来的值直接报错，别让用户以为"设好了"
            raise BridgeError(
                "config",
                "%s 只接受 true / false，收到：%r" % (key, value),
                hint=["例如 --set %s=true" % key],
            )
        merged[key] = parsed

    path = prefs_path(paths)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(merged, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)
    return merged


def _as_bool(value, fallback):
    """读文件时用：认不出来就用 fallback，不猜（文件可能被手改坏）。"""
    parsed = _coerce_bool(value)
    return bool(fallback) if parsed is None else parsed


def _coerce_bool(value):
    """把值收敛成 bool；认不出来返回 None（写的时候要报错，不能静默吞）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    return None
