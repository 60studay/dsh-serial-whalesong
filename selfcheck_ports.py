# -*- coding: utf-8 -*-
"""串口解析 / 自动选择 的自测（不需要硬件）。

针对一个用户真踩到的坑（2026-11-07 14:15）：

    拔插 USB 后 Windows 把 CH340 从 COM7 换成了 COM10，
    点「AI 自动选择」→ 400「COM7 打不开」，候选里明明列着 COM10 却不去选。

根因是 `len(ports) == 1` 这个条件：用户机器上有 7 个口（6 个蓝牙虚拟口），
所以永远不成立。这个自检锁住四件事：

  1. 蓝牙虚拟口必须被排除在"候选"之外（它们没有 VID:PID）
  2. 只有一个真实 USB 口时，配置写死的老号要能自动纠正
  3. `guess_port` 真的会猜（vid_pid / 序列号 / 描述 / 只剩一个）
  4. 同型号两块板子（都是 CH340）必须能用 `match_serial` 分开

运行：python selfcheck_ports.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge import ports as ports_mod
from serial_bridge.errors import BridgeError
from serial_bridge.ports import guess_port, real_ports, resolve_port

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


BT = [
    {"device": "COM3", "description": "蓝牙链接上的标准串行 (COM3)", "vid_pid": "", "serial_number": ""},
    {"device": "COM4", "description": "蓝牙链接上的标准串行 (COM4)", "vid_pid": "", "serial_number": ""},
    {"device": "COM5", "description": "蓝牙链接上的标准串行 (COM5)", "vid_pid": "", "serial_number": ""},
]
CH340_A = {"device": "COM10", "description": "USB-SERIAL CH340 (COM10)",
           "vid_pid": "1A86:7523", "serial_number": "SN-A"}
CH340_B = {"device": "COM11", "description": "USB-SERIAL CH340 (COM11)",
           "vid_pid": "1A86:7523", "serial_number": "SN-B"}


def use(ports):
    """把 scan_ports 换成假数据（纯逻辑自检，不碰真硬件）。"""
    ports_mod.scan_ports = lambda: list(ports)


def check_override_persistence():
    """1.18：页面上选的串口是"这次的选择"，要存 state、活过重启，且**不写进 config.json**。

    为什么单独拎一个函数：它要起一个真的 WebServer（只是不 serve），
    和上面那些纯函数检查不是一回事。放在临时目录里跑，不碰用户的真实状态文件。
    """
    import json
    import shutil
    import tempfile

    from serial_bridge.supervisor import WebServer

    root = tempfile.mkdtemp(prefix="port18-")
    try:
        proto = os.path.join(root, "protocols")
        os.makedirs(os.path.join(proto, "s"))
        scene_cfg = os.path.join(proto, "s", "config.json")
        # 配置里**只放匹配规则**，不写死口号 —— 这正是 1.18 想要的样子
        with open(scene_cfg, "w", encoding="utf-8") as handle:
            handle.write('{"match_vid_pid":"1A86:7523","baud":115200,"newline":"\\\\r\\\\n"}')
        with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as handle:
            handle.write('{"log_root":"%s"}' % os.path.join(root, "logs").replace("\\", "\\\\"))
        cfg_before = open(scene_cfg, encoding="utf-8").read()

        paths = {
            "protocol_root": proto,
            "common_config": os.path.join(root, "config.json"),
            "log_root": os.path.join(root, "logs"),
            "runtime_root": os.path.join(root, "logs", ".runtime"),
        }
        use(BT + [CH340_A, CH340_B])

        srv = WebServer(paths, "s", port=8896, mode="light")
        srv.reconnect("COM11", 9600)          # 用户在页面上选了第二块板子
        state_file = os.path.join(paths["runtime_root"], "state", "s.json")
        saved = json.load(open(state_file, encoding="utf-8"))
        check("选的口落到 state 文件（override_com）",
              saved.get("override_com") == "COM11", saved.get("override_com"))
        check("波特率也一起存", int(saved.get("override_baud") or 0) == 9600,
              saved.get("override_baud"))
        check("effective_cfg 认这个覆盖（定时/规则也跟着走）",
              srv.effective_cfg().get("com") == "COM11", srv.effective_cfg().get("com"))

        # 「重启」= 同一份 state 重新 new 一个 WebServer（真的是这个路径：
        # 进程重启后 __init__ 里就是把 state 读回来）
        srv2 = WebServer(paths, "s", port=8896, mode="light")
        check("重启后仍是 COM11", srv2.override_com == "COM11", srv2.override_com)
        check("重启后 effective_cfg 也是 COM11",
              srv2.effective_cfg().get("com") == "COM11", srv2.effective_cfg().get("com"))
        try:
            got = srv2.current_device()
            check("重启后能直接解析到 COM11", got.get("device") == "COM11", got)
        except BridgeError as exc:
            check("重启后能直接解析到 COM11", False, exc.kind)

        check("config.json 一个字没动", open(scene_cfg, encoding="utf-8").read() == cfg_before)

        # 拔掉一块板子 → 回到自动识别 → 按配置规则应该自己找到剩下那块
        use(BT + [CH340_A])
        result = srv2.auto_port()
        check("回到自动识别返回 ok", result.get("ok") is True, result)
        check("清掉的是 COM11", result.get("cleared") == "COM11", result.get("cleared"))
        check("清完按规则重新找到 COM10", result.get("port") == "COM10", result)
        saved2 = json.load(open(state_file, encoding="utf-8"))
        check("state 文件里的 override 也清了",
              not saved2.get("override_com"), saved2.get("override_com"))
        check("当前配置又不带口号了", "com" not in srv2.effective_cfg(),
              srv2.effective_cfg())
        check("config.json 依然一个字没动", open(scene_cfg, encoding="utf-8").read() == cfg_before)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    original = ports_mod.scan_ports
    try:
        print("\n[1] 蓝牙虚拟口不算候选")
        real = real_ports(BT + [CH340_A])
        check("排除蓝牙口后只剩 1 个", len(real) == 1 and real[0]["device"] == "COM10", real)
        check("没有 VID:PID 的一律不算", all(p.get("vid_pid") for p in real))

        print("\n[2] 配置写死的老号不在了 → 自动切到唯一的真实 USB 口")
        use(BT + [CH340_A])
        got = resolve_port({"com": "COM7"})
        check("resolve_port 不再抛错", got.get("device") == "COM10", got)
        check("source 标成 only_usb_port", got.get("source") == "only_usb_port", got)
        check("note 说清了为什么换", "COM7" in got.get("note", ""), got.get("note"))

        print("\n[3] 多个真实 USB 口 + 老号不在 → 仍然要报错（不能瞎猜）")
        use(BT + [CH340_A, CH340_B])
        try:
            resolve_port({"com": "COM7"})
            check("多口歧义要报错", False, "没有报错")
        except BridgeError as exc:
            check("多口歧义要报错", exc.kind == "no_device", exc.kind)

        print("\n[4] guess_port：真的会猜")
        use(BT + [CH340_A])
        guess = guess_port({})
        check("只有一个真实口就选它", guess and guess["device"] == "COM10", guess)
        use(BT + [CH340_A, CH340_B])
        guess = guess_port({"match_vid_pid": "1A86:7523"})
        check("同型号两块 → 猜不出来（返回 None，交给上层报歧义）", guess is None, guess)
        guess = guess_port({"match_serial": "SN-B"})
        check("按序列号能唯一命中", guess and guess["device"] == "COM11", guess)
        guess = guess_port({"match": "CH340", "match_serial": "SN-A"})
        check("序列号优先于描述", guess and guess["device"] == "COM10", guess)

        print("\n[5] match_serial 在 resolve_port 里也是一等判据")
        use(BT + [CH340_A, CH340_B])
        got = resolve_port({"match_vid_pid": "1A86:7523", "match_serial": "SN-B"})
        check("vid_pid 撞车时用序列号分开", got.get("device") == "COM11", got)
        check("source=match_serial", got.get("source") == "match_serial", got)

        got = resolve_port({"match_serial": "SN-A"})
        check("单独用 match_serial 也行", got.get("device") == "COM10", got)

        print("\n[6] 精确 COM 号命中时不做多余的事")
        use(BT + [CH340_A])
        got = resolve_port({"com": "COM10"})
        check("source=com", got.get("source") == "com", got)
        check("没有多余的 note", got.get("note") == "", got.get("note"))

        print("\n[7] 同型号两块 + 填了 com：必须能用 com 收窄（说明书指的路要走得通）")
        # 以前 com 只在第 3 步查，而歧义在第 1/2 步就抛了：
        # 错误提示让用户"填 com"，用户填了还是同样的错。
        use(BT + [CH340_A, CH340_B])

        try:
            resolve_port({"match_vid_pid": "1A86:7523"})
            check("两块 CH340 不填 com 应报歧义", False, "居然没报错")
        except BridgeError as exc:
            check("不填 com → ambiguous_device", exc.kind == "ambiguous_device", exc.kind)

        got = resolve_port({"match_vid_pid": "1A86:7523", "com": "COM11"})
        check("填了 com 就选中它", got.get("device") == "COM11", got)
        check("source=com（不是 match_vid_pid）", got.get("source") == "com", got)

        got = resolve_port({"match": "CH340", "com": "Com10"})   # 大小写也要认
        check("描述匹配 + com 收窄（大小写不敏感）", got.get("device") == "COM10", got)

        got = resolve_port({"match_serial": "SN-A", "com": "COM11"})
        check("序列号已能唯一命中时不看 com", got.get("device") == "COM10", got)

        print("\n[8] 1.18：页面选的口写进 state、活过重启、能回到自动识别")
        check_override_persistence()
    finally:
        ports_mod.scan_ports = original

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
