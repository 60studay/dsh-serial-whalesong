/**
 * 看客心跳 + 失效自救（1.19 从 app.js 摘出来，本来 115 行）
 *
 * 服务活着：每 5 秒 ping 一次，别的一概不多做（不加横幅、不改标题）。
 * 连续 3 次 ping 失败（≈15 秒）→ 这个标签已经没用了，它自己收拾：
 *   ① 回调 onDead()，由调用方去改标题（标题的规矩在 app 那边，见 1.13）
 *   ② 顶部盖一条灰黄横幅（不是错误红）+「关闭本页」按钮
 *   ③ 试 window.close()；200ms 后如果还活着，就明说浏览器不让脚本关别人开的标签
 *   ④ 顺便探 8898~8907，哪个还活着就 location.replace 跳过去（死标签变活标签）
 *
 * 跳过去 / 确认关不掉之后**停止心跳和探测**，别让死页面在后台一直刷。
 *
 * 边界：这只覆盖"页面 JS 已经加载、服务后来死了"这一类。
 * 服务从没起来过、或用户刷新过，浏览器显示的是它自己的错误页，脚本根本跑不了。
 */

import { api } from './core.js';

var FAILS_TO_DIE = 3;
// 8899 跳过：那是 DSH 自己的端口。no-cors 探活只认"有没有回应"，
// 探到 DSH 也算"通"，跳过去比留着死页更糟。
// 默认值只是"没配置时猜的"；真正用哪张表由后端 /status 的 probe_ports 决定（1.27）。
// 8899 **故意跳过**：那是 DSH 自己的 Web GUI 端口，no-cors 探活只判断"有没有回应"，
// 探到 DSH 也算通 —— 跳过去比留着死页更糟。
var DEFAULT_PROBE_PORTS = [8898, 8900, 8901, 8902, 8903, 8904, 8905, 8906, 8907];
var PROBE_PORTS = DEFAULT_PROBE_PORTS;

/** 后端给了配置表就用它（app.js 在 /status 里顺手传进来）。 */
export function setProbePorts(list) {
  if (Array.isArray(list) && list.length) PROBE_PORTS = list;
}

