# -*- coding: utf-8 -*-
"""自检：会开串口的子命令，是否都在"自动弹页面"白名单里（1.87）。

## 为什么要有这条

2026-11-11 加了 `loopback` 子命令（会开两个串口），但忘了加进
`helpers/autoopen.js` 的 `AUTO_OPEN_SUBS` —— 于是"用串口就自动弹页面"
对环回静默失效。对面上次就是这么发现的。

这类漏法有个共同点：**新增一个子命令 → 只改一边**。所以这里做**双向比对**：

```
bridge.py 里"会开串口"的子命令  ⊆  AUTO_OPEN_SUBS
```

哪些算"会开串口"：CLI 里显式列出来的。为了不靠猜，这里用两张表：
  · 白名单：打开串口 / 写串口的子命令（人工维护，新增子命令时改这里）
  · 例外：明确不需要弹页面的（例如 `close` 只释放、`prefs` 只读写配置）

任一边新增而另一边没跟上 → 红。
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "serial_bridge", "cli.py")
AUTO = os.path.join(HERE, "helpers", "autoopen.js")

# 这些子命令会**打开串口**（新增时必须同步 AUTO_OPEN_SUBS）
TOUCHES_SERIAL = {
    "send": True,
    "read": True,
    "control": True,        # 取描述/传输信息时会解析串口
    "loopback": True,
    "watch": True,          # 监视模式占口
    "daemon": True,         # 管家持口
    "web": True,            # 网页实例持口（但它自己会开页面，见 SELF_OPENS_PAGE）
}

# 明确不需要弹页面（也从不碰串口）
NO_SERIAL = {"scan", "info", "status", "close", "prefs"}
# 自己就会开页面（不需要"用串口顺手弹一张"）
SELF_OPENS_PAGE = {"web", "webctl"}


def cli_subcommands():
    """从 build_parser 里把所有子命令名抓出来。"""
    with open(CLI, encoding="utf-8") as handle:
        text = handle.read()
    names = set(re.findall(r'add_parser\("([a-z_]+)"\)', text))
    # 只读类（cli_query）与网页那组（cli_web）在别的文件里注册
    for extra in ("cli_query.py", "cli_web.py"):
        path = os.path.join(HERE, "serial_bridge", extra)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                names |= set(re.findall(r'add_parser\("([a-z_]+)"\)', handle.read()))
    return names


def auto_subs():
    with open(AUTO, encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r"AUTO_OPEN_SUBS\s*=\s*\[([^\]]*)\]", text)
    if not match:
        return set()
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def main():
    print("=" * 70)
    print("自检：会开串口的子命令都在自动弹页面白名单里")
    print("=" * 70)
    subs = cli_subcommands()
    white = auto_subs()
    print("子命令（parser 里）: %s" % "、".join(sorted(subs)) or "(没抓到)")
    print("AUTO_OPEN_SUBS     : %s" % "、".join(sorted(white)))

    fails = 0
    unknown = sorted(s for s in subs
                     if s not in TOUCHES_SERIAL and s not in NO_SERIAL
                     and s not in SELF_OPENS_PAGE)
    for name in unknown:
        fails += 1
        print("  FAIL 新子命令 %r 没分类：请决定它是否开串口，"
              "并更新本自检的 TOUCHES_SERIAL / NO_SERIAL" % name)

    for name in sorted(TOUCHES_SERIAL):
        if name in SELF_OPENS_PAGE:
            print("  PASS %-10s 自己就会开页面（不算漏）" % name)
            continue
        if name not in white:
            fails += 1
            print("  FAIL %r 会开串口，但不在 AUTO_OPEN_SUBS 里（用串口不会弹页面）" % name)
        else:
            print("  PASS %-10s 在白名单里" % name)

    for name in sorted(white - set(TOUCHES_SERIAL) - NO_SERIAL - SELF_OPENS_PAGE):
        fails += 1
        print("  FAIL 白名单里的 %r 既不在 TOUCHES_SERIAL 也不在 NO_SERIAL"
              "（要么是错的，要么本自检该更新）" % name)

    print("=" * 70)
    total = len(TOUCHES_SERIAL) + len(unknown)
    print("PASS %d / FAIL %d" % (max(0, total - fails), fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
