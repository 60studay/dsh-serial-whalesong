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

import hashlib
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
    # 给别人留的口子：任意 CSS 变量（形状见下面 EXTRA_* 的约束）。
    # 我们的那几项只是"官方选择题"，想彻底自定义的人用这个 + /theme/custom.css。
    "extra": {},
}

_ALLOWED_PREFIXES = ("http://", "https://", "data:image/", "/theme/image/")
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
    extra = raw.get("extra")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and _VAR_NAME_RE.match(key) and isinstance(value, str):
                data["extra"][key] = value[:EXTRA_VALUE_MAX]
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
        elif key == "side_color":
            merged["side_color"] = _check_accent(value, "side_color")
        else:
            merged["extra"] = _check_extra(value)

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
        raise BridgeError("config", "背景图只接受 http(s)://、data:image/… 或本机已上传的 /theme/image/…。",
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


# ── 本地图片（用户："为什么只能填网址，不能从电脑里选图？"）──────────────────
#
# 浏览器**不允许** http 页面直接加载 `file:///C:/...`（安全模型），所以"填个本地路径"
# 必然失败。能走通的路只有一条：用户选文件 → 浏览器把字节发给本地服务 → 服务存到
# `<runtime_root>/theme/` → 页面从 `/theme/image/<名字>` 读回来。这也是对面原本建议的 C 方案。
#
# 存的位置是运行期目录（`.runtime/` 已 ignore），**不进仓库**；磁盘上只留最近几张，
# 免得用户每换一次图就攒一张。

IMAGE_EXTS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "gif": "gif"}
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_KEEP = 8
_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(?:png|jpg|jpeg|webp|gif)$")

# ── 给"想完全自定义的人"留的接口 ─────────────────────────────
# 官方那几项（背景/透明度/主色/侧栏）只是**选择题**；真要自由度，有两条路：
#   ① extra：一堆 CSS 变量（页面上用 setProperty 设，不拼 CSS 文本，注入不了）
#   ② /theme/custom.css：运行期目录里的任意 CSS，页面最后加载，想怎么改都行
EXTRA_MAX_KEYS = 64
EXTRA_VALUE_MAX = 300
_VAR_NAME_RE = re.compile(r"^--[a-z0-9][a-z0-9-]{0,39}$")

# 公开的变量清单（GET /theme 会带回去，写皮肤的人照着改就行）
TOKENS = [
    "--bg", "--panel", "--fg", "--muted", "--line", "--line-strong",
    "--accent", "--accent-hover", "--human", "--ai", "--paused", "--danger",
    "--btn-face", "--btn-highlight", "--btn-shadow", "--hover-bg",
    "--tag-ai-bg", "--tag-human-bg", "--tag-auto-bg", "--tag-rule-bg",
    "--tag-rule-fg", "--tag-rule-line", "--tag-dev-bg",
    "--pill-human-bg", "--pill-ai-bg", "--pill-paused-bg",
    "--tx-fg", "--input-bg", "--overlay-bg", "--shadow-pop",
    "--panel-alpha", "--side-color", "--side-alpha", "--bg-image", "--bg-alpha",
    "--font-ui", "--font-mono", "--logo",
]


def theme_dir(paths):
    return os.path.join(paths["runtime_root"], "theme")


def save_image(paths, data, ext):
    """存一张图，返回可访问的 url 路径（`/theme/image/<名字>`）。

    `ext` 由调用方从**文件名**取，这里再校验一次；内容不猜格式（够用，且不引入依赖）。
    """
    suffix = IMAGE_EXTS.get(str(ext or "").lower().lstrip("."))
    if not suffix:
        raise BridgeError("config", "只支持 png / jpg / webp / gif 图片。")
    if not data:
        raise BridgeError("config", "图片是空的。")
    if len(data) > IMAGE_MAX_BYTES:
        raise BridgeError("config", "图片太大了（上限 8 MB）。",
                          hint=["先压一下，或者用外链地址"])
    name = "%s.%s" % (hashlib.sha256(data).hexdigest()[:16], suffix)
    directory = theme_dir(paths)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(data)
    _trim_images(directory, keep=IMAGE_KEEP)
    return "/theme/image/" + name


def image_path(paths, name):
    """按名字取本地图片的绝对路径；名字不合法 / 不存在都返回 None。

    名字只认"16 位 hex + 受支持的扩展名"，**不给任何拼路径的机会**（没有 `..`、没有分隔符）。
    """
    if not _NAME_RE.match(str(name or "")):
        return None
    path = os.path.join(theme_dir(paths), name)
    return path if os.path.isfile(path) else None


def _trim_images(directory, keep=IMAGE_KEEP):
    """只留最近 keep 张，别让用户每换一次图就攒一张。"""
    try:
        files = [os.path.join(directory, f) for f in os.listdir(directory)
                 if _NAME_RE.match(f)]
        if len(files) <= keep:
            return
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def _check_extra(value):
    """任意 CSS 变量。**只做形状校验，不猜你想干什么** —— 这本来就是给自由度用的。

    名字必须是 `--kebab-case`（≤40 字符），值 ≤300 字符且不含花括号/换行：
    这些东西是页面上用 `style.setProperty()` 设的，不会拼进 CSS 文本，
    所以注入不了；限制花括号只是防止有人误以为这里能写整段 CSS（那该用 custom.css）。
    """
    if not isinstance(value, dict):
        raise BridgeError("config", "extra 要是个对象（变量名 → 值）。",
                          hint=['例如 {"--accent": "#ff3366"}'])
    if len(value) > EXTRA_MAX_KEYS:
        raise BridgeError("config", "自定义变量最多 %d 个。" % EXTRA_MAX_KEYS)
    out = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        if not _VAR_NAME_RE.match(name):
            raise BridgeError("config", "变量名不合法：%r" % name,
                              hint=["要写成 --kebab-case，例如 --accent / --tag-ai-bg"])
        text = "" if raw is None else str(raw).strip()
        if len(text) > EXTRA_VALUE_MAX:
            raise BridgeError("config", "%s 的值太长了（上限 %d 字符）。" % (name, EXTRA_VALUE_MAX))
        if any(ch in text for ch in "{}\n\r"):
            raise BridgeError("config", "%s 的值里不能有花括号或换行。" % name,
                              hint=["要写整段 CSS 请放到运行期目录的 theme/custom.css"])
        out[name] = text
    return out


def custom_css_path(paths):
    """运行期目录里的 `theme/custom.css`（有就返回路径，没有返回 None）。

    这条是"完全自由"的那条路：页面最后加载它，任何 CSS 都能写。
    """
    path = os.path.join(theme_dir(paths), "custom.css")
    return path if os.path.isfile(path) else None