export function startViewerHeartbeat(opts) {
  var onDead = (opts && opts.onDead) || function () {};
  var fails = 0;
  var stopped = false;
  var timer = setInterval(function () {
    if (stopped) return;
    // api() 在 HTTP 层失败时 resolve({ok:false})，网络断了才 reject —— 两种都算没心跳
    api('/ping').then(function (data) {
      if (data && data.ok) { fails = 0; return; }
      miss();
    }, miss);
  }, 5000);

  function miss() {
    if (stopped) return;
    fails += 1;
    if (fails < FAILS_TO_DIE) return;
    stopped = true;
    clearInterval(timer);
    becomeDead();
  }

  /* 服务重启后自动活过来：以前这个标签一直停"已失效"，用户必须手动刷新
     （用户 20:5x 就踩了：他把页面开着，我重启服务，他以为"看不到数据"是功能坏了）。
     现在每 5 秒试一次同源 /viewer，通了就 reload。 */
  function retrySelf() {
    fetch(location.origin + '/viewer', {
      mode: 'no-cors', cache: 'no-store'
    }).then(function () {
      window.location.reload();
    }, function () { /* 还没起来，下次再试 */ });
  }

  function becomeDead() {
    try { onDead(); } catch (e) { /* 标题改不动也不能挡住自救 */ }
    buildDeadBanner();
    try { window.close(); } catch (e) { /* 被拦了，下面走降级提示 */ }
    setTimeout(function () {
      var hint = document.getElementById('dead-close-hint');
      if (hint) hint.style.display = '';
    }, 200);
    // 先**只重试自己**：同一个服务重启回来就自动恢复，别急着跳到别的实例上去。
    // 以前是先 jump 再重试，结果"我的 8903 死了"会把你悄悄送到 8898 的生产页面
    // （用户 20:5x 的困惑就是这么来的：他看到的页面和别人看到的不是同一个）。
    let tries = 0;
    setInterval(function () {
      tries += 1;
      retrySelf();
      // 自己连试 6 次（30 秒）都不通，才去找别的还活着的实例
      if (tries === 6) jumpToAliveInstance();
    }, 5000);
  }

  function buildDeadBanner() {
    if (document.getElementById('dead-banner')) return;
    var bar = document.createElement('div');
    bar.id = 'dead-banner';
    bar.setAttribute('style',
      'position:fixed;top:0;left:0;right:0;z-index:99999;background:#fffbeb;' +
      'color:#7c2d12;border-bottom:1px solid #fcd34d;padding:10px 16px;' +
      'font:13px/1.6 system-ui,-apple-system,sans-serif;display:flex;' +
      'align-items:center;gap:12px;flex-wrap:wrap;box-shadow:0 1px 3px rgba(0,0,0,.08)');

    var text = document.createElement('span');
    text.style.flex = '1';
    text.textContent = '这个页面已经失效：串口网页服务已关闭。服务重启后本页会自动恢复；等不及就点右边关掉，或按下面提示处理。';
    bar.appendChild(text);

    // ① 「立即重连」：这个**一定有效**（服务回来了点它就活，服务没回来就再失败一次）
    var retryBtn = document.createElement('button');
    retryBtn.textContent = '立即重连';
    retryBtn.setAttribute('style',
      'font:inherit;padding:4px 12px;border-radius:6px;cursor:pointer;' +
      'border:1px solid #f59e0b;background:#f59e0b;color:#fff;font-weight:600');
    retryBtn.addEventListener('click', function () {
      retryBtn.textContent = '重连中…';
      window.location.reload();
    });
    bar.appendChild(retryBtn);

    // ② 「关闭本页」：标签是用户自己开的，浏览器**不允许**脚本关它 ——
    // 以前点了毫无反应（用户 21:4x 报的"点了好像什么都没发生"）。
    // 现在关不掉就**把按钮改成提示语**，让这一下点击有可见结果。
    var btn = document.createElement('button');
    btn.textContent = '关闭本页';
    btn.setAttribute('style',
      'font:inherit;padding:4px 12px;border-radius:6px;cursor:pointer;' +
      'border:1px solid var(--line,#3c3c3c);background:transparent;color:#7c2d12;font-weight:600');
    btn.addEventListener('click', function () {
      try { window.close(); } catch (e) { /* 被拦就走下面的降级 */ }
      setTimeout(function () {
        if (window.closed) return;          // 真关掉了：什么都不用做
        var hint = document.getElementById('dead-close-hint');
        if (hint) {
          hint.style.display = '';
          hint.style.fontWeight = '700';
          hint.style.color = '#7c2d12';
        }
        btn.textContent = '浏览器不让脚本关标签 → 按 Ctrl+W';
        btn.disabled = true;
        btn.style.opacity = '.6';
        btn.style.cursor = 'default';
      }, 250);
    });
    bar.appendChild(btn);

    var hint = document.createElement('span');
    hint.id = 'dead-close-hint';
    hint.style.display = 'none';
    hint.style.fontSize = '12px';
    hint.style.opacity = '.85';
    hint.textContent = '浏览器不让脚本关掉别人开的标签，请按 Ctrl+W';
    bar.appendChild(hint);

    document.body.appendChild(bar);
    document.body.style.paddingTop = '52px';
  }

  /* 探活：no-cors 只能判断"有没有回应"，所以只认我们自己的端口段；
     探到一个就跳过去——跳完这个页面就不再是死的了。 */
  function jumpToAliveInstance() {
    var index = 0;
    function next() {
      if (index >= PROBE_PORTS.length) return;   // 一个都不通：留着横幅，不再刷
      var port = PROBE_PORTS[index++];
      var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
      var timer2 = ctrl ? setTimeout(function () { ctrl.abort(); }, 300) : null;
      fetch('http://127.0.0.1:' + port + '/viewer', {
        mode: 'no-cors', cache: 'no-store',
        signal: ctrl ? ctrl.signal : undefined
      }).then(function () {
        if (timer2) clearTimeout(timer2);
        window.location.replace('http://127.0.0.1:' + port + '/');
      }, function () {
        if (timer2) clearTimeout(timer2);
        next();
      });
    }
    next();
  }

  window.addEventListener('pagehide', function () {
    try { navigator.sendBeacon('/bye'); } catch (e) { /* 老浏览器没有就算了 */ }
  });
}
