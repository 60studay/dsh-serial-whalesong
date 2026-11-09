# 给陌生智能体的一页纸（QUICKSTART for agents）

> ⚠️ **这份文件是"引导组"材料，不是盲测材料。**
> 用它测的是"**有说明书时能不能用好**"；如果你要测"**默认状态它会不会自己选插件**"，
> 请用 `_需求文档/测试卡-盲测.md`（那张卡只讲任务，不提工具）。

> **这份文件的读者是 AI 智能体，不是人。**
> 如果你第一次拿到这个插件，按这个顺序做，**不要自己写串口脚本**。
> 最后有"反馈模板"，把你遇到的问题按那个格式写回去。

---

## 0. 一句话：这是什么

鲸歌（dsh-whalesong）是 DSH 的**串口工具集**：9 个工具 + 一个网页。
它替你做三件你自己写脚本才需要做的事：**找端口、判一帧何时结束、把每次收发都记下来**。

---

## 1. 先看有什么（1 次调用）

```js
serial_list()
```

返回：机器上所有串口 + 已配置的场景 + 每个场景现在会解析到哪个口。

> ⚠️ **如果两个串口的 VID:PID 相同（比如两个 CH340）**，场景可能报"歧义"。
> 这时候**不要放弃**：直接看返回里的 `ports`，用下面的"指定端口发送"。

---

## 2. 发第一条命令（1 次调用）

**方式 A：有可用场景**

```js
serial_send({ scene: "STC8G测试板", command: "V" })
```

**方式 B：不确定用哪个场景（或没配场景）**

```js
serial_send({ com: "COM7", baud: 115200, command: "V" })
```

两者都返回结构化结果：

```json
{ "ok": true, "port": "COM7", "tx": "V", "rx": "OK VER stc8g-lab v4\r\n",
  "rx_hex": "4F 4B ...", "bytes": 22, "elapsed_ms": 63 }
```

**有 `rx` 就说明通了。** 不需要再写 PowerShell / pyserial。

---

## 3. 9 个工具速查

| 工具 | 什么时候用 |
|---|---|
| `serial_list` | 第一步永远用它：看口、看场景、看解析结果 |
| `serial_send` | 发一条命令等回显（最常用） |
| `serial_read` | 只想读、不发送（设备自己一直吐数据时） |
| `serial_status` | 看连接状态、控制权、已发次数、端口是否被占用 |
| `serial_control` | 切换控制权：`ai` / `human` / `pause` / `resume` |
| `serial_watch` | 定时自动发送（轮询）、规则判定开关 |
| `serial_info` | 看某个场景的端口/波特率/协议笔记（**接新设备先读这个**） |
| `serial_web` | 打开可视化页面（人也能看） |
| `serial_daemon` | 常驻进程，避免每次都开关串口（高频用时） |

---

## 4. 最常见的 5 个错误 → 怎么办

| 返回里的 `kind` | 意思 | 你该做 |
|---|---|---|
| `ambiguous` | 多个同型号串口，分不清 | 用 `serial_send({com:"COM7",…})` 显式指定 |
| `port_busy` | 口被占了 | 先 `serial_status` 看谁占的；必要时 `serial_control({action:"human"})` 或关掉网页 |
| `no_response` | 设备没回 | **检查波特率**、检查设备是否需要 `\r\n`、确认是不是 AT 模式 |
| `not_found` | 口号不存在 | 重新 `serial_list`（插拔后口号会变） |
| `permission` | 控制权不是 AI | `serial_control({action:"ai"})`，或让人类点「交给AI」 |

**每一个错误都会带 `hint` 或 `next`**，照着做就行。

---

## 5. 这台机器上现在的实测设备（例子，仅供参考）

```
COM7   115200  STC8G 测试板 v4
       命令：? P V L0 L1 K E <文本>
       故障注入：D <ms> 延迟 / N <n> 半包 / J <n> 垃圾字节 / S <n> 长突发 / B 二进制帧
       ⚠️ L0/L1 只是回 OK LED 0/1，本机没外接 LED，灯不会亮 —— 别把"回包"当"物理现象"

COM10  115200  HC-05 蓝牙模块（AT 模式）
       命令：AT、AT+VERSION?、AT+ROLE?、AT+NAME?、AT+UART?、AT+ADDR?
       注意：查询要带 ?；AT+PAIR / AT+LINK 有 20 秒窗口，别用默认超时
```

验证一下就知道谁是谁：

```js
serial_send({ com: "COM7", baud: 115200, command: "V" })   // 回 OK VER → 这是板子
serial_send({ com: "COM10", baud: 115200, command: "AT" }) // 回 OK → 这是蓝牙模块
```

---

## 6. 不要做的事（踩过的坑）

| ❌ 别做 | 为什么 |
|---|---|
| 用 `pwsh` + `System.IO.Ports.SerialPort` 自己写 | 十几行、要处理编码/超时/半包，而且**没有任何记录**，出了问题查不到 |
| 猜波特率后直接发 | 先用 `serial_info` 看场景配置；要扫波特率就逐档指定 `baud` |
| 对 HC-05 发 `AT+RESET` / `AT+RMAAD` | RESET 会退出 AT 模式；RMAAD 会**清空已配对列表**（这两条都真踩过） |
| 认为"AT 没响应 = 模块坏了" | 数据透传模式下本来就不回 AT，看灯更准 |
| 在 `human` 控制权下发命令 | 会被拒（这是设计，不是 bug） |

---

## 7. 一个完整的小例子（读版本 → 改参数 → 读回）

```js
serial_list()                                             // 看口
serial_send({ scene: "蓝牙AT", command: "AT+VERSION?" })    // +VERSION:hc05V2.3_le OK
serial_send({ scene: "蓝牙AT", command: "AT+ROLE=1" })      // OK
serial_send({ scene: "蓝牙AT", command: "AT+ROLE?" })       // +ROLE:1  ← 改成功了
serial_web({ scene: "蓝牙AT" })                           // 打开页面，人也看得到
```

全程 4 次调用、**0 行串口代码**。

---

## 8. 反馈模板（遇到问题就按这个写）

```markdown
- 我用的工具与参数：（例如 serial_send({com:"COM7", command:"V"})）
- 返回了什么：（原样贴 JSON 或错误文本）
- 设备是什么：（板子型号 / 模块型号 / 不确定）
- 我以为会发生什么：
- 物理上看到了什么：（灯、风扇、继电器等任何可见变化）
- 我试过的替代办法：（例如改用 pwsh 成功/失败）
```

**这六项齐全，问题基本能一次定位。**

---

## 9. 如果插件真的做不了

有几种事它确实不行（**这种时候请明确说"插件做不了 X"**，别默默换脚本）：

- 烧录固件（ISP / SWD）
- 需要精确控制 DTR/RTS 时序的操作
- 非串口的设备（USB HID、CAN、SPI 直连）
- 需要一次保持端口 20 秒以上的长时间窗口（现在还不支持，已在修）

---

**版本**：对应插件 v2.2.2 / 本文件随插件一起更新。
**反馈给谁**：把上面的模板贴回给用户，他会转给插件作者。
