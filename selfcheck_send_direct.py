# -*- coding: utf-8 -*-
"""没有网页实例时能不能直接发（对面 18:10 的 P0 盲区）。

背景：1.35 的意义就是"第一次用的人**没有网页实例**"。
但 `send` 的三级降级链在"网页没跑"时会走到"直接开串口"，
2026-11-09 那次 P0（`UnboundLocalError: resolve_port`）就发生在这一层，
而当时 453 项自检全绿 —— 没有任何一条覆盖"无页面直发"。

这个自检锁三件事：

  1. 没有网页实例时，`send --com COMx` 能直接发并拿到回显
  2. 没有网页实例时，`send --scene S` 走场景解析（歧义时要给候选，不许崩）
  3. 起了网页实例后，`send` 走转发（`port_source == "web"`）

它需要一块**真板子**；一块都没有时整体 SKIP（不是失败）。
用哪个口由环境变量指定，未指定时自动挑第一个 CH340。

运行：python selfcheck_send_direct.py
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PY = sys.executable
BASE = HERE
PASS = []
FAIL = []
SKIP = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def skip(name, why=""):
    SKIP.append("%s %s" % (name, why))
    print("  SKIP %s %s" % (name, why))


def run(argv, timeout=40):
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run([PY, os.path.join(BASE, "bridge.py")] + argv,
                          cwd=BASE, env=env, capture_output=True, timeout=timeout)
    text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    line = text.splitlines()[-1] if text else ""
    try:
        return json.loads(line)
    except ValueError:
        return {"ok": False, "error": {"kind": "internal",
                                       "message": "非 JSON 输出：%s" % text[-200:]}}


def pick_port():
    from serial_bridge.ports import scan_ports
    forced = os.environ.get("SERIAL_TEST_PORT", "").strip()
    if forced:
        return forced
    ports = [item for item in scan_ports(force=True) if item.get("vid_pid")]
    return ports[0]["device"] if ports else ""


def main():
    print("=" * 70)
    print("没有网页实例时的直发（1.35 的核心路径）")
    print("=" * 70)

    port = pick_port()
    if not port:
        skip("直发三连", "（没插 USB 串口，无法测）")
        return finish()

    # 先确保没有网页实例在跑（否则测的就不是这条路）
    run(["webctl", "--action", "close", "--scene", "蓝牙AT"])
    time.sleep(0.8)

    print("\n[1] 无页面：send --com %s" % port)
    got = run(["send", "--com", port, "--command", "AT"])
    check("① 直发能通", bool(got.get("ok")),
          got.get("error", {}).get("kind", ""))
    data = got.get("data") or {}
    check("① 拿到了回显（或明确的无回显）",
          bool(data.get("response")) or data.get("empty") is True,
          json.dumps(data, ensure_ascii=False)[:120])
    check("① 没走网页转发", data.get("port_source") in ("override", "com", "only_port", ""),
          str(data.get("port_source")))

    print("\n[2] 无页面：send --scene 蓝牙AT")
    got2 = run(["send", "--scene", "蓝牙AT", "--command", "AT"])
    if got2.get("ok"):
        check("② 场景直发能通", True)
    else:
        err = got2.get("error") or {}
        # 两块同型号 CH340 时场景本来就分不清；这时**必须给候选**，不许崩
        check("② 歧义时给候选而不是崩",
              err.get("kind") in ("ambiguous_device", "no_device")
              and bool(err.get("candidates") or err.get("hint")),
              json.dumps(err, ensure_ascii=False)[:160])

    print("\n[3] 起实例后：send 走转发")
    opened = run(["webctl", "--action", "open", "--scene", "蓝牙AT"], timeout=60)
    time.sleep(2.5)
    if not opened.get("ok"):
        skip("③ 转发", "（实例没起来）")
    else:
        got3 = run(["send", "--scene", "蓝牙AT", "--command", "AT"])
        data3 = got3.get("data") or {}
        check("③ 走网页转发", data3.get("port_source") == "web",
              str(data3.get("port_source")))
        run(["webctl", "--action", "close", "--scene", "蓝牙AT"])

    return finish()


def finish():
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d / SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
    if SKIP:
        for item in SKIP:
            print("  · %s" % item)
    print("没有网页实例时也能直接发 ✅" if not FAIL else "无页面直发有问题 ❌")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
