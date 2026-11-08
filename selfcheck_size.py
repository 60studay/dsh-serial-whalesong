# -*- coding: utf-8 -*-
"""结构体检自检：单文件行数超阈值就报出来。

为什么要这个脚本
----------------
代码会自己长大。没有机械拦截，"这次拆干净了"只能保证几个月。

这个项目已经拆过一次（`supervisor.py` 1262 → 836 行），但**没有守卫**，
于是它又慢慢回到 836，`index.js` 涨到 986、`web/index.html` 涨到 1019。

所以阈值不能靠自觉，要靠脚本。**超了就 FAIL，下次加功能前先处理。**

阈值（写死在这里，改阈值要说明理由）
------------------------------------
    > 400 行  → WARN  还行，但要说清为什么这么大
    > 600 行  → FAIL  必须体检，下次功能前处理
    > 800 行  → FAIL  必须拆，不许再往里加

这个脚本自己也有边界：它只量**行数**，不知道代码好坏。
行数少但职责混乱的文件它照样放过。所以它是**下限守卫**，不是质量证明。

用法：
    python selfcheck_size.py            # 严格：>600 就 FAIL
    python selfcheck_size.py --strict   # 更严：>400 就 FAIL
    python selfcheck_size.py --list     # 只列出各文件行数
"""

import io
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# 阈值：改这里等于改团队约定，改之前想清楚
WARN_LINES = 400
FAIL_LINES = 600
HARD_LINES = 800

# 倒计时（1.19）：距硬上限只剩这么点余量就判失败。
# 为什么不等撞到 800 才红：那时候下一个功能随手加 30 行就撞线，
# 根本没有"停下来拆"的余地。留 100 行缓冲，红的时候还站得住。
COUNTDOWN_MARGIN = 100

# 结构账本：把"600 行以上的文件"按天记一行，趋势要能看见（1.19）
LEDGER = os.path.join(HERE, "_需求文档", "结构账本.md")
LEDGER_MIN = 600
LEDGER_HEADER = """# 结构账本（`selfcheck_size.py` 自动追加，别手改）

> 每次跑自检，把 **600 行以上**的文件和行数记一行。
> 目的不是记账，是**看见趋势**：拆分有效就往下走，哪天悄悄涨回来，打开这一页就能发现。
> 同样的数字不会重复追加 —— 否则每跑一次自检就多一行，反而看不清趋势。

| 日期 | 600 行以上（行数） | 这些文件合计 |
|---|---|---|
"""

# 只量这些后缀
SUFFIXES = (".py", ".js", ".html", ".css")

# 这些目录整体跳过（第三方 / 生成物 / 归档）
SKIP_DIRS = {
    "vendor", "__pycache__", ".git", "_归档", "logs", ".runtime",
    "node_modules", "_testlogs", "_模块配置备份",
}

# 这些具体文件跳过（第三方代码的拷贝）
SKIP_FILES = {
    os.path.join("web", "index.html.bak"),
}

# 已知的、有明确理由的超标文件。
#
# 这是"棘轮"：**允许它现在这么大，但绝不许再涨**。
# 超过登记的行数 = FAIL。等你把它拆小了，把登记删掉。
#
# 为什么要有这个机制：如果自检永远红，人就会开始忽略它，
# 那守卫就白写了。所以给现状一个"合法的存量"，只拦新增。
#
# ⚠️ 往这里加新条目 = 承认你在欠债。加之前先问：能不能现在拆？
KNOWN_OVERSIZE = {
}

PASS = []
FAIL = []
WARN = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        return True
    FAIL.append("%s %s" % (name, detail))
    return False


def iter_source_files():
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(SUFFIXES):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, HERE)
            if rel in SKIP_FILES:
                continue
            yield rel, full


def count_lines(path):
    """数行数。读不了就返回 None（不要让一个坏文件把整个自检打死）。"""
    try:
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            return len(handle.read().splitlines())
    except Exception:
        return None


