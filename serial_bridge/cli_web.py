# -*- coding: utf-8 -*-
"""网页相关子命令：web / webctl / prefs。

为什么单独一个文件（1.19 拆 cli.py）
------------------------------------
这一组是"**关于网页**"的命令，逻辑上不碰串口：web 起前台服务、webctl 遥控
后台实例、prefs 读写 UI 偏好。它们也是最好测的一组（`selfcheck_webctl.py`
直接对着它们跑），所以先拆这组。

注意 `webctl` 的默认值是"**不弹浏览器**"：不传 flag = 用户没说 = 别打扰。
弹窗只有三个来源（见 `cmd_webctl` 里的注释），改这里之前先读那段。
"""

from .cli_base import _coerce, _ok
from .config import list_scenes
from .errors import BridgeError
from . import uiprefs


def _default_scene(paths):
    """没指定场景时：只有一个场景就用它，多个就报错让调用方明确。"""
    scenes = [s["name"] for s in list_scenes(paths["common_config"], paths["protocol_root"])]
    if len(scenes) == 1:
        return scenes[0]
    if not scenes:
        raise BridgeError("scene_missing", "一个场景都没有。",
                          hint=["在 protocols/ 下建一个场景目录"])
    raise BridgeError("config", "有多个场景，必须指定 scene。",
                      hint=["可用场景：%s" % "、".join(scenes),
                            "也可以先调 serial_list 看每个场景现在会落到哪个口"])


def cmd_webctl(paths, args):
    """网页服务遥控：open / status / close / reap（给 AI 的 serial_web 工具用）。"""
    from . import web_service
    action = args.action

    # 收尸：不起服务、也不需要场景。用户没法一条命令清干净死实例，所以单独给个动作。
    if action == "reap":
        return _ok(web_service.reap_dead(paths))

    # 其余动作先顺手扫一遍：死掉的实例文件留着会让人以为"还在跑"。
    # 没有实例文件时这一步是空转（只是 listdir），不给调用方增加可见延迟。
    web_service.reap_dead(paths)

    scene = (args.scene or "").strip() or _default_scene(paths)
    if action == "open":
        # 不传 flag = 不弹（"用户没说就别打扰"）。弹窗只有三个来源：
        #   ① 用户/AI 显式要（--open-browser；页面已在跑再加 --force-browser）；
        #   ② 开关=开 时由 send/read/control 触发的自动路径（--open-browser --reopen-if-idle）；
        #   ③ ②的补充：页面在跑但窗口被关了，--reopen-if-idle 会再弹一次。
        # 以前这里写死 True，把"用户没说"翻译成"要弹"，复用路径还无条件重弹，
        # 于是每看一眼网页就弹一个窗口。
        return _ok(web_service.open_web(
            paths, scene,
            port=int(args.port or 0),
            mode=args.mode,
            open_browser=bool(getattr(args, "open_browser", False)),
            force_window=bool(getattr(args, "force_window", False)),
            reopen_if_idle=bool(getattr(args, "reopen_if_idle", False))))
    if action == "close":
        return _ok(web_service.close_web(paths, scene))
    return _ok(web_service.status_web(paths, scene))


def cmd_web(paths, args):
    from .supervisor import run_web
    return run_web(paths, args.scene, args.port, args.mode,
                   bool(getattr(args, "open_browser", False)))


def cmd_prefs(paths, args):
    """读/写 UI 偏好（设置页用）。

    不带 --set = 读；带 --set 键=值 = 写（**唯一写入口**，见 uiprefs 的注释）。
    返回完整的当前值，设置页拿它直接回显，不用再读一次。
    """
    sets = getattr(args, "set", None) or []
    if not sets:
        return _ok(uiprefs.read_prefs(paths))
    patch = {}
    for item in sets:
        if "=" not in item:
            raise BridgeError("config", "--set 要写成 键=值，收到：%s" % item,
                              hint=["例如 --set auto_open_browser=true"])
        key, value = item.split("=", 1)
        patch[key.strip()] = _coerce(value.strip())
    return _ok(uiprefs.write_prefs(paths, patch))


def add_parsers(sub):
    """把 web / webctl / prefs 的定义挂到子命令表上。"""
    web = sub.add_parser("web")
    web.add_argument("--scene", required=True)
    web.add_argument("--port", type=int, default=0)
    web.add_argument("--mode", default="light", choices=["light", "watch"])
    web.add_argument("--open-browser", dest="open_browser", action="store_true", default=None,
                     help="强制打开浏览器（压过设置页的开关）")
    web.add_argument("--no-browser", dest="open_browser", action="store_false",
                     help="强制不打开浏览器（压过设置页的开关）")

    # 网页遥控：ai 工具 serial_web 用的。web 是前台阻塞，webctl 是"起完就返回"
    webctl = sub.add_parser("webctl")
    webctl.add_argument("--action", default="status",
                        choices=["open", "status", "close", "reap"])
    webctl.add_argument("--scene", default="", help="留空 = 唯一的场景")
    webctl.add_argument("--port", type=int, default=0, help="0 = 自动挑（8898 起）")
    webctl.add_argument("--mode", default="light", choices=["light", "watch"])
    webctl.add_argument("--open-browser", dest="open_browser", action="store_true",
                        default=None,
                        help="这次起页面时顺手打开浏览器（只在'从没有到有'时生效）")
    webctl.add_argument("--no-browser", dest="open_browser", action="store_false",
                        help="明确不打开浏览器（默认就是这条）")
    webctl.add_argument("--force-browser", dest="force_window", action="store_true",
                        help="页面已经在跑也弹一次——只有用户亲口说要才传")
    webctl.add_argument("--reopen-if-idle", dest="reopen_if_idle", action="store_true",
                        help="页面在跑但浏览器窗口已关（看客心跳断了）→ 再弹一次；给自动路径用")

    # UI 偏好：设置页读写。不带 --set 就是读。
    prefs = sub.add_parser("prefs")
    prefs.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="写一个偏好项；不带就是读")
