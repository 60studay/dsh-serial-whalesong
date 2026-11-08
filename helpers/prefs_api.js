/* 设置页的 HTTP 端点：GET / POST /api/whalesong/prefs
 *
 * 为什么需要它：DSH 的插件详情页（`plugins.bundle.config` 插槽）跑在浏览器里，
 * 只能 fetch，跑不了 CLI；而偏好文件由 Python 内核统一读写（一份实现、原子写）。
 * 这个端点只做一件事：把请求转成 `bridge.py prefs`，把 JSON 原样回给面板。
 *
 * 三条防线：
 *   ① 没有 webServer 服务（CLI 模式）就静默跳过，不影响插件其余功能；
 *   ② 同一路径只注册一次——插件重载/多实例重放时重复注册会把 web 服务搞崩；
 *   ③ 任何异常都回 500 + 原因，绝不让面板"看起来保存成功了"其实没写进去。
 */

const ROUTE = '/api/whalesong';
const MAX_BODY = 8 * 1024;

// 模块级守卫：插件被 apply 两次（重载/多实例）时第二次直接跳过。
let claimed = false;

export function registerPrefsApi(ctx, { runCli }) {
  // 不能用 `if (ctx.webServer)` 探一下：cordis 对没 inject 的服务直接抛
  // "cannot get property without inject"，插件会整个起不来（实测踩过）。
  // `ctx.inject(deps, cb)` 才是"这些服务就绪后再跑"的正规写法——
  // 命令行宿主里永远没有 webServer，这段就一直挂着，不影响串口功能。
  if (typeof ctx.inject !== 'function') return;

  ctx.inject(['webServer'], (scoped) => {
    if (claimed) return;
    claimed = true;

    const handler = async (req, res) => {
      try {
        const url = new URL(String(req.url ?? '/'), 'http://127.0.0.1');
        if (url.pathname !== `${ROUTE}/prefs`) {
          return send(res, 404, { ok: false, error: '没有这个接口' });
        }
        // 防跨站：浏览器同源请求会带 same-origin / same-site；别的站点来的直接拒。
        const site = String(req.headers?.['sec-fetch-site'] ?? '');
        if (site && site !== 'same-origin' && site !== 'same-site' && site !== 'none') {
          return send(res, 403, { ok: false, error: '只接受本页面发来的请求' });
        }

        if (req.method === 'GET') {
          return send(res, 200, { ok: true, data: await runCli(['prefs']) });
        }
        if (req.method === 'POST') {
          const body = await readJsonBody(req);
          if (body === undefined) {
            return send(res, 400, { ok: false, error: '请求体必须是 JSON 对象' });
          }
          if (typeof body.auto_open_browser !== 'boolean') {
            return send(res, 400, {
              ok: false,
              error: 'auto_open_browser 必须是 true / false',
            });
          }
          const value = body.auto_open_browser ? 'true' : 'false';
          const data = await runCli(['prefs', '--set', `auto_open_browser=${value}`]);
          return send(res, 200, { ok: true, data });
        }
        return send(res, 405, { ok: false, error: '只支持 GET / POST' });
      } catch (error) {
        // 把 Python 的结构化错误原文带出去，让设置页能显示"为什么没存上"。
        return send(res, 500, { ok: false, error: String(error?.message ?? error) });
      }
    };

    scoped.effect(() => {
      const dispose = scoped.webServer.register({ kind: 'prefix', path: ROUTE, handler });
      return () => {
        claimed = false;
        if (typeof dispose === 'function') dispose();
      };
    }, '鲸歌：设置页接口 /api/whalesong/prefs');
  });
}

function send(res, status, payload) {
  try {
    const text = JSON.stringify(payload);
    res.statusCode = status;
    res.setHeader('content-type', 'application/json; charset=utf-8');
    res.setHeader('cache-control', 'no-store');
    res.end(text);
  } catch {
    try { res.end('{"ok":false,"error":"响应序列化失败"}'); } catch { /* 连接已断 */ }
  }
}

/** 读 JSON 请求体。不是对象 / 太大 / 不是 JSON 一律返回 undefined，交给上层报 400。 */
function readJsonBody(req) {
  return new Promise((resolve) => {
    let size = 0;
    const chunks = [];
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      resolve(value);
    };
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        finish(undefined);
        return;
      }
      chunks.push(chunk);
    });
    req.on('error', () => finish(undefined));
    req.on('end', () => {
      try {
        const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
        finish(parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : undefined);
      } catch {
        finish(undefined);
      }
    });
  });
}
