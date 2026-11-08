# -*- coding: utf-8 -*-
"""外观（2.2）：背景图 / 背景不透明度 / 主色 —— **运行期状态，不进 config、不进仓库**。

为什么单独一个文件而不是塞进 `uiprefs.py`
--------------------------------------------
`uiprefs` 那边是"只有 bool"的开关（自动打开网页），读的时候容错、写的时候认不出就报错。
外观这三项是字符串/浮点，校验规则完全不同（URL 要防 `javascript:`、透明度要 0~1、主色要 hex）。
硬塞进去会让那个文件变成"一半 bool 一半随便"的东西，以后谁都不敢动。
所以：**开关归 uiprefs，外观归这里**，都写 `<runtime_root>/*.json`（`.runtime/` 已 ignore）。

三条硬约束（用户 17:0x 定的，写在 CSS 注释里也有）：
  ① 背景图只挂在**专用背景层**（`body::before`），z-index 低于页头 —— 不许直接写 body
  ② 页头品牌区自带不透明背景，背景图/透明度**不作用于它**（logo 必须清楚）
  ③ 背景图 `pointer-events: none`，不许挡住点击

安全边界：`bg_image` 只接受 http(s) 与 data:image，**别的一律拒绝**。
页面会把它的值塞进 `background-image`，`javascript:` 之类必须在这儿就挡掉。
"""

import json
import os
import re

from .errors import BridgeError

THEME_NAME = "ui-theme.json"

DEFAULTS = {
    "bg_image": "",      # 空 = 不铺背景图
    "bg_alpha": 1.0,     # 背景图的不透明度（只影响背景层）
    "accent": "",        # 空 = 用主题自带主色
    # 用户 18:5x：收发区/状态轨道/输入框"和背景不搭"，希望能调透明度
    "panel_alpha": 1.0,  # 面板不透明度（收发区 / 状态轨道 / 输入框）
    # 侧栏想单独改底色、单独铺图
    "side_image": "",    # 空 = 侧栏不铺图
    "side_color": "",    # 空 = 用主题底色
    "side_alpha": 1.0,   # 侧栏不透明度
}

_ALLOWED_PREFIXES = ("http://", "https://", "data:image/")
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def theme_path(paths):
    return os.path.join(paths["runtime_root"], THEME_NAME)


def read_theme(paths):
    """读外观设置。文件没有 / 坏了 / 不是对象 → 回默认值，不抛异常。

    理由和 uiprefs 一样：这是"长什么样"的设置，读失败不该让网页起不来。
    """
    data = dict(DEFAULTS)
    try:
        with open(theme_path(paths), "r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except (IOError, OSError, ValueError):
        return data
    if not isinstance(raw, dict):
        return data
    if isinstance(raw.get("bg_image"), str):
        data["bg_image"] = raw["bg_image"]
    try:
        data["bg_alpha"] = max(0.0, min(1.0, float(raw.get("bg_alpha", 1.0))))
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("accent"), str) and _HEX_RE.match(raw["accent"].strip()):
        data["accent"] = raw["accent"].strip()
    for key in ("panel_alpha", "side_alpha"):
        try:
            data[key] = max(0.0, min(1.0, float(raw.get(key, 1.0))))
        except (TypeError, ValueError):
            pass
    if isinstance(raw.get("side_image"), str):
        data["side_image"] = raw["side_image"]
    if isinstance(raw.get("side_color"), str) and _HEX_RE.match(raw["side_color"].strip()):
        data["side_color"] = raw["side_color"].strip()
    return data


def write_theme(paths, patch):
    """合并并原子落盘。认不出来的值**报错**，不静默吞（用户会以为设好了）。"""
    if not isinstance(patch, dict) or not patch:
        raise BridgeError("config", "没给要改的字段。",
                          hint=["例如 --set bg_alpha=0.5"])
    unknown = [key for key in patch if key not in DEFAULTS]
    if unknown:
        raise BridgeError("config",
                          "不认识的外观项：%s" % "、".join(sorted(unknown)),
                          hint=["可用的键：%s" % "、".join(sorted(DEFAULTS))])

    merged = read_theme(paths)
    for key, value in patch.items():
        if key in ("bg_image", "side_image"):
            merged[key] = _check_bg_image(value, key)
        elif key in ("bg_alpha", "panel_alpha", "side_alpha"):
            merged[key] = _check_alpha(value, key)
        elif key == "accent":
            merged["accent"] = _check_accent(value)
        else:
            merged["side_color"] = _check_accent(value, "side_color")

    path = theme_path(paths)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return merged


def _check_bg_image(value, key="bg_image"):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 4000:
        raise BridgeError("config", "背景图地址太长了（>4000 字符）。",
                          hint=["用 http(s) 链接，别把整张图塞成 data URI"])
    if not text.lower().startswith(_ALLOWED_PREFIXES):
        raise BridgeError("config", "背景图地址只接受 http(s):// 或 data:image/…。",
                          hint=["例如 https://example.com/bg.jpg"])
    return text


def _check_alpha(value, key="bg_alpha"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BridgeError("config", "%s 要 0~1 之间的数字，收到：%r" % (key, value))
    if number > 1:            # 顺手接受百分数：用户填 60 就是 0.6
        number = number / 100.0
    if not (0.0 <= number <= 1.0):
        raise BridgeError("config", "%s 要 0~1 之间（或 0~100 的百分数）。" % key)
    return round(number, 3)


def _check_accent(value, key="accent"):
    text = str(value or "").strip()
    if not text:
        return ""
    if not _HEX_RE.match(text):
        raise BridgeError("config", "%s 要写成 #rgb 或 #rrggbb，收到：%r" % (key, value))
    return text
