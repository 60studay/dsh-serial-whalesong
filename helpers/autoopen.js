/* 开关=开 时，"我在用串口干活"就顺手把网页开起来（只弹第一次）。
 *
 * 规矩（通讯板 12:48 / 12:49）：
 *   ① **不阻塞**：detached 子进程，不 await、不读输出、异常全吞。
 *      一次 serial_send 不能被开网页拖慢一毫秒（铁规则 R3）。
 *   ② **只弹第一次**：页面已经在跑就不动。走的是 `webctl --action open --open-browser`
 *      **不带 --force-browser**，复用路径因此不会弹（弹第二次就是用户骂过的"点一次弹一次"）。
 *   ③ **开关=关 就一点都不做**：读完偏好文件立刻返回，不 spawn、不检查、不建目录。
 *   ④ 取不到 `--scene` 就**不触发**（多场景时绝不猜）。
 *
 * 为什么挂在 runCli 上：它是所有工具的唯一咽喉，`argv[0]` 就是子命令，
 * 不用去改 send / read / control 三个工具。
 */

import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

/** 哪些子命令算"真的在动这块板子"。纯查询 / 后台观察 / 开关自身都不算。 */
export const AUTO_OPEN_SUBS = ['send', 'read', 'control'];

// 默认 `= {}`：调用点漏传参数时也不能抛。函数体里每句都在 try/catch 内，
// 唯独"形参解构"发生在 try 之前，对面拿 maybeAutoOpen(undefined) 试出来过。
export function maybeAutoOpen({ argv, python, env, entry, runtimeRoot } = {}) {
  try {
    if (!AUTO_OPEN_SUBS.includes(String(argv?.[0] ?? ''))) return;
    if (!readAutoOpen(runtimeRoot)) return;
    const scene = pickScene(argv);
    if (!scene) return;

    const child = spawn(
      python.file,
      [...(python.args ?? []), entry, 'webctl',
        '--action', 'open', '--scene', scene,
        // --reopen-if-idle：页面在跑但窗口被关了（看客心跳断）→ 把页面送回来。
        // 窗口还开着时它是空操作，所以不会"每条命令弹一个窗口"。
        '--open-browser', '--reopen-if-idle'],
      {
        cwd: path.dirname(entry),
        env,
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      },
    );
    // 起不来就算了：网页是锦上添花，绝不能连累这次串口调用。
    child.on('error', () => {});
    child.unref();
  } catch {
    /* 同上：这里任何异常都不许冒泡 */
  }
}

/** 读 <runtime_root>/ui-prefs.json。没写过 / 坏了 / 不是对象，都当"关"。 */
function readAutoOpen(runtimeRoot) {
  try {
    const raw = JSON.parse(
      readFileSync(path.join(runtimeRoot, 'ui-prefs.json'), 'utf8').replace(/^\uFEFF/, ''),
    );
    return raw?.auto_open_browser === true;
  } catch {
    return false;
  }
}

/** 从 argv 里原样取 `--scene` 的值；没有就返回空串（调用方据此不触发）。 */
function pickScene(argv) {
  const index = argv.indexOf('--scene');
  if (index < 0) return '';
  return String(argv[index + 1] ?? '').trim();
}
