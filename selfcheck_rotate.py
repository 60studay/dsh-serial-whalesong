# -*- coding: utf-8 -*-
"""自检：只进不出的累加（1.20）。

为什么有这一条
--------------
用户让做一次"还有没有像通讯板那样无限累加的地方"的体检。查出两处真的没有上限：

  1.20-a  `logs/<场景>/log.txt`        —— 只要在跑就一直追加，从不轮转
  1.20-b  `logs/.runtime/alerts.jsonl` —— 网页告警列表的数据源，只追加不裁剪

这两处都属于"平时看不见、跑一夜就变胖"，**功能自检永远发现不了**，
所以单独用一个小上限把小文件当大文件测：真跑几天才会发生的事，这里 1 秒就复现。

顺带锁住两条纪律
----------------
* **只轮转不删除**：日志是证据，清空等于把证据删了；
  但超过保留份数（3 份）最老的那份要真删，否则等于没上限。
* **`tail()` 只读当前文件**：把历史文件也读进来，`serial_read(lines=50)`
  会突然变成几百行，用户会以为工具坏了。

运行：python selfcheck_rotate.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from serial_bridge.log import Logger
from serial_bridge.state import ALERT_KEEP, SharedState

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def files_in(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def main():
    root = tempfile.mkdtemp(prefix="rotate-")
    try:
        logs = os.path.join(root, "logs")
        # 上限调到 300 字节：几行就轮转一次，跑 200 行相当于真跑好几天
        logger = Logger(logs, "s", max_bytes=300, keep=3)
        for index in range(200):
            logger.append("TX", "第 %03d 行内容内容内容内容" % index)

        scene_dir = os.path.join(logs, "s")

        print("\n[1] 1.20-a：log.txt 到上限就轮转")
        names = files_in(scene_dir)
        check("轮转出了 log.1.txt", "log.1.txt" in names, names)
        check("目录里最多 4 个文件（当前 + 3 份历史）", len(names) <= 4, names)
        check("当前 log.txt 还在", "log.txt" in names, names)
        check("当前 log.txt 只含最近几次写入（不累积全量）",
              os.path.getsize(os.path.join(scene_dir, "log.txt")) <= 300 + 100,
              os.path.getsize(os.path.join(scene_dir, "log.txt")))

        print("\n[2] 只轮转不删除：历史留着，但只留 3 份")
        check("log.1 / log.2 / log.3 都在",
              all(("log.%d.txt" % i) in names for i in (1, 2, 3)), names)
        check("不会无限长出去（没有 log.4.txt）", "log.4.txt" not in names, names)

        print("\n[3] tail() 只读当前文件")
        tail = logger.tail(5)
        tail_lines = [ln for ln in tail.splitlines() if ln.strip()]
        check("tail(5) 最多 5 行（不把历史读进来）", len(tail_lines) <= 5, len(tail_lines))
        check("tail 里有最新的那行", "第 199 行" in tail, tail[-80:])
        check("tail 里没有最早的那行（它已经被轮转出去了）", "第 000 行" not in tail, tail[:80])

        print("\n[4] 1.20-b：告警账本超上限就裁到最近 %d 条" % ALERT_KEEP)
        runtime = os.path.join(root, "logs", ".runtime")
        state = SharedState(runtime, "s")
        for index in range(ALERT_KEEP + 100):
            state.append_alert({"msg": "a%d" % index})
        with open(state.alerts_path, encoding="utf-8") as handle:
            lines = [ln for ln in handle.read().splitlines() if ln.strip()]
        check("行数被裁到 %d" % ALERT_KEEP, len(lines) == ALERT_KEEP, len(lines))
        kept = [json.loads(ln) for ln in lines]
        check("最旧的 100 条真的没了", kept[0].get("msg") == "a100", kept[0].get("msg"))
        check("最新的那条还在", kept[-1].get("msg") == "a%d" % (ALERT_KEEP + 99),
              kept[-1].get("msg"))
        check("序号/时间没被改乱（每条都有 ts 和 time）",
              all("ts" in item and "time" in item for item in kept))

        print("\n[5] 裁剪之后 /alerts 的读取照旧")
        recent = state.read_alerts(3)
        check("read_alerts(3) 返回 3 条", len(recent) == 3, len(recent))
        check("返回的是最近的 3 条、顺序不乱",
              [item.get("msg") for item in recent] ==
              ["a%d" % (ALERT_KEEP + 97), "a%d" % (ALERT_KEEP + 98), "a%d" % (ALERT_KEEP + 99)],
              [item.get("msg") for item in recent])

        print("\n[6] 没超上限时不动文件（别每次都重写）")
        small = SharedState(os.path.join(root, "small", ".runtime"), "s")
        for index in range(5):
            small.append_alert({"msg": "b%d" % index})
        check("trim_alerts 没超上限时返回 False", small.trim_alerts() is False)
        check("5 条还是 5 条", len(small.read_alerts(100)) == 5, small.read_alerts(100))

        print("\n[7] 清空告警仍然好使（不能被裁剪逻辑带坏）")
        cleared = small.clear_alerts()
        check("clear_alerts 返回清掉的条数", cleared == 5, cleared)
        check("清空后读不到东西", small.read_alerts(10) == [], small.read_alerts(10))
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
