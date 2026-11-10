# -*- coding: utf-8 -*-
"""监视模式下常驻持口的读循环（从 supervisor.py 抽出来的）。

职责很窄：拿着一个已打开的连接，持续读、广播给网页、按需触发判定。
它不管 HTTP，也不管"该不该持口"（那是 port_manager 的事）。
"""

import threading
import time

from . import io as serial_io
from .ports import resolve_port
from .rule_checker import RuleChecker


class WatchLoop(object):
    """watch 模式的常驻读取线程。

    两个开关在这里也是独立的：
      定时发送线程（send_enabled）负责写
      判定（check_enabled）在数据到达时同步跑，但有规则级超时保护
    任一线程出错都不影响另一个；规则引擎出错更不会影响读口。
    """

    def __init__(self, scene, paths, state, logger):
        self.scene = scene
        self.paths = paths
        self.state = state
        self.logger = logger
        # 配置从哪来。默认场景配置；supervisor 会换成 effective_cfg（含页面 override），
        # 否则"页面选的串口"到不了定时/规则这两条路径（对面 1.12）。
        self.cfg_provider = lambda: self.scene.cfg
        self.ser = None
        self.thread = None
        self.send_thread = None
        self.stop_event = threading.Event()
        self.buffer = bytearray()
        self.lock = threading.Lock()
        # 自动发送的"发送记录"：网页靠它显示 `>> 命令` + 回显
        self.auto_frames = []
        # 串口互斥：定时发送线程、手动 /send、/poll 读一次，三路都要开关同一个口。
        # 不加锁会自己跟自己抢出 "被占用"（真机实测复现）。
        self.port_lock = threading.Lock()
        self.no_response_streak = 0
        self.last_error = None
        self.device = ""
        self.running = False
        # 手动发送的时间窗：读线程用它判断"这段回显是手动触发的"。
        # 以前这个属性只在 WebServer 上，读线程读 self._manual_until 直接 AttributeError
        # —— 第一条回显就把读线程干掉，之后"只有 TX 没有 RX"（1.31 的根因）。
        self._manual_until = 0.0
        # 1.60：这个口是"监视模式"还是"轻模式定时发送握着"——决定回显怎么拿。
        self.watch_mode = False
        # 1.66：同步收这次回显的窗口内，读线程**不要**再记一行 RX。
        # 否则同一条回显会被记两遍（读线程按 chunk 记 + 这里整体记）。
        self._suppress_reader_log_until = 0.0
        self.last_activity_at = 0.0     # 1.68：最近一次收发的时刻（判断“是不是有人绕过插件”）
        # 判定统一交给 RuleChecker（与管家共用同一份逻辑，不再各写一份）
        self.checker = RuleChecker(scene, state, logger, send_action=self._send_rule)

    def start(self, device, baud):
        self.stop()
        self.device = device
        self.baud = baud
        self.ser = serial_io.open_port(self.cfg_provider(), device, baud)
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, name="watch-read", daemon=True)
        self.thread.start()
        self.send_thread = threading.Thread(target=self._send_loop, name="watch-send", daemon=True)
        self.send_thread.start()

    def stop(self):
        self.running = False
        self.stop_event.set()
        for thread in (self.thread, self.send_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        self.thread = None
        self.send_thread = None
        if self.checker._worker is not None:
            self.checker._worker.close()
            self.checker._worker = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def take(self):
        with self.lock:
            data = bytes(self.buffer)
            self.buffer.clear()
            return data

    def send_raw(self, command, raw_bytes=None, source="timer"):
        """发一条命令并返回回显文本。

        持有句柄就写句柄（监视模式）；没持有就临时开口（轻模式）。
        定时发送和"规则自动处置"都走这里，所以两种模式行为一致。

        source 决定网页上这条命令挂什么徽章：
          "timer" 定时发送（用户自己设的，黄）
          "rule"  规则自动处置（**系统自己动手**，红 —— 最需要被看见的一类）
        以前两者共用默认值 "auto"，页面上全显示成「定时」，
        等于把"系统自主行为"藏进了"我自己设的"里（用户实测报的 bug）。

        **cfg 从 `self.cfg_provider()` 取，不是 `self.scene.cfg`**：
        用户在页面上选的串口/波特率是"运行期覆盖"（override_com / override_baud），
        只存在 supervisor 的 effective_cfg() 里。以前这里直接读场景配置，
        于是「页面选了 COM7，定时却把命令发给了 config 里写的 COM10」——
        真机上表现为"定时发出去一点回显都没有"（对面 1.12 的决定性实验）。

        raw_bytes 不为 None 时按原始字节发送（hex 场景的命令），两条路径都要尊重它。

        每次发送都会往 auto_frames 里记一帧，网页据此显示命令 + 回显。
        """
        cfg = self.cfg_provider()
        # 定时 / 规则自动处置：**不许等回包**。
        # transact 的等待由 cfg["timeout_read_ms"] 控制，默认 500ms；
        # 这里压到 80ms —— 板子秒回时照样拿到回显（规则还能判），
        # 板子不回话时也只拖 80ms，节奏不会被 1.5s 的等首字节吃掉。
        # 迟到（>80ms）的回包按"迟到"处理，不补判、不改节奏（对面 1.8 判据）。
        if source in ("timer", "rule"):
            cfg = dict(cfg)
            cfg["timeout_read_ms"] = int(cfg.get("autosend_timeout_ms") or 80)
            # **首字节窗也要压**：没收到字节时 io.transact 是在 wait_first_byte_ms 处 break 的，
            # 只压 timeout_read_ms（那管的是"收到字节后等静默"）等于没治。
            # 对面实测：wait_first_byte_ms=1500（代码默认）时，设备不回话 500ms 就跑成 1591ms。
            # 方案 A：首字节窗 ≤ 间隔-50，设备秒回照样拿到回显，不回也只拖到间隔附近。
            try:
                watch_cfg = (self.state.read() or {}).get("watch") or {}
                interval_ms = int(watch_cfg.get("interval_ms") or 0)
            except Exception:
                interval_ms = 0
            if interval_ms > 50:
                current = int(cfg.get("wait_first_byte_ms") or 1500)
                cfg["wait_first_byte_ms"] = max(50, min(current, interval_ms - 50))
        newline = serial_io.decode_newline(cfg.get("newline"), "\\r\\n")
        if self.running and self.ser is None:
            # 1.68 回归：`start()` 会先把 running 置真、再打开句柄。这中间有几十毫秒，
            # 定时线程正好撞进来就会走"每轮开关"，而我们自己的句柄已经占着口 →
            # 日志里每拍一条"COM7 被其他程序占用"，但收发其实是成功的（只是绕了一圈）。
            deadline = time.time() + 0.6
            while self.running and self.ser is None and time.time() < deadline:
                time.sleep(0.01)

        # 1.68：**句柄还开着就用它**，不要看 running —— stop() 会先把 running 置假、
        # 再关句柄，这中间定时线程会走"每轮开关"，结果开的是自己正占着的口
        # （日志每拍一条"被其他程序占用"，其实是自己）。
        if self.ser is not None:
            # **握手柄时也必须互斥**：定时线程和网页/AI 的手动发送会同时写同一个口，
            # 不锁的话两条命令的字节会交错，设备收到的就是乱码（表现为"不回话"）。
            # 1.31 实测：定时 500ms 跑着时 AI 发命令，日志只有 TX 没有 RX。
            with self.port_lock:
                if raw_bytes is not None:
                    self.ser.write(raw_bytes)
                else:
                    self.ser.write(str(command).encode("utf-8") + newline)
            self.logger.append("TX", command, self.device)
            self.last_activity_at = time.time()
            # 监视模式：回显走读线程进 buffer，这里只记"发了什么"。
            # **但帧上必须标出来**：以前这里什么都不标，于是界面上看起来像
            # "发了、设备没回"，和"真的没回"分不开——排查时会把两种完全不同的
            # 情况混成一个（这次就是：命令可能已经到了 COM7，只是帧里没回显）。
            if not getattr(self, "watch_mode", False):
                # 1.60：轻模式下这条口是"定时发送自己握着"（1.8 尾巴），**没有读线程会事后补回显**，
                # 标 pending 等于永远空。这里像 transact 那样同步收一次。
                raw = self._collect_echo(
                    max(0.8, int(cfg.get("wait_first_byte_ms") or 1500) / 1000.0),
                    max(0.02, int(cfg.get("timeout_read_ms") or 200) / 1000.0))
                text = raw.decode("utf-8", errors="replace")
                if text:
                    self.logger.append("RX", text)
                self.last_activity_at = time.time()      # 1.66：整条记一次，读线程这次不记
                self._push_frame(command, text, " ".join("%02X" % b for b in raw),
                                 len(raw), source, self.device)
                if raw:
                    self.no_response_streak = 0
                else:
                    self.no_response_streak += 1
                return text
            frame = self._push_frame(command, "", "", 0, source, self.device)
            if isinstance(frame, dict):
                frame["rx_pending"] = True
            return ""
        resolved = resolve_port(cfg)
        with self.port_lock:
            result = serial_io.transact(cfg, resolved["device"], command,
                                        raw_bytes=raw_bytes)
        self.logger.append("TX", command, self.device)
        if result["rx_text"]:
            self.logger.append("RX", result["rx_text"])
        self._push_frame(command, result["rx_text"], result["rx_hex"], result["bytes"],
                         source, (result.get("device") or self.device))
        # 轻模式的自动发送也要更新"无回显计数"，否则 no_response 规则永远不触发
        if result["empty"]:
            self.no_response_streak += 1
        else:
            self.no_response_streak = 0
        return result["rx_text"]

    def _send_rule(self, command):
        """规则自动处置的发送回调：单独包一层，只为把来源标成 "rule"。

        不直接在 RuleChecker 里传 source，是因为"怎么发"属于这一层，
        判定层不该知道网页徽章长什么样。
        """
        return self.send_raw(command, source="rule")

    def _collect_echo(self, timeout_s, silence_s):
        """从读线程的缓冲里凑一条回显（先等首字节，再等到静默）。

        和 web/serial.py 里那份同一个思路：**只读缓冲，不动句柄**。
        轻模式定时发送自己握着口，没有 transact 可用，只能这样拿回显。
        """
        out = b""
        # 1.66：收集期间让读线程闭嘴，收集完由这里**整条**记一行 ——
        # 这样每条 TX 后面正好一条 RX（以前是两条：读线程一片 + 这里一条）。
        self._suppress_reader_log_until = time.time() + timeout_s + silence_s + 1.0
        deadline = time.time() + timeout_s
        while time.time() < deadline and not out:
            out += self.take()
            if not out:
                time.sleep(0.01)
        if not out:
            return b""
        quiet_at = time.time() + silence_s
        while time.time() < quiet_at:
            chunk = self.take()
            if chunk:
                out += chunk
                quiet_at = time.time() + silence_s
            else:
                time.sleep(0.01)
        return out

    def _push_frame(self, tx, rx, rx_hex, nbytes, source="auto", port=""):
        """记一帧给网页显示。返回这一帧本身，调用方可以补字段（比如 rx_pending）。

        1.70：帧要带**自己的口号**。以前页面只能拿"当前场景解析到的口"画徽章，
        于是 `--com COM10` 发的命令也显示 COM7 —— 用户看到的"发给了谁"是假的。
        """
        frame = {
            "tx": tx, "rx": rx, "hex": rx_hex,
            "bytes": int(nbytes or 0), "ts": time.time(), "source": source,
            "port": str(port or getattr(self, "device", "") or ""),
            # 统一字段，两种模式都有：
            #   轻模式 = 同步收发，回显已经在这一帧里 → False；
            #   监视模式 = 回显走读线程，先标 True，取到后再补（见 send_raw）。
            # 以前只有监视模式带这个键，读 /poll 的人得先判断键在不在。
            "rx_pending": False,
        }
        with self.lock:
            self.auto_frames.append(frame)
            if len(self.auto_frames) > 200:
                del self.auto_frames[:-200]
        return frame

    def read_frames(self, since=0.0):
        """按游标取帧，**不消费**（1.17）。

        游标就是上一帧的 `ts`。多个看客（页面标签、测试脚本）各自持游标，
        互不抢帧；服务端只保留最近 200 条（`_push_frame` 里裁）。
        """
        try:
            since = float(since or 0)
        except (TypeError, ValueError):
            since = 0.0
        with self.lock:
            return [dict(f) for f in self.auto_frames
                    if float(f.get("ts") or 0) > since]

    def take_frames(self):
        """取走待显示的自动发送帧。"""
        with self.lock:
            frames = list(self.auto_frames)
            self.auto_frames.clear()
            return frames

    def _read_loop(self):
        while not self.stop_event.is_set():
            try:
                chunk = self.ser.read(512)
            except Exception as exc:
                self.last_error = str(exc)
                time.sleep(0.2)
                continue
            if not chunk:
                continue
            # 这一段**必须整体兜住**：读线程一旦因为某次处理异常悄悄死掉，
            # 之后所有回显都进不了缓冲 —— 页面/接口看到的就是"只有 TX 没有 RX"，
            # 而且 running 还是 True、watch_error 还是空（1.31 现场就是这样）。
            try:
                with self.lock:
                    self.buffer.extend(chunk)
                text = chunk.decode("utf-8", errors="replace")
                # 1.66：同步收集期间不重复记 RX（见 _collect_echo）
                if time.time() >= self._suppress_reader_log_until:
                    self.logger.append("RX", text)
                self.last_activity_at = time.time()
                self.no_response_streak = 0
                # 刚手动/网页发过命令 → 这段回显算 manual，否则算轮询 watch
                source = "manual" if time.time() < self._manual_until else "watch"
                self._run_checks(text, source, chunk)
            except Exception as exc:          # noqa: BLE001 读线程不能死
                self.last_error = "处理回显失败：%s" % exc
                continue

    def _send_loop(self):
        """（已停用）定时发送现在由 WebServer._autosend_loop 统一负责。

        原因：这个循环只活在 WatchLoop 里，而 WatchLoop 只在"监视模式 + 控制权 ai"
        时才启动。于是 light 模式下把 send_enabled 打开，watch_running 永远是 false、
        一条命令也发不出去（Bug 4）。定时发送属于"用户配置的自动化"，不该依赖
        网页是不是在占口，所以搬到 WebServer 层，两种模式都能跑。
        """
        return


    def _run_checks(self, text, source, raw_bytes=None):
        """判定入口：委托给共用的 RuleChecker（开关/熔断/告警都在那边）。

        raw_bytes 一定要传：hex / hex_regex 判据比的是原始字节，
        只给解码后的 text 会把 0xFF 变成替换符，永远命中不了。
        """
        self.checker.run(text, source, self.no_response_streak, raw_bytes=raw_bytes)
