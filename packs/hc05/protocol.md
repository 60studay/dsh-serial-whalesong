# HC-05 / HC-06 蓝牙串口模块

> **状态**：✅ 本机实测（2026-11-09，两块 `hc05V2.3_le` 克隆板，主从配对成功）
> **来源**：模块实测 + [HC-05 主从机通信](https://cloud.tencent.cn/developer/article/2497827)

## 先确认是不是它

```
AT  →  OK
```

**不 OK 的两种常见原因**（都不是设备坏）：

1. **不在 AT 模式**：HC-05 要 `KEY/EN` 接高电平再上电（或按住 KEY 上电），LED 慢闪才是 AT 模式。
2. **波特率不对**：AT 波特率**跟随 `AT+UART` 设置**；出厂常 38400，克隆版常见 115200。
   → 用 `serial_send(autoBaud=true)` 一次扫完。

## 两个模式

| 模式 | 表现 | 能干什么 |
|---|---|---|
| **AT 模式** | 慢闪（约 2 秒一亮） | 发 `AT+...` 改配置 |
| **数据透传** | 快闪（未连）/ 双闪或常亮（已连） | **发什么都是数据**，AT 永远不会有回应 |

⚠️ **已配对的模块除非重新上电，否则进不了 AT 模式。**

## 命令（AT 模式）

| 命令 | 回包 | 作用 |
|---|---|---|
| `AT` | `OK` | 探活 |
| `AT+VERSION?` | `+VERSION:xxx` | 固件版本 |
| `AT+ROLE?` / `AT+ROLE=0/1` | `+ROLE:0/1` | 0=从机，1=主机 |
| `AT+CMODE?` / `AT+CMODE=0/1` | `+CMODE:0/1` | 1=任意地址自动连，0=只连绑定地址 |
| `AT+NAME?` / `AT+NAME=xxx` | `+NAME:xxx` | 设备名（会转大写存储） |
| `AT+UART?` / `AT+UART=115200,0,0` | `+UART:115200,0,0` | 数据波特率 |
| `AT+PSWD?` / `AT+PSWD=xxxx` | `+PSWD:xxxx` | 配对码 |
| `AT+BIND?` / `AT+BIND=xxxx,yy,zzzzzz` | `+BIND:...` | 绑定从机地址 |
| `AT+ADDR?` | `+ADDR:xx:xx:xx:xx:xx:xx` | 本机地址 |
| `AT+STATE?` | `+STATE:...` | 当前状态（PAIRABLE / CONNECTING / CONNECTED） |
| `AT+RESET` | `OK` | 重启（会退出 AT 模式） |

## 主从配对的最小配方（实测通过）

**从机**：

```
AT+ROLE=0
AT+UART=115200,0,0
AT+PSWD=123456          ← 见下面"坑 1"
```

**主机**：

```
AT+ROLE=1
AT+CMODE=1              ← 任意地址自动连
AT+UART=115200,0,0
AT+PSWD=123456          ← 两边必须一样
AT+RESET                ← 复位后自己会去连
```

复位后等 5~10 秒，两边 LED 变双闪/常亮，从主机发的字节会原样出现在从机。

## 坑（都踩过）

1. **克隆固件（`hc05V2.3_le`）的密码是 6 位**：
   `AT+PSWD=1234` → `FAIL`；`AT+PSWD=123456` → `OK`。
2. **这批固件不实现** `AT+PAIR` / `AT+LINK` / `AT+INQ` / `AT+INIT` / `AT+INQM`
   —— 发过去是**静默**（不是 `ERROR`）。**别按官方手动配对流程试，白耽误时间。**
3. **打开串口默认会拉 DTR → 模块复位**。用 pyserial 时必须 `dtr=False, rts=False`。
4. **AT 波特率跟随 `AT+UART`**，改完 UART 要换波特率继续。
5. HC-05 是经典蓝牙 2.0，**和 BLE 模块不能互通**。

## 用我们的工具怎么玩

```
serial_send(autoBaud=true, com="COM7", command="AT")   ← 一次找到波特率并探活
serial_send(com="COM7", command="AT+ROLE?", hold=true) ← 连发时保持端口
serial_close(com="COM7")
```
