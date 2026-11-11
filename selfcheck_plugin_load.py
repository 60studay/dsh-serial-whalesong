# -*- coding: utf-8 -*-
"""自检：插件能不能被 DSH 真正加载（1.86）。

## 为什么要有这一条

2026-11-11 的 P0：`serial_loopback` 的 `render` 写在了 `output` **外面**，
DSH 加载时直接拒绝整个插件：

```
tool "serial_loopback" must declare output { schema, render }
```

后果是**11 个工具全消失**、自动开网页也失效 —— 而当时的自检全是绿的，
因为它们只检查"文件语法对不对"，不检查"DSH 收不收"。

## 做法

用 DSH 自带的 node 真跑一次 `index.js` 的 `apply()`：
逐个工具断言 `output` 里有 `schema` 和 `render`，且 `execute` 是函数。
故意把 `render` 挪出去 → 这里必须变红。

不依赖 DSH 运行时内部结构：只在 `apply()` 外面套一层假的 `ctx`
（`tools.register` 收工具、`inject` 收依赖），够用且稳定。
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "index.js")

NODE_CANDIDATES = [
    os.environ.get("DSH_NODE", ""),
    r"C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
    shutil.which("node") or "",
]


def find_node():
    for item in NODE_CANDIDATES:
        if item and os.path.isfile(item):
            return item
    return ""


HARNESS = r"""
import { pathToFileURL } from 'node:url';

const pluginPath = process.argv[2];
const tools = [];
const ctx = {
  tools: { register: (def) => tools.push(def) },
  inject: () => {},
  logger: { info() {}, warn() {}, error() {} },
  on: () => {},
  // 插件用 ctx.effect(() => ctx.tools.register(tool)) 注册工具：
  // 假 ctx 必须**真的执行**回调，否则一个工具都收不到（第一版就踩了这个）
  effect: (fn) => { try { fn(); } catch (error) { console.error('effect failed: ' + error.message); } return () => {}; },
};
const mod = await import(pathToFileURL(pluginPath).href);
const apply = mod.apply || (mod.default && mod.default.apply);
if (typeof apply !== 'function') {
  console.log(JSON.stringify({ ok: false, reason: 'no apply()' }));
  process.exit(0);
}
try {
  await apply(ctx, {});
} catch (error) {
  console.log(JSON.stringify({ ok: false, reason: 'apply() threw: ' + (error && error.message) }));
  process.exit(0);
}
console.log(JSON.stringify({
  ok: true,
  tools: tools.map((tool) => ({
    name: String(tool && tool.name || ''),
    hasExecute: typeof (tool && tool.execute) === 'function',
    hasOutput: !!(tool && tool.output && typeof tool.output === 'object'),
    hasSchema: !!(tool && tool.output && tool.output.schema),
    hasRender: !!(tool && tool.output && typeof tool.output.render === 'function'),
  })),
}));
"""


def main():
    print("=" * 70)
    print("自检：插件能不能被 DSH 加载")
    print("=" * 70)
    node = find_node()
    if not node:
        print("  SKIP 找不到 node（设 DSH_NODE 或装 node）")
        print("=" * 70)
        return 0
    if not os.path.isfile(PLUGIN):
        print("  FAIL 找不到 index.js")
        print("=" * 70)
        print("PASS 0 / FAIL 1")
        return 1

    script = os.path.join(HERE, "_plugin_load_probe.mjs")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(HARNESS)
    try:
        proc = subprocess.run([node, script, PLUGIN], capture_output=True,
                              text=True, encoding="utf-8", timeout=60,
                              cwd=HERE)
    finally:
        try:
            os.remove(script)
        except OSError:
            pass

    out = (proc.stdout or "").strip().splitlines()
    payload = {}
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if not payload:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        print("  FAIL apply() 没跑起来：%s" % (detail[-1] if detail else "无输出"))
        print("=" * 70)
        print("PASS 0 / FAIL 1")
        return 1
    if not payload.get("ok"):
        print("  FAIL %s" % payload.get("reason"))
        print("=" * 70)
        print("PASS 0 / FAIL 1")
        return 1

    tools = payload.get("tools") or []
    fails = 0
    if not tools:
        print("  FAIL apply() 一个工具都没注册")
        fails += 1
    print("[工具] 共 %d 个" % len(tools))
    for tool in tools:
        problems = []
        if not tool.get("hasOutput"):
            problems.append("缺 output")
        else:
            if not tool.get("hasSchema"):
                problems.append("output 里缺 schema")
            if not tool.get("hasRender"):
                problems.append("output 里缺 render（写在外面 = DSH 拒绝加载整个插件）")
        if not tool.get("hasExecute"):
            problems.append("缺 execute")
        if problems:
            fails += 1
            print("  FAIL %-16s %s" % (tool.get("name"), "；".join(problems)))
        else:
            print("  PASS %-16s output{schema,render} + execute" % tool.get("name"))

    print("=" * 70)
    print("PASS %d / FAIL %d" % (max(0, len(tools) - fails), fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
