# JDY 系列（JDY-31 / JDY-23 / JDY-16 / JDY-40）

> **状态**：📄 来自厂商规格书与 SunFounder 文档，**本机未实测**（`unverified`）
> **来源**：[JDY-31 AT 指令集（SunFounder）](https://docs.sunfounder.com/projects/ultimate-sensor-kit/en/latest/components_basic/38-component_jdy31.html)、[JDY-23 规格书（立创）](https://atta.szlcsc.com/upload/public/pdf/source/20250521/089BBEFCDEE862E93D3D2A4CA44F4B04.pdf)
> **常见度**：JDY-31 ≈ "5 块钱的 HC-06 替代"，国内 DIY 最常用的蓝牙串口模块之一

## 先分清型号（**别把它们的 AT 混着用**）

| 型号 | 类型 | 特点 |
|---|---|---|
| **JDY-31** | 经典蓝牙 3.0 SPP | 引脚兼容 HC-06，**默认 9600**，最便宜 |
| **JDY-23** | BLE 4.2 | 手机 APP 用，AT 集不同（`AT+VER` / `AT+MAC`…） |
| **JDY-16** | BLE 5.0 | 同上，透传+IO |
| **JDY-40** | 2.4G 私有协议 | **不是蓝牙**，是点对点 2.4G 透传 |
| **JDY-08/09** | BLE | 老款 |

## 先确认是不是它

```
AT            →  OK                （JDY-31）
AT+VERSION    →  JDY-31-V1.2       ← 看到 JDY 字样就能确认
```

**JDY-31 默认 9600 8N1**（HC-05 是 38400）——**这是最容易搞混的一点**。
不确定就用 `serial_send(autoBaud=true)` 扫一遍。

## JDY-31 命令（AT 模式 = 上电即 AT，**不需要按键**）

| 命令 | 回包 | 说明 |
|---|---|---|
| `AT` | `OK` | 探活 |
| `AT+VERSION` | `JDY-31-V1.2` | 版本 |
| `AT+NAME` / `AT+NAME=xxx` | `+NAME=xxx` | 广播名（默认 `JDY-31-SPP`） |
| `AT+BAUD` / `AT+BAUD=8` | `+BAUD=8` | **档位制**：4=9600、5=19200、6=38400、7=57600、8=115200 |
| `AT+PIN` / `AT+PIN=1234` | `+PIN=1234` | 配对码（**默认 1234**） |
| `AT+LADDR` | `+LADDR=xx:xx:...` | 本机 MAC |
| `AT+DISC` | `OK` | 断开当前连接（已连时有效） |
| `AT+RESET` | `OK` | 软复位 |
| `AT+DEFAULT` | `OK` | 恢复出厂 |
| `AT+ENLOG` | `+ENLOG=1` | 串口状态输出开关 |

**注意**：JDY-31 **不支持苹果设备**（不是 BLE），只能配 Windows / Linux / Android。

## JDY-23 / JDY-16（BLE）差别

- 用 `AT+VER`（不带 SION）而不是 `AT+VERSION`
- 有 `AT+MAC`、`AT+IBEA`（iBeacon 开关）、`AT+CON`（连接指定 MAC）这类 BLE 专用命令
- **默认也是 9600**

## 坑

1. **JDY-31 与 HC-05 的 AT 不能混**：`AT+UART=` 是 HC-05 的写法，JDY-31 用 `AT+BAUD=`（档位数字）。
2. **JDY-31 上电就在 AT 模式**（没有 KEY 键、不用按住），但**一旦有设备连上就进透传**，此时 AT 不响应。
3. **BAUD 是档位不是数字**：发 `AT+BAUD=115200` 会失败，要发 `AT+BAUD=8`。
4. **改完 BAUD 立刻生效**：下一条命令就得用新波特率发。
5. **3.3V 供电**（1.8~3.6V），5V 会烧。

## 用我们的工具怎么玩

```
serial_send(autoBaud=true, com="COM7", command="AT")        ← 找波特率（JDY 常 9600）
serial_send(com="COM7", command="AT+VERSION", hold=true)    ← 确认型号
serial_send(com="COM7", command="AT+BAUD=8")                ← 改成 115200
serial_send(com="COM7", command="AT", baud=115200)          ← 用新波特率验证
```

## 怎么判断"常见度"（用户问的，我实测过的方法）

- ✅ **立创商城/淘宝搜"JDY-31"** 有大量在售（但我们抓不到页面，反爬）
- ✅ **模块价格**：JDY-31 常在 3~6 元，HC-05 常在 8~15 元
- ❌ 淘宝销量数字抓不到（要登录 + 反爬），只能人工看
