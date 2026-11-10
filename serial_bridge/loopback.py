# -*- coding: utf-8 -*-
"""跨口环回：往 A 写、从 B 读（2026-11-10，gcSTM32 项目的反馈 2）。

为什么需要：
    验证"两个蓝牙模块是否真的配对"唯一可靠的判据就是**从 A 写、从 B 读**。
    以前插件只能 `serial_send`（同一个口发和收），于是那位 AI 只能自己开两个
    .NET 句柄 —— 代价是**网页和日志里什么都没有**，用户看到空白，
    以为插件没工作。能力缺失不能靠"绕过"来解决。

边界：
    · 两个口都真开（各自独占），所以需要它们都没被别人占；
    · 先写后读，读侧**持续读到期望值或超时**，不假设"一问一答"；
    · 不猜协议：编码原样透传，期望值只做子串/字节匹配；
    · 任何一侧打不开都返回结构化错误，绝不半开半关（finally 里关）。
"""

import time

from .errors import BridgeError
from .locking import port_lock

# 读侧刷新间隔：太小会空转 CPU，太大可能漏掉短回包
_POLL_S = 0.01


def _open(device, baud, logger=None):
    import serial  # pyserial（vendored）

    try:
        handle = serial.Serial(device, int(baud), timeout=0.05)
    except Exception as exc:            # noqa: BLE001  统一翻成 kind
        kind = "port_busy" if "拒绝访问" in str(exc) or "Access is denied" in str(exc) else "no_device"
        raise BridgeError(kind, "打不开 %s：%s" % (device, exc),
                          hint=["确认口没被串口助手 / 别的脚本占着", "确认口号没写错（serial_list 可查）"])
    return handle


def loopback(from_device, to_device, data, baud=115200, expect=None,
             timeout_ms=1500, newline="", logger=None):
    """往 `from_device` 写 `data`，从 `to_device` 读回显。

    返回 dict：写入字节数、读到什么、是否匹配 `expect`、耗时。
    """
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if newline:
        payload += newline.encode("utf-8") if isinstance(newline, str) else bytes(newline)

    expected = None
    if expect not in (None, ""):
        expected = expect.encode("utf-8") if isinstance(expect, str) else bytes(expect)

    timeout_s = max(0.05, float(timeout_ms) / 1000.0)
    tx_handle = rx_handle = None
    started = time.time()
    got = b""
    try:
        # 两个口都要锁：别人（定时发送 / 网页 / 别的 AI）正用着任何一个都不行
        with port_lock({}, from_device):
            tx_handle = _open(from_device, baud, logger)
            try:
                with port_lock({}, to_device):
                    rx_handle = _open(to_device, baud, logger)
                    try:
                        if logger is not None:
                            logger.append("TX", "[loopback %s→%s] %s"
                                          % (from_device, to_device, payload.decode("utf-8", "replace")))
                        tx_handle.write(payload)
                        tx_handle.flush()
                        deadline = time.time() + timeout_s
                        while time.time() < deadline:
                            chunk = rx_handle.read(256)
                            if chunk:
                                got += chunk
                                if expected is not None and expected in got:
                                    break
                                continue
                            time.sleep(_POLL_S)
                    finally:
                        if rx_handle is not None:
                            rx_handle.close()
                            rx_handle = None
            finally:
                if tx_handle is not None:
                    tx_handle.close()
                    tx_handle = None
    except BridgeError:
        raise
    except Exception as exc:            # noqa: BLE001
        raise BridgeError("internal", "环回失败：%s" % exc,
                          hint=["确认两个口都插着且没被占用"])
    finally:
        for handle in (tx_handle, rx_handle):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    text = got.decode("utf-8", errors="replace")
    if logger is not None and text:
        logger.append("RX", text)
    matched = None if expected is None else (expected in got)
    return {
        "from": from_device,
        "to": to_device,
        "baud": int(baud),
        "sent": payload.decode("utf-8", errors="replace"),
        "sent_bytes": len(payload),
        "received": text,
        "received_hex": " ".join("%02X" % b for b in got),
        "received_bytes": len(got),
        "expect": expect or "",
        "matched": matched,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
