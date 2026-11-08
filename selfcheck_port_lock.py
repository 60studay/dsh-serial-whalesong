# -*- coding: utf-8 -*-
"""按物理端口的跨进程锁 —— 自测（**不需要硬件**）。

针对通讯板 19:20 的需求。锁要证明四件事：

  1. 同一把锁，第二次 acquire 会被挡住（真互斥）
  2. 陈旧锁（进程崩溃留下的）能自愈，不会让后来的永远等
  3. 等超时给出的是 `port_busy` + 可操作提示，不是裸异常
  4. **锁挂在端口上，不是场景上**：两个场景解析到同一个 COM 号时抢的是同一把锁

另外验一条最容易被忽略的：`io.transact` / `send_only` / `drain`
三个入口**都**套上了锁（漏一个就会串包）。

运行：python selfcheck_port_lock.py
"""
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from serial_bridge.errors import BridgeError
from serial_bridge import locking

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def main():
    root_dir = tempfile.mkdtemp(prefix="locktest-")
    try:
        print("\n[1] 真互斥：拿到的第二把要失败")
        path, fd = locking.acquire("COM_TEST", root_dir, timeout_s=0.3)
        check("第一次拿到锁", os.path.exists(path))
        payload = json.load(io.open(path, encoding="utf-8"))
        check("锁里记了 pid", payload.get("pid") == os.getpid(), payload)

        try:
            locking.acquire("COM_TEST", root_dir, timeout_s=0.3)
            check("第二次被挡住", False, "居然也拿到了")
        except BridgeError as exc:
            check("第二次被挡住", exc.kind == "port_busy", exc.kind)
            check("提示里说了等几秒", any("等几秒" in h for h in exc.hint), exc.hint)
            check("提示里给了删锁文件的路", any(".lock" in h for h in exc.hint), exc.hint)
        finally:
            locking.release(path, fd)
        check("释放后锁文件没了", not os.path.exists(path))

        print("\n[2] 陈旧锁自愈（进程被 kill 留下的）")
        os.makedirs(os.path.join(root_dir, "locks"), exist_ok=True)
        stale_path = locking.lock_path("COM_STALE", root_dir)
        with io.open(stale_path, "w", encoding="utf-8") as handle:
            json.dump({"pid": 999999, "ts": time.time() - 120, "device": "COM_STALE"}, handle)
        old = time.time() - 120
        os.utime(stale_path, (old, old))
        path2, fd2 = locking.acquire("COM_STALE", root_dir, timeout_s=0.5)
        check("陈旧锁被抢过来", os.path.exists(path2))
        locked = json.load(io.open(path2, encoding="utf-8"))
        check("锁里的 pid 换成了自己", locked.get("pid") == os.getpid(), locked)
        locking.release(path2, fd2)

        print("\n[3] 锁的 key 是端口，不是场景")
        p_a = locking.lock_path("COM7", root_dir)
        p_b = locking.lock_path("COM7", root_dir)
        p_c = locking.lock_path("/dev/ttyUSB0", root_dir)
        check("同一个端口两次算出的路径相同", p_a == p_b)
        check("不同端口不同锁", p_a != p_c)
        check("带斜杠的设备名被 sanitize 了",
              "/" not in os.path.basename(p_c) and "\\" not in os.path.basename(p_c), p_c)

        print("\n[4] io 的三个入口都套了锁")
        import serial_bridge.io as sio
        import inspect
        for name in ("transact", "send_only", "drain"):
            src = inspect.getsource(getattr(sio, name))
            check("%s 里有 port_lock" % name, "port_lock(" in src, src[:60])
        # 实现体里不该再套一层（会自己锁自己）
        for name in ("_transact_impl", "_send_only_impl", "_drain_impl"):
            impl = getattr(sio, name, None)
            check("%s 存在（原实现被保留）" % name, impl is not None)
            if impl:
                src = inspect.getsource(impl)
                check("%s 内部没有再套锁" % name, "port_lock(" not in src, src[:60])

        print("\n[5] 跨进程互斥（真开两个 python 进程抢）")
        # 线程锁证明不了跨进程；这里起两个真进程，各自"拿锁 → 睡一会儿 → 放锁"，
        # 把两段持有时间拼起来看有没有重叠。
        code = (
            "import sys, time, json\n"
            "sys.path.insert(0, r'%s')\n"
            "from serial_bridge import locking\n"
            "t0 = time.time()\n"
            "path, fd = locking.acquire('COM_PROC', timeout_s=10)\n"
            "t1 = time.time()\n"
            "time.sleep(0.4)\n"
            "locking.release(path, fd)\n"
            "t2 = time.time()\n"
            "print(json.dumps({'hold_start': t1, 'hold_end': t2}))\n" % HERE
        )
        env = dict(os.environ)
        env["SERIAL_BRIDGE_RUNTIME_DIR"] = root_dir
        env["PYTHONIOENCODING"] = "utf-8"
        import subprocess
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
        results = []
        ok = True
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            if proc.returncode != 0:
                ok = False
                results.append(None)
                print("    子进程失败:", err.decode("utf-8", "replace")[:200])
                continue
            results.append(json.loads(out.decode("utf-8").strip().splitlines()[-1]))
        check("两个进程都成功拿到过锁", ok and all(results), results)
        if ok and all(results):
            a, b = sorted(results, key=lambda item: item["hold_start"])
            overlap = a["hold_end"] > b["hold_start"]
            check("两次持有时间不重叠（真互斥）", not overlap,
                  "A %.3f~%.3f  B %.3f~%.3f" % (a["hold_start"], a["hold_end"],
                                                b["hold_start"], b["hold_end"]))
            check("第二个确实等过一会儿", b["hold_start"] - a["hold_start"] >= 0.3,
                  "%.3f" % (b["hold_start"] - a["hold_start"]))
        check("跑完没有残留锁文件",
              not os.path.exists(locking.lock_path("COM_PROC", root_dir)))

    finally:
        import shutil
        shutil.rmtree(root_dir, ignore_errors=True)
    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
