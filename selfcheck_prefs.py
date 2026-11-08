# -*- coding: utf-8 -*-
"""UI 偏好（设置页那个「自动打开网页」开关）的自检。不需要硬件。

为什么要单独一个脚本：
    这个开关横跨三处——Python 内核（读写 + 决定弹不弹）、JS 工具层（传哪个 flag）、
    浏览器面板（读写接口）。三处任何一处漏了，症状都是"开关看着有，其实不生效"，
    而纯逻辑自检测不出来。所以这里把**语义表**整张钉死，再加上"单写者"的静态检查。
"""

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def read_text(path):
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def main():
    from serial_bridge import uiprefs
    from serial_bridge.cli import build_parser
    from serial_bridge.errors import BridgeError

    print("=" * 70)
    print("自检：UI 偏好（自动打开网页）")
    print("=" * 70)

    root = tempfile.mkdtemp(prefix="ws-prefs-")
    paths = {"runtime_root": root, "common_config": os.path.join(root, "config.json")}

    try:
        print("\n[1] 没写过 = 关（默认值）")
        check("默认 auto_open_browser=False",
              uiprefs.read_prefs(paths) == {"auto_open_browser": False},
              uiprefs.read_prefs(paths))

        print("\n[2] 写 → 读回")
        saved = uiprefs.write_prefs(paths, {"auto_open_browser": True})
        check("写返回 True", saved.get("auto_open_browser") is True, saved)
        check("读回 True", uiprefs.read_prefs(paths).get("auto_open_browser") is True)
        check("文件落在 runtime_root 下",
              uiprefs.prefs_path(paths).startswith(root), uiprefs.prefs_path(paths))
        check("不是写到 config.json",
              os.path.abspath(uiprefs.prefs_path(paths)) != os.path.abspath(paths["common_config"]))

        print("\n[3] 原子写：落盘是完整 JSON、不留临时文件")
        with io.open(uiprefs.prefs_path(paths), encoding="utf-8") as handle:
            on_disk = json.load(handle)
        check("JSON 可解析且内容对", on_disk == {"auto_open_browser": True}, on_disk)
        leftovers = [n for n in os.listdir(root) if n.endswith(".tmp")]
        check("没有 .tmp 残留", not leftovers, leftovers)

        print("\n[4] 字符串/数字写法也能收敛（设置页可能发字符串）")
        uiprefs.write_prefs(paths, {"auto_open_browser": "false"})
        check("'false' → False", uiprefs.read_prefs(paths)["auto_open_browser"] is False)
        uiprefs.write_prefs(paths, {"auto_open_browser": 1})
        check("1 → True", uiprefs.read_prefs(paths)["auto_open_browser"] is True)

        print("\n[5] 拼错 / 值非法必须报错，不能静默吞")
        try:
            uiprefs.write_prefs(paths, {"auto_open_brower": True})
            check("未知键应报错", False, "没抛异常")
        except BridgeError as exc:
            check("未知键 kind=config", exc.kind == "config", exc.kind)
            check("提示里列出可用键", "auto_open_browser" in " ".join(exc.hint or []), exc.hint)
        try:
            uiprefs.write_prefs(paths, {"auto_open_browser": "maybe"})
            check("非法值应报错", False, "没抛异常")
        except BridgeError as exc:
            check("非法值 kind=config", exc.kind == "config", exc.kind)

        print("\n[6] 文件被手改坏 → 回默认，不崩")
        with io.open(uiprefs.prefs_path(paths), "w", encoding="utf-8") as handle:
            handle.write("{ 半个 json")
        check("坏 JSON → 默认关", uiprefs.read_prefs(paths) == {"auto_open_browser": False})
        with io.open(uiprefs.prefs_path(paths), "w", encoding="utf-8") as handle:
            handle.write("[]")
        check("非对象 → 默认关", uiprefs.read_prefs(paths) == {"auto_open_browser": False})
        os.remove(uiprefs.prefs_path(paths))
        check("文件不存在 → 默认关", uiprefs.read_prefs(paths) == {"auto_open_browser": False})

        print("\n[7] 语义表：显式参数 > 开关 > 默认关")
        uiprefs.write_prefs(paths, {"auto_open_browser": False})
        off = paths
        check("开关关 + 不传 → 不弹", uiprefs.resolve_open_browser(off, None) is False)
        check("开关关 + true → 弹", uiprefs.resolve_open_browser(off, True) is True)
        check("开关关 + false → 不弹", uiprefs.resolve_open_browser(off, False) is False)
        uiprefs.write_prefs(paths, {"auto_open_browser": True})
        check("开关开 + 不传 → 弹", uiprefs.resolve_open_browser(paths, None) is True)
        check("开关开 + true → 弹", uiprefs.resolve_open_browser(paths, True) is True)
        check("开关开 + false → 不弹（显式压过开关）",
              uiprefs.resolve_open_browser(paths, False) is False)

        print("\n[8] 命令行参数：三态，不能把「不传」当成 True")
        parser = build_parser()
        plain = parser.parse_args(["webctl", "--action", "open"])
        check("webctl 不传 flag → None", plain.open_browser is None, plain.open_browser)
        forced = parser.parse_args(["webctl", "--action", "open", "--open-browser"])
        check("--open-browser → True", forced.open_browser is True, forced.open_browser)
        denied = parser.parse_args(["webctl", "--action", "open", "--no-browser"])
        check("--no-browser → False", denied.open_browser is False, denied.open_browser)
        webplain = parser.parse_args(["web", "--scene", "x"])
        check("web 不传 flag → None", webplain.open_browser is None, webplain.open_browser)

        print("\n[9] prefs 子命令")
        got = parser.parse_args(["prefs"])
        check("prefs 无参可解析", got.action_name == "prefs", got.action_name)
        setter = parser.parse_args(["prefs", "--set", "auto_open_browser=true"])
        check("--set 可解析", setter.set == ["auto_open_browser=true"], setter.set)

        print("\n[10] 工具层：false 和「不传」必须是两条路（静态检查）")
        web_js = read_text(os.path.join(HERE, "tools", "web.js"))
        check("有 --no-browser 分支", "'--no-browser'" in web_js)
        check("判断了 === false", "=== false" in web_js)
        check("描述不再写死「省略就不弹」",
              "Omit to just return the URL" not in web_js)

        print("\n[11] 单写者：只有内核的 uiprefs 写这个文件")
        for rel in ("index.js", os.path.join("tools", "web.js"),
                    os.path.join("tools", "serial.js"),
                    os.path.join("tools", "control.js"),
                    os.path.join("tools", "daemon.js"),
                    "client.js"):
            text = read_text(os.path.join(HERE, rel))
            check("%s 不直接写 ui-prefs" % rel,
                  "ui-prefs.json" not in text, "JS 侧不该拼这个文件名")
        writer_files = []
        bridge_dir = os.path.join(HERE, "serial_bridge")
        for name in sorted(os.listdir(bridge_dir)):
            if not name.endswith(".py"):
                continue
            text = read_text(os.path.join(bridge_dir, name))
            if "ui-prefs.json" in text:
                writer_files.append(name)
        check("Python 侧只有 uiprefs.py 认这个文件名",
              writer_files == ["uiprefs.py"], writer_files)

        print("\n[12] 设置页端点与客户端挂点存在（静态检查）")
        api_js = read_text(os.path.join(HERE, "helpers", "prefs_api.js"))
        # 去掉行注释再查：注释里提到别这么写不算"真这么写"。
        api_code = "\n".join(line.split("//")[0] for line in api_js.splitlines())
        check("端点路径对", "/api/whalesong" in api_code, "缺 /api/whalesong")
        check("只注册一次（防重复注册）", "claimed" in api_code)
        # 直接读 ctx.webServer 会让 cordis 报 "cannot get property without inject"，
        # 插件整个起不来（今天实测踩过），必须走 ctx.inject。
        check("用 ctx.inject 等服务就绪", "ctx.inject(" in api_code, "缺 ctx.inject")
        check("不直接读 ctx.webServer",
              "ctx.webServer" not in api_code and "ctx?.webServer" not in api_code,
              "会触发 cordis 依赖检查")
        client_js = read_text(os.path.join(HERE, "client.js"))
        check("挂 plugins.bundle.config", "plugins.bundle.config" in client_js)
        check("key = 包名", "'dsh-whalesong'" in client_js)
        # 注释里提一句不算"做了引导"，真去看有没有注册这个槽
        check("不做首次引导",
              "slots.inject('plugins.bundle.activation'" not in client_js
              and "name: 'plugins.bundle.activation'" not in client_js)
        pkg = json.loads(read_text(os.path.join(HERE, "package.json")))
        check("package.json 有 dsh.client", "client" in (pkg.get("dsh") or {}),
              pkg.get("dsh"))
        check("package.json 导出 ./client",
              "./client" in (pkg.get("exports") or {}), pkg.get("exports"))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
