/**
 * 侧栏控件（1.26：从 app.js 摘出来，app.js 只剩 105 行余量，再涨 5 行就踩线）
 *
 * 管四件事，全部是"改了之后发一个请求、然后重画"：
 *   ① 控制权按钮（交给AI / 暂停AI / 继续AI / 人类接管）
 *   ② 连接模式（轻模式 / 监视）
 *   ③ 显示模式（文字 / 十六进制）
 *   ④ 定时发送表单 + 暂停/恢复 + 彻底退出
 *
 * 为什么用回调而不是直接 import app.js 的东西：这些动作都要"读当前状态"或"改本文件的状态"
 * （`lastState`、`dispMode`、`pollTimer`）。跨文件去拿那三个变量，等于把 app.js 拆了个寂寞。
 * 所以只收回调：
 *   getRunning()      → 当前后台循环是否在跑（暂停/恢复按钮要按它选动作）
 *   onDispMode(mode)  → 显示模式变了（app.js 要记住并改输入框提示）
 *   tick()            → 立刻轮询一次
 *   stopPolling()     → 彻底退出后停掉轮询
 */

import { $, post, pushEvent, errLine } from './core.js';

export function initSidebar(opts) {
  var o = opts || {};
  var getRunning = o.getRunning || function () { return false; };
  var onDispMode = o.onDispMode || function () {};
  var tick = o.tick || function () {};
  var stopPolling = o.stopPolling || function () {};

  /* ① 控制权：四个按钮共用一套处理，动作在 data-action 上 */
  document.querySelectorAll('#controlbar button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      post('/control', { action: btn.dataset.action }).then(function (data) {
        // 控制权变化本身由 renderStatus → 轨道统一显示，这里不再往数据区写
        if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
        if (data.note) pushEvent('', data.note);
        if (data.port_action === 'released') {
          pushEvent('', '已释放串口，可以打开串口助手了');
        } else if (data.port_action === 'acquired') {
          pushEvent('', '已重新占用串口（监视模式）');
        } else if (typeof data.port_action === 'string' && data.port_action.indexOf('failed') === 0) {
          pushEvent('bad', '串口未能重新占用：' + data.port_action);
        }
        tick();
      });
    });
  });

  /* ② 连接模式 */
  document.querySelectorAll('input[name=wmode]').forEach(function (r) {
    r.addEventListener('change', function () {
      post('/mode', { mode: r.value }).then(function (data) {
        if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
        pushEvent('', '已切到' + (data.mode === 'watch' ? '监视模式（占口）' : '轻模式（不占口）'));
        tick();
      });
    });
  });

  /* ③ 显示模式：只影响前端怎么显示，不用发请求 */
  document.querySelectorAll('input[name=disp]').forEach(function (r) {
    r.addEventListener('change', function () {
      onDispMode(r.value);
      $('cmd').placeholder = r.value === 'hex'
        ? '十六进制，如 AA 55 01 00，回车发送'
        : '输入指令后回车发送';
    });
  });

  /* ④ 定时发送：任何一个字段变了就整份提交（后端按"没传就不改"合并） */
  function pushWatch() {
    post('/watch', {
      send_enabled: $('sendEnabled').checked,
      check_enabled: $('checkEnabled').checked,
      check_source: $('checkSource').value,
      command: $('watchCommand').value,
      interval_ms: parseInt($('watchInterval').value, 10) || 1000
    }).then(function (data) {
      if (!data.ok) pushEvent('bad', errLine(data.error));
      else tick();
    });
  }
  ['sendEnabled', 'checkEnabled', 'checkSource', 'watchCommand', 'watchInterval']
    .forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener('change', pushWatch);
    });

  /* ⑤ 暂停 / 恢复 与 彻底退出 分开 */
  $('btnPausePort').addEventListener('click', function () {
    var running = !!getRunning();
    var path = running ? '/port/pause' : '/port/resume';
    post(path, {}).then(function (data) {
      if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
      if (running) {
        pushEvent('', '已暂停并释放串口（网页仍可用）');
      } else if (data.acquired) {
        pushEvent('', '已重新占用串口');
      } else {
        pushEvent('', '未占用串口：控制权不在 AI 或当前是轻模式');
      }
      tick();
    });
  });

  $('btnExit').addEventListener('click', function () {
    if (!confirm('彻底退出？网页将关闭，串口会释放。\n（只想临时把串口让给别的程序，请用「暂停并释放串口」）')) return;
    post('/shutdown', {}).then(function () {
      document.body.innerHTML =
        '<div style="padding:40px;font:16px/1.8 monospace;color:var(--fg)">' +
        '<h2 style="color:var(--human)">鲸歌已退出</h2>' +
        '<p>HTTP 服务已停止，串口已释放。可以关闭这个标签页。</p>' +
        '<p style="color:var(--muted)">需要时重新运行：python bridge.py web --scene &lt;场景&gt;</p></div>';
      stopPolling();
    });
  });
}
