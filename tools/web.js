/* 鲸歌工具层 · 网页服务（serial_web）。
 * 起/停/查后台网页实例。弹不弹浏览器由三层决定：显式参数 > 设置页开关 > 默认不弹。
 * 注意：这里**只**决定传哪个 flag；没显式要求时一个都不传，让 Python 去读开关。
 */

import { renderError } from '../helpers/errors.js';

export function register({ register, runCli, sceneParam, logRoot }) {
// ── serial_web ─────────────────────────────────────────────────────────────
register({
  name: 'serial_web',
  description:
    'Open, query, or close the visual web UI for a scene and return a clickable URL. ' +
    'Use it when the human asks to see the web page: you get the URL back immediately ' +
    '(the server runs in the background), and you can paste that URL to them. ' +
    'Opening twice is idempotent — an already running instance is reused, never duplicated. ' +
    'Closing really releases the port, so a serial terminal can use it right after.',
  parameters: {
    action: {
      type: 'string',
      required: true,
      enum: ['open', 'status', 'close'],
      description: 'open = start (or reuse) the web UI; status = is it running; close = stop it and free the port.',
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
        'Open the page in the human screen browser. Omit to follow the plugin setting ' +
        '"自动打开网页" (DSH left sidebar → Plugins → 鲸歌串口). That setting is OFF by default, ' +
        'so omitting usually just returns the URL. true forces a window open; false forbids it.',
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
      if (value.note) lines.push(`<note>${value.note}</note>`);
      return [{ type: 'text', text: lines.join('\n') }];
    },
  },
  isConcurrencySafe: () => false,
  async execute(args) {
    const action = String(args?.action ?? '').trim();
    if (!['open', 'status', 'close'].includes(action)) {
      throw new Error(`action 只能是 open / status / close，收到 ${action}`);
    }
    const argv = ['webctl', '--action', action];
    const scene = String(args?.scene ?? '').trim();
    if (scene) argv.push('--scene', scene);
    if (Number.isFinite(args?.port)) argv.push('--port', String(Math.trunc(args.port)));
    if (args?.mode === 'light' || args?.mode === 'watch') argv.push('--mode', args.mode);
    // 三个分支都要写全：false 和"不传"是两件事。
    // false → 显式禁止；不传 → 交给 Python 去读设置页的开关（默认关）。
    if (args?.openBrowser === true) argv.push('--open-browser');
    else if (args?.openBrowser === false) argv.push('--no-browser');
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
    if (data.note) result.note = data.note;
    return result;
  },
});
}
