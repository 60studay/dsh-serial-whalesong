# ESP8266 / ESP-01（AT 固件）

> **状态**：📄 来自公开 AT 指令集文档，**本机未实测**（`unverified`）
> **来源**：[ESP AT Commands Set](https://raw.githubusercontent.com/Moo2017/esp-at/refs/heads/master/docs/ESP_AT_Commands_Set.md)、[ESP-01 常用 AT](https://raw.githubusercontent.com/carl1961/esp01-firmware-flash-tutorial/refs/heads/main/esp01_at_command.md)
> **用之前**：先 `AT` 探活，再 `AT+GMR` 看固件版本 —— **不同固件支持的指令集不一样**。

## 先确认是不是它

```
AT     →  OK
AT+GMR →  AT version:... / SDK version:...
```

**不 OK 的三个常见原因**：

1. **波特率不对**：常见 115200，老固件 9600 或 74880 → 用 `autoBaud` 扫
2. **不是 AT 固件**（刷成了 NodeMCU / MicroPython）→ 会回乱码或什么都不回
3. **供电不足**：ESP8266 峰值电流大（>300mA），USB-TTL 的 3.3V 常常带不动 → 表现是"偶尔回一句 OK，然后断"

## 基础命令

| 命令 | 回包 | 作用 |
|---|---|---|
| `AT` | `OK` | 探活 |
| `AT+RST` | `OK` + 启动日志 | 重启 |
| `AT+GMR` | 版本信息 | 固件版本 |
| `ATE0` / `ATE1` | `OK` | 关/开回显（脚本化时关掉更省事） |
| `AT+RESTORE` | `OK` | 恢复出厂 |
| `AT+CWMODE?` / `AT+CWMODE=1/2/3` | `+CWMODE:n` | Station / SoftAP / 两者 |
| `AT+CWLAP` | `+CWLAP:(...)` 多行 | 扫描周围 WiFi（**耗时长，几秒**） |
| `AT+CWJAP="ssid","pwd"` | `WIFI CONNECTED` → `OK` | 连路由（**可能 5~10 秒**） |
| `AT+CIFSR` | `+CIFSR:STAIP,"x.x.x.x"` | 查本机 IP |
| `AT+CIPMUX=0/1` | `OK` | 单连接 / 多连接 |
| `AT+CIPSTART="TCP","host",port` | `CONNECT` → `OK` | 建 TCP |
| `AT+CIPSEND=<len>` | `>` 提示符 → 发 len 字节 → `SEND OK` | 发数据 |
| `AT+CIPCLOSE` | `OK` | 关连接 |

## 坑

1. **`AT+CIPSEND` 是两段式**：先发命令，等 `>` 提示符，再发**正好 len 字节**。
   少了不行、多了会被当新命令。**这类"要等提示符"的交互，用 `hold` 会话做最稳。**
2. **`AT+CWJAP` 很慢**（5~10 秒），默认超时容易误判成失败 → 发之前把
   `wait_first_byte_ms` 调大（≥8000），或者改成分步：先 `AT+CWJAP?` 看是否已连。
3. **ESP-01 只有 1MB flash**，AT 固件占满，别指望再塞别的。
4. **3.3V 供电**，**绝不能接 5V**。
5. 透传模式（`AT+CIPMODE=1` + `AT+CIPSEND`）下，**AT 指令会变成数据发出去** ——
   想退出得先发 `+++`（前后各留 1 秒静默）。

## 什么时候别用它

要"简单透传一根线"，HC-05 更省事；ESP8266 的价值在 WiFi/TCP/HTTP。

## 用我们的工具怎么玩

```
serial_send(autoBaud=true, com="COM7", command="AT")      ← 找波特率 + 探活
serial_send(com="COM7", command="AT+GMR", hold=true)      ← 连发保持端口
serial_send(com="COM7", command="AT+CWJAP=...")           ← 注意超时给足
```
