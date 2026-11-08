/* 鲸歌工具层 · 网页服务（serial_web）。
 * 起/停/查后台网页实例。
 *
 * 弹窗规矩（通讯板 12:49 定的唯一含义）：
 *   · **用户亲口说要开网页** → 传 openBrowser=true → 无条件弹（页面在跑也弹）。
 *   · 其他情况（不传 / false）→ 都不弹，只回 URL。
 *   · 「开关=开 时用串口就自动弹」那条不在这里，在 helpers/autoopen.js（runCli 咽喉上）。
 */

import { renderError } from '../helpers/errors.js';

export function register({ register, runCli, sceneParam, logRoot }) {
// ── serial_web ─────────────────────────────────────────────────────────────
register({
  name: 'serial_web',
  description:
    'Open, query, close, or reap the visual web UI for a scene and return a clickable URL. ' +
    'Use it when the human asks to see the web page: you get the URL back immediately ' +
    '(the server runs in the background), and you can paste that URL to them. ' +
    'Opening twice is idempotent — an already running instance is reused, never duplicated. ' +
    'Closing really releases the port, so a serial terminal can use it right after. ' +
    'action=reap deletes leftover state files of instances that already died ' +
    '(use it when the human says stale pages/《失效》tabs are piling up).',
  parameters: {
    action: {
      type: 'string',
      required: true,
      enum: ['open', 'status', 'close', 'reap'],
      description:
        'open = start (or reuse) the web UI; status = is it running; ' +
        'close = stop it and free the port; reap = delete state files of dead instances ' +
        '(no scene needed, reports how many were cleaned).',
    },
    scene: { ...sceneParam, description: 'Scene name. Omit when there is only one scene.' },
    port: { type: 'number', description: 'Preferred HTTP port. Omit to auto-pick from 8898 upward.' },
    mode: {
      type: 'string',
      enum: ['light', 'watch'],
      description: 'light (default) does not hold the port; watch holds it to stream continuously.',
    },
    openBrowser: {
      type: 'boolean',
      description:
        'Pass true ONLY when the human explicitly asked to open the web page ("帮我开网页"). ' +
        'true always pops a window, even if the page is already running. ' +
        'Omit or pass false to just return the URL; the plugin setting "自动打开网页" ' +
        '(DSH left sidebar → Plugins → 鲸歌串口) governs the automatic case, not this parameter.',
    },
  },
  output: {
    schema: {
      type: 'object',
      additionalProperties: false,
      required: ['action', 'running'],
      properties: {
        action: { type: 'string' },
        scene: { type: 'string' },
        running: { type: 'boolean' },
        url: { type: 'string', description: 'Clickable URL to show the human.' },
        port: { type: 'integer' },
        pid: { type: 'integer' },
        mode: { type: 'string' },
        alreadyRunning: { type: 'boolean' },
        stopped: { type: 'boolean' },
        released: { type: 'boolean', description: 'True when the port is really free again.' },
        reaped: {
          type: 'array',
          items: { type: 'string' },
          description: 'Scenes whose dead instance state files were deleted (action=reap).',
        },
        kept: {
          type: 'array',
          items: { type: 'string' },
          description: 'Scenes whose instance is still alive (action=reap).',
        },
        note: { type: 'string' },
      },
    },
    render: (_args, value) => {
      const lines = [`<web action="${value.action}" running="${value.running}"/>`];
      if (value.url) lines.push(`<url>${value.url}</url>`);
      if (value.port) lines.push(`<port>${value.port}</port>`);
      if (value.mode) lines.push(`<mode>${value.mode}</mode>`);
      if (value.alreadyRunning) lines.push('<already_running>true</already_running>');
      if (value.released !== undefined) lines.push(`<released>${value.released}</released>`);
      if (value.reaped) lines.push(`<reaped count="${value.reaped.length}">${value.reaped.join(' ')}</reaped>`);
      if (value.kept) lines.push(`<kept count="${value.kept.length}">${value.kept.join(' ')}</kept>`);
      if (value.note) lines.push(`<note>${value.note}</note>`);
      return [{ type: 'text', text: lines.join('\n') }];
    },
  },
  isConcurrencySafe: () => false,
  async execute(args) {
    const action = String(args?.action ?? '').trim();
    if (!['open', 'status', 'close', 'reap'].includes(action)) {
      throw new Error(`action 只能是 open / status / close / reap，收到 ${action}`);
    }
    const argv = ['webctl', '--action', action];
    const scene = String(args?.scene ?? '').trim();
    if (scene) argv.push('--scene', scene);
    if (Number.isFinite(args?.port)) argv.push('--port', String(Math.trunc(args.port)));
    if (args?.mode === 'light' || args?.mode === 'watch') argv.push('--mode', args.mode);
    // 显式 true = 用户亲口说要 → 无条件弹（页面在跑也弹，所以要 --force-browser）。
    // false 和"不传"是同一条路：都不弹，只回 URL。开关不管这条路。
    if (args?.openBrowser === true) argv.push('--open-browser', '--force-browser');
    else argv.push('--no-browser');
    const data = await runCli(argv);

    const result = { action, running: Boolean(data.running) };
    if (data.scene) result.scene = data.scene;
    if (data.url) result.url = data.url;
    if (data.port) result.port = data.port;
    if (data.pid) result.pid = data.pid;
    if (data.mode) result.mode = data.mode;
    if (data.already_running !== undefined) result.alreadyRunning = Boolean(data.already_running);
    if (data.stopped !== undefined) result.stopped = Boolean(data.stopped);
    if (data.released !== undefined) result.released = Boolean(data.released);
    if (Array.isArray(data.reaped)) result.reaped = data.reaped.map(String);
    if (Array.isArray(data.kept)) result.kept = data.kept.map(String);
    if (data.note) result.note = data.note;
    if (action === 'reap') {
      const count = result.reaped ? result.reaped.length : 0;
      const alive = result.kept ? result.kept.length : 0;
      result.note = count
        ? `收掉 ${count} 个死实例${alive ? `，另有 ${alive} 个还活着` : ''}`
        : `没有死实例${alive ? `，${alive} 个还活着` : ''}`;
    }
    return result;
  },
});
}