def record_ledger(rows):
    """把"600 行以上的文件 + 行数 + 日期"追加一行到《结构账本.md》（1.19）。

    同日同样的数字不重复追加：这个脚本每次跑都会调用，重复记录会把趋势冲淡。
    写失败不抛异常 —— 一个记账动作不该让守卫本身失败。
    """
    big = [(rel, n) for n, rel in rows if n >= LEDGER_MIN]
    if not big:
        return
    big.sort(key=lambda item: -item[1])
    summary = " / ".join("%s %d" % (rel, n) for rel, n in big)
    row = "| %s | %s | %d |" % (time.strftime("%Y-%m-%d"), summary,
                                sum(n for _, n in big))
    try:
        existing = ""
        if os.path.exists(LEDGER):
            with io.open(LEDGER, encoding="utf-8", errors="replace") as handle:
                existing = handle.read()
        if not existing.strip():
            existing = LEDGER_HEADER
        lines = [ln for ln in existing.splitlines() if ln.strip()]
        if lines and lines[-1] == row:
            print("  （结构账本：和上次一样，不重复记）")
            return
        with io.open(LEDGER, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(existing.rstrip("\n") + "\n" + row + "\n")
        print("  结构账本：+1 行（%d 个文件 ≥ %d 行）" % (len(big), LEDGER_MIN))
    except Exception as exc:      # 记账失败不影响守卫结论
        print("  （结构账本写不进去：%s）" % exc)


def main(argv):
    strict = "--strict" in argv
    list_only = "--list" in argv

    print("=" * 70)
    print("自检：文件行数（反屎山守卫）")
    print("=" * 70)

    rows = []
    unreadable = []
    for rel, full in iter_source_files():
        lines = count_lines(full)
        if lines is None:
            unreadable.append(rel)
            continue
        rows.append((lines, rel.replace("\\", "/")))

    rows.sort(reverse=True)

    if list_only:
        print("\n%-6s %s" % ("行数", "文件"))
        for lines, rel in rows:
            print("%-6d %s" % (lines, rel))
        print("\n合计 %d 个文件，%d 行" % (len(rows), sum(n for n, _ in rows)))
        return 0

    print("\n共 %d 个源文件，%d 行" % (len(rows), sum(n for n, _ in rows)))

    # ── 1. 所有文件都读得出来 ─────────────────────────────────────────────
    print("\n[可读性]")
    check("所有源文件可读", not unreadable,
          "读不了：%s" % unreadable[:5])

    # ── 2. 硬上限：>800 必须拆（存量登记过的走棘轮，不许涨）─────────────
    print("\n[硬上限 %d 行：必须拆]" % HARD_LINES)
    hard = []
    for lines, rel in rows:
        if lines <= HARD_LINES:
            continue
        registered = KNOWN_OVERSIZE.get(rel)
        if registered is None:
            hard.append((lines, rel))
            print("  ✗ %-32s %d 行  （没登记，新超标）" % (rel, lines))
            continue
        budget = int(registered.get("lines") or 0)
        if lines > budget:
            hard.append((lines, rel))
            print("  ✗ %-32s %d 行  （登记 %d，涨了 %d）"
                  % (rel, lines, budget, lines - budget))
        else:
            print("  ~ %-32s %d 行  （存量登记 %d，没涨）" % (rel, lines, budget))
    check("没有新增或变大的超标文件", not hard,
          "超标：%s" % ", ".join("%s(%d)" % (r, n) for n, r in hard[:6]))

    # ── 2b. 倒计时：离硬上限只剩这么点余量就判 FAIL（1.19）───────────────
    print("\n[倒计时：离 %d 行的硬上限还剩多少]" % HARD_LINES)
    countdown = []
    for lines, rel in rows:
        if rel in KNOWN_OVERSIZE or lines > HARD_LINES:
            continue
        left = HARD_LINES - lines
        if left < COUNTDOWN_MARGIN:
            countdown.append((lines, rel, left))
            print("  ✗ %-32s %d 行（只剩 %d 行余量）" % (rel, lines, left))
        elif left < COUNTDOWN_MARGIN * 2:
            print("  ! %-32s %d 行（剩 %d 行）" % (rel, lines, left))
    if not countdown:
        print("  （都对硬上限还有 %d 行以上余量）" % COUNTDOWN_MARGIN)
    check("没有文件逼近硬上限（余量 < %d 行）" % COUNTDOWN_MARGIN, not countdown,
          "逼近：%s" % ", ".join("%s(%d)" % (r, n) for n, r, _ in countdown[:6]))

    # ── 2c. 结构账本：600 行以上的文件按天记一行，趋势要能看见 ────────────
    print("\n[结构账本]")
    record_ledger(rows)

    # ── 2d. 棘轮：登记的行数只许降不许升 ────────────────────────────────
    print("\n[棘轮：登记额度要写清理由]")
    for rel, info in sorted(KNOWN_OVERSIZE.items()):
        has_reason = bool(str(info.get("reason") or "").strip())
        has_budget = int(info.get("lines") or 0) > 0
        print("  %s %-30s 额度 %s 行" % ("✓" if has_reason and has_budget else "✗",
                                        rel, info.get("lines")))
        check("登记 %s 写了理由和额度" % rel, has_reason and has_budget,
              "reason=%r lines=%r" % (info.get("reason"), info.get("lines")))

    # ── 3. 失败线：>600 必须体检 ─────────────────────────────────────────
    print("\n[体检线 %d 行：下次功能前处理]" % FAIL_LINES)
    over = [(n, r) for n, r in rows
            if FAIL_LINES < n <= HARD_LINES and r not in KNOWN_OVERSIZE]
    # 已登记的老账不重复算进"体检线"，它已经在硬上限段里走棘轮了
    over = [(n, r) for n, r in over if r not in KNOWN_OVERSIZE]
    for lines, rel in over:
        print("  ! %-32s %d 行" % (rel, lines))
    if strict:
        check("没有文件超过 %d 行（严格模式）" % FAIL_LINES,
              not (hard or over),
              "超标 %d 个" % len(hard + over))
    else:
        # 默认模式下，600~800 只是记录，不算失败 —— 但会打印出来提醒
        if over:
            print("  （记录在案，不算失败；下次动这些文件前先体检）")

    # ── 4. 提示线：>400 有理由吗 ─────────────────────────────────────────
    print("\n[提示线 %d 行]" % WARN_LINES)
    warn = [(n, r) for n, r in rows if WARN_LINES < n <= FAIL_LINES]
    for lines, rel in warn:
        print("  · %-32s %d 行" % (rel, lines))
    if not warn:
        print("  （无）")

    return summary(len(over), len(hard))


def summary(over_count, hard_count):
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n结构体检没通过：")
        for item in FAIL:
            print("  · " + item)
    elif over_count:
        print("通过，但有 %d 个文件在体检线以上 —— 下次动它们之前先体检。" % over_count)
    else:
        print("所有文件都在体检线以下 ✅")
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:
        # 和别的自检一样：宁可报错，也不要只留一个 traceback
        print("\n结构自检没能跑起来：%s: %s" % (type(exc).__name__, exc))
        print("这本身就是失败。")
        sys.exit(1)