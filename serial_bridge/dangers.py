# -*- coding: utf-8 -*-
"""危险命令护栏（2026-11-10，gcSTM32 反馈 3.1）。

## 为什么需要

HC-05 兼容固件的 `AT+BIND?` 不是"查询"：它会把 `+BIND=` 字面量写进绑定地址，
查一次坏一次，主机从此永远 CONNECTING。这类坑属于**设备知识**，不是通用规则 ——
所以规则也写在设备那一侧（场景 `config.json` 的 `dangerous_commands`），
`protocol.md` 里写人话版的原因。

## 两个来源，两种强度

| 位置 | 性质 | 谁读 |
|---|---|---|
| `protocols/<场景>/protocol.md` | **硬性要求**（人看的设备事实） | 人 / AI 读文档 |
| `protocols/<场景>/config.json` 的 `dangerous_commands` | **运行时闸门**（机器执行） | 插件在发送前拦一下 |

文档里说"别发"，代码里也真的拦 —— 只写文档的话，AI 不看就照发。

## 拦了之后怎么放行

调用方明确知道自己在做什么时加 `--yes`（工具：`confirm: true`）。
**不是永久禁止**，是"必须多一次确认"，因为有些场景确实需要发（例如恢复出厂）。
"""


def _normalize(text):
    return str(text or "").strip().upper()


def matching(cfg, command):
    """这条命令有没有命中场景里声明的危险命令。

    返回命中的那条规则 dict；没有命中返回 None。
    规则写法：`{"command": "AT+BIND?", "why": "...", "safer": "..."}`，
    `command` 做**前缀不敏感匹配**（`AT+BIND` 能盖住 `AT+BIND?` 和 `AT+BIND=xxx`）。
    """
    rules = (cfg or {}).get("dangerous_commands") or []
    if not isinstance(rules, (list, tuple)) or not command:
        return None
    text = _normalize(command)
    for rule in rules:
        if isinstance(rule, str):
            rule = {"command": rule}
        if not isinstance(rule, dict):
            continue
        target = _normalize(rule.get("command"))
        if not target:
            continue
        if text == target or text.startswith(target):
            return rule
    return None


def explain(rule):
    """把规则翻成一句给调用方看的话。"""
    rule = rule or {}
    why = str(rule.get("why") or "").strip()
    safer = str(rule.get("safer") or "").strip()
    text = "这条命令在这个设备上是危险的"
    if why:
        text += "：" + why
    if safer:
        text += "。替代做法：" + safer
    return text
