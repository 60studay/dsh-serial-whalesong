# -*- coding: utf-8 -*-
"""webctl 与默认场景解析的自检（不需要硬件）。

为什么单独写这个：4.8 交付时我漏测了"不传 scene"这条路径，
而它恰好是 AI 最自然的用法（`serial_web({"action":"open"})`）。
上线后发现 `_default_scene()` 把 `list_scenes()` 的参数传少了，直接 TypeError。

这个脚本就是那个洞的补丁：把"不传 scene"和"多场景"两条路都钉死。
"""

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


def _read(path):
    import io
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def main():
    from serial_bridge.cli import _default_scene, _paths
    from serial_bridge.errors import BridgeError

    paths = _paths()
    real_scenes = [d for d in os.listdir(paths["protocol_root"])
                   if os.path.isdir(os.path.join(paths["protocol_root"], d))]
    print("using protocol_root=%s（%d 个场景）" % (paths["protocol_root"], len(real_scenes)))

    print("\n[1] 真实环境：多场景时必须给出可操作的错误，而不是崩")
    try:
        picked = _default_scene(paths)
        check("多场景应拒绝自动选择", len(real_scenes) <= 1,
              "居然选了 %r（场景数 %d）" % (picked, len(real_scenes)))
    except BridgeError as exc:
        check("kind=config", exc.kind == "config", exc.kind)
        joined = " ".join(exc.hint or [])
        check("提示里列出可用场景", all(name in joined for name in real_scenes), exc.hint)
        check("不是 internal 崩", exc.kind != "internal", exc.kind)

    root = tempfile.mkdtemp(prefix="webctl-")
    try:
        print("\n[2] 只有一个场景时，默认就该选它")
        single = os.path.join(root, "single", "protocols")
        os.makedirs(os.path.join(single, "唯一场景"))
        with open(os.path.join(root, "single", "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        fake_paths = {
            "protocol_root": single,
            "common_config": os.path.join(root, "single", "config.json"),
        }
        check("单场景自动选中", _default_scene(fake_paths) == "唯一场景",
              _default_scene(fake_paths))

        print("\n[3] 一个场景都没有时，报 scene_missing")
        empty = os.path.join(root, "empty", "protocols")
        os.makedirs(empty)
        with open(os.path.join(root, "empty", "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        try:
            _default_scene({"protocol_root": empty,
                            "common_config": os.path.join(root, "empty", "config.json")})
            check("空目录应报错", False, "没抛异常")
        except BridgeError as exc:
            check("kind=scene_missing", exc.kind == "scene_missing", exc.kind)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n[4] webctl 子命令存在且参数齐全")
    from serial_bridge.cli import build_parser
    parser = build_parser()
    try:
        args = parser.parse_args(["webctl", "--action", "status"])
        check("可以省略 --scene", args.scene == "", args.scene)
        check("action 默认值可用", args.action == "status", args.action)
        check("mode 默认 light", args.mode == "light", args.mode)
        # 弹不弹浏览器是三态：不传 = 听设置页的开关（默认关），不能默认成 True。
        # 这条是 openBrowser bug 的回归锁：以前 default=True，false 和"不传"都拦不住。
        check("不传 flag 时 open_browser 是 None", args.open_browser is None,
              args.open_browser)
        check("--open-browser 是显式 True",
              parser.parse_args(["webctl", "--open-browser"]).open_browser is True)
        check("--no-browser 是显式 False",
              parser.parse_args(["webctl", "--no-browser"]).open_browser is False)
        check("--action reap 可解析",
              parser.parse_args(["webctl", "--action", "reap"]).action == "reap")
    except SystemExit:
        check("webctl 参数可解析", False, "argparse 退出")

    print("\n[5] 收尸：死实例文件删掉，活实例留着")
    import json
    from serial_bridge import web_service
    root = tempfile.mkdtemp(prefix="reap-")
    try:
        fake = {"runtime_root": root}
        os.makedirs(os.path.join(root, "state"))
        dead_path = web_service.instance_path(fake, "死场景")
        with open(dead_path, "w", encoding="utf-8") as fh:
            json.dump({"url": "http://127.0.0.1:9", "pid": 999999, "port": 9}, fh)

        # ① 不桩：端口 9 连不上，必须判死并删文件
        result = web_service.reap_dead(fake)
        check("死实例文件被删", not os.path.exists(dead_path), "文件还在")
        check("reaped 报数对", result["reaped"] == ["死场景"], result.get("reaped"))

        # ② 桩一个"活着"的：必须留着，别误删
        alive_path = web_service.instance_path(fake, "活场景")
        with open(alive_path, "w", encoding="utf-8") as fh:
            json.dump({"url": "http://127.0.0.1:8898", "pid": 1, "port": 8898}, fh)
        orig = web_service._instance_alive
        web_service._instance_alive = lambda info, timeout=1.0: \
            str((info or {}).get("url") or "").endswith("8898")
        try:
            result = web_service.reap_dead(fake)
        finally:
            web_service._instance_alive = orig
        check("活实例文件留着", os.path.exists(alive_path), "被误删了")
        check("kept 报数对", result["kept"] == ["活场景"], result.get("kept"))
        check("这次没收任何东西", result["reaped"] == [], result.get("reaped"))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 1.19 之后这一整块从 app.js 搬到了 js/viewer.js：
    # 静态检查跟着搬家，否则"代码还在、自检找不到"会变成假失败。
    print("\n[6] 失效标签自救（静态检查 web/js/viewer.js）")
    app_js = _read(os.path.join(HERE, "web", "app.js"))
    viewer_js = _read(os.path.join(HERE, "web", "js", "viewer.js"))
    # 标题的规矩在 app.js（setPageTitle），自救流程在 viewer.js
    check("标题加 [已失效] 前缀", "[已失效]" in app_js)
    check("有「关闭本页」按钮", "关闭本页" in viewer_js)
    check("试 window.close()", "window.close()" in viewer_js)
    check("关不掉时说 Ctrl+W", "Ctrl+W" in viewer_js)
    check("连续 3 次失败才判死", "FAILS_TO_DIE = 3" in viewer_js)
    check("探测端口含 8898 和 8907", "8898" in viewer_js and "8907" in viewer_js)
    check("探测超时 300ms", "}, 300)" in viewer_js)
    check("探测用 no-cors", "mode: 'no-cors'" in viewer_js)
    check("判死后停止心跳", "clearInterval(timer)" in viewer_js)
    check("跳过 8899（DSH 自己的口）",
          "8899" not in viewer_js.split("PROBE_PORTS = ")[1][:80])

    print("\n[7] maybeAutoOpen 漏传参数不抛（P2 回归）")
    auto_js = _read(os.path.join(HERE, "helpers", "autoopen.js"))
    check("形参有默认值 = {}", "runtimeRoot } = {})" in auto_js)

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
