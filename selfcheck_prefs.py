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

        print("\n[7] 复用路径：页面在跑时弹不弹，看窗口还在不在（桩测试）")
        from serial_bridge import web_service
        calls = []
        marked = []
        idle = {"value": False}
        orig_read, orig_open = web_service.read_instance, web_service._open_browser
        orig_idle = web_service._viewer_idle
        orig_mark = web_service._mark_viewer_seen
        web_service.read_instance = lambda p, s: {
            "url": "http://127.0.0.1:8899/", "port": 8899, "pid": 1,
            "status": {"mode": "light"},
        }
        # 注意签名：open_web 现在会带上 paths 调它（60 秒去重要用）
        web_service._open_browser = lambda url, paths=None: calls.append(url)
        web_service._viewer_idle = lambda url, timeout=1.0: idle["value"]
        web_service._mark_viewer_seen = lambda url, timeout=1.0: marked.append(url)
        try:
            # 自动路径（开关=开 由 send 触发），窗口还开着：不许弹
            web_service.open_web(paths, "s", open_browser=True, force_window=False,
                                 reopen_if_idle=True)
            check("窗口还开着 → 不弹", calls == [], calls)
            # 用户把窗口关了：下次用串口要把页面送回来
            idle["value"] = True
            web_service.open_web(paths, "s", open_browser=True, force_window=False,
                                 reopen_if_idle=True)
            check("窗口关了 → 弹一次", len(calls) == 1, calls)
            check("弹完先记一笔（防连弹）", len(marked) == 1, marked)
            # 不带 reopen_if_idle 的老路径：即使窗口关了也不弹
            calls.clear()
            web_service.open_web(paths, "s", open_browser=True, force_window=False)
            check("不传 reopen_if_idle → 不弹", calls == [], calls)
            # 用户亲口说要：页面在跑也弹一次
            web_service.open_web(paths, "s", open_browser=True, force_window=True)
            check("显式 force 弹一次", len(calls) == 1, calls)
            # 再说一次"不要"：仍然不新增
            web_service.open_web(paths, "s", open_browser=False, force_window=False)
            check("不要就不弹", len(calls) == 1, calls)
        finally:
            web_service.read_instance = orig_read
            web_service._open_browser = orig_open
            web_service._viewer_idle = orig_idle
            web_service._mark_viewer_seen = orig_mark

        print("\n[8] 命令行参数：「不传」= 不弹，不能默认成 True")
        parser = build_parser()
        plain = parser.parse_args(["webctl", "--action", "open"])
        check("webctl 不传 flag → None", plain.open_browser is None, plain.open_browser)
        check("webctl 不传 force → False", plain.force_window is False, plain.force_window)
        forced = parser.parse_args(["webctl", "--action", "open", "--open-browser"])
        check("--open-browser → True", forced.open_browser is True, forced.open_browser)
        denied = parser.parse_args(["webctl", "--action", "open", "--no-browser"])
        check("--no-browser → False", denied.open_browser is False, denied.open_browser)
        forc = parser.parse_args(["webctl", "--action", "open", "--force-browser"])
        check("--force-browser → True", forc.force_window is True, forc.force_window)
        reo = parser.parse_args(["webctl", "--action", "open", "--reopen-if-idle"])
        check("--reopen-if-idle → True", reo.reopen_if_idle is True, reo.reopen_if_idle)
        webplain = parser.parse_args(["web", "--scene", "x"])
        check("web 不传 flag → None", webplain.open_browser is None, webplain.open_browser)

        print("\n[9] 自动开页面：触发范围与三条硬规矩（静态检查）")
        auto_js = read_text(os.path.join(HERE, "helpers", "autoopen.js"))
        import re as _re
        # 注释里提到"别这么写"不算真这么写：块注释和行注释都先剥掉。
        auto_code = _re.sub(r"/\*.*?\*/", "", auto_js, flags=_re.S)
        auto_code = "\n".join(line.split("//")[0] for line in auto_code.splitlines())
        m = _re.search(r"AUTO_OPEN_SUBS\s*=\s*\[([^\]]*)\]", auto_code)
        subs = _re.findall(r"'([a-z_]+)'", m.group(1)) if m else []
        check("触发范围 = send / read / control", sorted(subs) == ["control", "read", "send"], subs)
        for quiet in ("list", "info", "status", "watch", "daemon", "webctl", "prefs"):
            check("不因 %s 触发" % quiet, quiet not in subs, subs)
        check("detached 子进程（不阻塞主收发）", "detached: true" in auto_code)
        check("输出丢掉（不读、不解析）", "stdio: 'ignore'" in auto_code)
        check("绝不带 --force-browser（第二次不重弹）",
              "--force-browser" not in auto_code, "自动路径不许无条件弹")
        check("带 --reopen-if-idle（窗口关了要送回来）",
              "--reopen-if-idle" in auto_code, "缺 reopen 语义")
        check("取不到 scene 就不触发", "pickScene" in auto_code)
        check("读的是 ui-prefs.json", "ui-prefs.json" in auto_code)
        check("runCli 里挂了钩子",
              "maybeAutoOpen(" in read_text(os.path.join(HERE, "index.js")))

        print("\n[9] prefs 子命令")
        got = parser.parse_args(["prefs"])
        check("prefs 无参可解析", got.action_name == "prefs", got.action_name)
        setter = parser.parse_args(["prefs", "--set", "auto_open_browser=true"])
        check("--set 可解析", setter.set == ["auto_open_browser=true"], setter.set)

        print("\n[10] 工具层：显式要 → 无条件弹；否则一律不弹（静态检查）")
        web_js = read_text(os.path.join(HERE, "tools", "web.js"))
        check("显式 true 传 --open-browser", "'--open-browser'" in web_js)
        check("显式 true 同时传 --force-browser", "'--force-browser'" in web_js)
        check("其余情况一律 --no-browser",
              "else argv.push('--no-browser')" in web_js)
        check("描述写清「用户亲口说要才传 true」",
              "explicitly asked" in web_js)

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
        # 重载会重新求值模块：注册记录必须放 globalThis，否则新旧两份代码
        # 各自看到"没注册"，旧 effect 的清理会把刚注册的路由注销掉（实测 404）。
        check("注册记录放在 globalThis", "globalThis[REGISTRY_KEY]" in api_code or
              "globalThis[REGISTRY_KEY] =" in api_code)
        check("先收旧注册再注册新的", "reg.live" in api_code)
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
