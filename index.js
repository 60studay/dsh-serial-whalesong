/**
 * dsh-whalesong（鲸歌）v2 —— 通用串口工具插件。
 *
 * 设计要点：
 * - 不常驻、不占口：每次调用临时打开串口，发完就读回显并关闭。
 * - 逻辑全在 Python 内核（serial_bridge/）：匹配、错误分类、判据引擎、控制权状态。
 *   这里只做参数编排、结果整形、以及把结构化错误翻译成模型能直接照着行动的提示。
 * - 零设备知识：本文件不认识任何具体命令字/故障字，一切来自场景配置。
 *
 * 每个"场景"= protocols/<场景名>/ 下的 config.json（串口、匹配规则、watch 块）+ protocol.md。
 */
import { execFile } from 'node:child_process';
import { promises as fsp, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

import { makeEnv, readJson } from './helpers/env.js';
import { renderError } from './helpers/errors.js';
import { maybeAutoOpen } from './helpers/autoopen.js';
import { registerPrefsApi } from './helpers/prefs_api.js';
import { register as registerSerialTools } from './tools/serial.js';
import { register as registerControlTools } from './tools/control.js';
import { register as registerDaemonTools } from './tools/daemon.js';
import { register as registerWebTools } from './tools/web.js';

const execFileAsync = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENTRY = path.join(HERE, 'bridge.py');
const VENDOR = path.join(HERE, 'vendor', 'pyserial-3.5');
const BUNDLED_PROTOCOLS = path.join(HERE, 'protocols');

export const name = 'whalesong';
// 只声明 tools：webServer 在命令行宿主里可能不存在，硬写进来会让插件起不来。
// 设置页接口那边用 `ctx.inject(['webServer'], ...)` 等它就绪（没有就永远挂着）。
export const inject = ['tools'];

const DEFAULT_PYTHON = process.env.DSH_PYTHON || 'python';
// 日志默认写到插件自己的目录下（跟着插件走，不碰用户家目录，也不暴露用户名）。
// 需要换位置就设环境变量 SERIAL_BRIDGE_LOG_DIR。
const DEFAULT_LOG_ROOT =
  process.env.SERIAL_BRIDGE_LOG_DIR || path.join(HERE, 'logs');
const CALL_TIMEOUT_MS = 25000;

/** 结构化错误的 kind → 给模型的一句话行动建议（与 kind 一一对应，不含设备语义）。 */

export function apply(ctx, config = {}) {
  const configuredPython =
    typeof config.pythonPath === 'string' && config.pythonPath.trim() !== ''
      ? config.pythonPath.trim()
      : DEFAULT_PYTHON;

  // 1.54：候选顺序**按"可靠程度"排**，并且跳过 Windows 商店别名的空壳 python.exe
  // （`C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\python.exe` 是个转发壳：
  //  启动时可能弹一个以 `C:\Users\...` 为标题的控制台窗口，而且经常没有 serial 模块）。
  const pythonCandidates = [];
  const bundledPython = process.env.DSH_HOME
    ? path.join(process.env.DSH_HOME, 'dsh-runtimes', 'dsh-primary-runtime',
      'dependencies', 'python', 'python.exe')
    : '';
  const explicitlyConfigured =
    typeof config.pythonPath === 'string' && config.pythonPath.trim() !== '';
  if (explicitlyConfigured) pythonCandidates.push({ file: configuredPython, args: [] });
  if (bundledPython) pythonCandidates.push({ file: bundledPython, args: [] });
  pythonCandidates.push({ file: 'py', args: ['-3'] }, { file: 'python', args: [] });
  if (!explicitlyConfigured && !bundledPython) {
    // 配置和内置都没有时才把 `python` 放最前（总得有一个能跑的）
    pythonCandidates.unshift({ file: 'python', args: [] });
  }

  // logRoot 的决定顺序：插件配置 > 插件自带 config.json 的 log_root > 默认值。
  // 读 config.json 是为了让"插件进程"和"手动启动的网页"用同一个目录——
  // 否则两边各写一份 state，控制权就同步不上（Bug 3）。
  const bundledCommon = (() => {
    try {
      return JSON.parse(readFileSync(path.join(HERE, 'config.json'), 'utf8').replace(/^\uFEFF/, '')) ?? {};
    } catch {
      return {};
    }
  })();

  const bundledLogRoot =
    typeof bundledCommon?.log_root === 'string' ? bundledCommon.log_root.trim() : '';

  const logRoot =
    typeof config.logRoot === 'string' && config.logRoot.trim() !== ''
      ? config.logRoot.trim()
      : (bundledLogRoot || DEFAULT_LOG_ROOT);

  // runtime 目录：必须和 Python 的 cli._paths 同一套规则（env > config.json > <log_root>/.runtime），
  // 否则"开关=开 时自动开页面"会去读另一个文件，永远读到默认值。
  const bundledRuntimeRoot =
    typeof bundledCommon?.runtime_root === 'string' ? bundledCommon.runtime_root.trim() : '';
  const runtimeRoot =
    process.env.SERIAL_BRIDGE_RUNTIME_DIR || bundledRuntimeRoot || path.join(logRoot, '.runtime');

  const configuredProtocolRoot =
    typeof config.protocolRoot === 'string' && config.protocolRoot.trim() !== ''
      ? config.protocolRoot.trim()
      : undefined;

  const state = { python: undefined, protocolRoot: configuredProtocolRoot };

  async function resolveProtocolRoot() {
    if (state.protocolRoot) {
      const ok = await fsp.stat(state.protocolRoot).then((s) => s.isDirectory()).catch(() => false);
      if (ok) return state.protocolRoot;
    }
    state.protocolRoot = BUNDLED_PROTOCOLS;
    return state.protocolRoot;
  }

  async function resolvePython(env) {
    if (state.python) return state.python;

    // 1.54：**解释器要缓存到磁盘**，下次启动不再探测（每次调用都探测 = 每次都可能闪窗）
    const cachePath = path.join(runtimeRoot, 'python-cache.json');
    try {
      const cached = JSON.parse(readFileSync(cachePath, 'utf8').replace(/^\uFEFF/, ''));
      if (cached && typeof cached.file === 'string' && cached.file) {
        state.python = { file: cached.file, args: Array.isArray(cached.args) ? cached.args : [],
          version: String(cached.version || '') };
        return state.python;
      }
    } catch { /* 第一次运行没有缓存，正常 */ }

    const failures = [];
    for (const candidate of pythonCandidates) {
      if (/[\\/]WindowsApps[\\/]/i.test(String(candidate.file))) {
        failures.push(`${candidate.file}: 跳过（Windows 商店别名，会弹空控制台窗口）`);
        continue;
      }
      try {
        const { stdout } = await execFileAsync(
          candidate.file,
          [...candidate.args, '-c',
            'import sys, serial; print(sys.version.split()[0] + " serial " + serial.VERSION)'],
          { env, encoding: 'utf8', timeout: 10000, windowsHide: true, maxBuffer: 1 << 20 },
        );
        state.python = { ...candidate, version: String(stdout).trim() };
        try {
          await fsp.mkdir(runtimeRoot, { recursive: true });
          await fsp.writeFile(cachePath,
            JSON.stringify({ file: candidate.file, args: candidate.args,
              version: state.python.version }, null, 2), 'utf8');
        } catch { /* 缓存写不进去不影响使用 */ }
        return state.python;
      } catch (error) {
        const detail = String(error?.stderr ?? error?.message ?? '').trim().split(/\r?\n/)[0];
        failures.push(`${candidate.file}${candidate.args.length ? ' ' + candidate.args.join(' ') : ''}: ${detail || '不可用'}`);
      }
    }
    throw new Error(
      '找不到可用的 Python 解释器（需要 Python 3.8+）。\n' +
        `已尝试：\n  ${failures.join('\n  ')}\n` +
        '可在插件配置里用 pythonPath 指定解释器绝对路径。',
    );
  }

  /**
   * 跑一次 Python 内核，返回 data。
   * 内核总是输出一行 JSON：{ok:true,data} 或 {ok:false,error}。
   * 非零退出码不代表"调用失败"，只代表"业务失败"——两者都解析 JSON。
   */
  async function runCli(argv) {
    const protocolRoot = await resolveProtocolRoot();
    let env = makeEnv({ logRoot, protocolRoot, vendor: VENDOR });
    const python = await resolvePython(env);
    // 1.54：把"这次用的是哪个解释器"告诉内核，`serial_status` 会带出来
    // （用户排查"黑窗标题是 C:\Users\..."时，第一件事就是确认到底跑了哪个 python）
    env = { ...env, SERIAL_BRIDGE_PYTHON: python.file };

    // 「开关=开 时用串口就自动开页面」挂在这里：runCli 是所有工具的唯一咽喉。
    // 里面不 await、不读输出、异常全吞——一次 serial_send 不能被它拖慢一毫秒。
    maybeAutoOpen({ argv, python, env, entry: ENTRY, runtimeRoot });

    let stdout = '';
    let stderr = '';
    let timedOut = false;
    try {
      const out = await execFileAsync(python.file, [...python.args, ENTRY, ...argv],
        { env, encoding: 'utf8', timeout: CALL_TIMEOUT_MS, windowsHide: true, maxBuffer: 8 << 20 });
      stdout = String(out.stdout ?? '');
      stderr = String(out.stderr ?? '');
    } catch (error) {
      stdout = String(error?.stdout ?? '');
      stderr = String(error?.stderr ?? '');
      timedOut = Boolean(error?.killed);
      if (!stdout.trim()) {
        if (timedOut) {
          throw new Error(`鲸歌调用超时（${CALL_TIMEOUT_MS} ms）：串口可能被占用，或设备没有回应`);
        }
        const detail = stderr.trim() || String(error?.message ?? error);
        throw new Error(`鲸歌启动失败：${detail.split(/\r?\n/).slice(-4).join('\n')}`);
      }
    }

    const line = stdout.trim().split(/\r?\n/).filter((item) => item.trim() !== '').pop() ?? '';
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      throw new Error(`鲸歌返回了非 JSON 输出：${line.slice(0, 400)}`);
    }
    if (!parsed?.ok) {
      const error = parsed?.error ?? {};
      const failure = new Error(renderError(error.kind, error, '鲸歌返回失败'));
      failure.kind = error.kind;
      throw failure;
    }
    return parsed.data;
  }

  function register(tool) {
    ctx.effect(() => {
      const disposer = ctx.tools.register(tool);
      return typeof disposer === 'function' ? disposer : () => {};
    });
  }

  const sceneParam = {
    type: 'string',
    description:
      'Scene name: the folder under the protocols directory (for example "蓝牙AT"). ' +
      'Use serial_list to see the configured scenes.',
  };


  // 各工具组的注册都走同一个 ctx：它们只拿到 runCli / register / sceneParam / logRoot，
  // 互相之间不共享任何状态（每个 execute 独立跑一次 CLI）。
  const toolCtx = { register, runCli, sceneParam, logRoot };
  registerSerialTools(toolCtx);
  registerControlTools(toolCtx);
  registerDaemonTools(toolCtx);
  registerWebTools(toolCtx);

  // 设置页那一个开关的后端入口（没有 webServer 的宿主里自动跳过）。
  registerPrefsApi(ctx, { runCli });
}
