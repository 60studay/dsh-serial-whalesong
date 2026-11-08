/**
 * 选口这一组动作（1.19 从 app.js 摘出来，本来 136 行）
 *
 * 包含四件事，它们都改"当前用哪个口"：
 *   ① 「AI 自动选择」  → POST /autodetect，再 POST /reconnect
 *   ② 「回到自动识别」 → POST /port/auto（1.18：只清 state，不动 config）
 *   ③ 「检测占用」     → POST /port/probe（只有点了才真的 open 一次串口）
 *   ④ 下拉框换口/波特率 → POST /reconnect
 *
 * 为什么不自己处理刷新：刷新下拉框和跑轮询是 app.js 的职责（它们还要读 lastState），
 * 所以这里只收四个回调：
 *   onPicked(port)  用户选定了一个口 → app 记进 preferredPort，避免重建下拉框时跳回旧项
 *   onProbe(result) 探测结果 → app 存下来给状态栏用（8 秒内优先显示它）
 *   refreshPorts()  重建下拉框
 *   tick()          立刻跑一次轮询
 */

import { $, post, pushEvent, errLine } from './core.js';

export function initPortPicker(opts) {
  var o = opts || {};
  var onPicked = o.onPicked || function () {};
  var onProbe = o.onProbe || function () {};
  var refreshPorts = o.refreshPorts || function () {};
  var tick = o.tick || function () {};

  /* ── AI 自动选择 ─────────────────────────────────────────
   *  用户反馈"按下去特别没有实感"：原来按钮不变、下拉框不动，
   *  只有一行灰字，看不出到底选没选上。现在给三重反馈：
   *  1) 按钮自己变成"检测中…"并脉动（表示真的在干活）
   *  2) 结果文字变绿/变红（一眼分清成没成）
   *  3) 下拉框真的跳到选中的那一项并闪一下绿边（最关键的确认）
   */
  $('btnAuto').addEventListener('click', function () {
    var btn = $('btnAuto');
    var reason = $('autoReason');
    var original = btn.textContent;
    if (btn.disabled) return;

    btn.disabled = true;
    btn.classList.add('working');
    btn.textContent = '检测中…';
    reason.className = 'hint';
    reason.textContent = '正在按场景规则扫描串口…';

    function restore() {
      btn.disabled = false;
      btn.classList.remove('working');
      btn.textContent = original;
    }
    function fail(error) {
      restore();
      reason.className = 'hint bad';
      reason.textContent = '✗ ' + errLine(error);
      pushEvent('bad', '自动选择失败：' + errLine(error));
    }

    post('/autodetect', {}).then(function (data) {
      if (!data.ok) { fail(data.error); return; }
      var port = data.port;

      // 3) 让下拉框真的跳到这一项（先跳，用户马上看见变化）
      var sel = $('portSelect');
      var existed = false;
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === port) { sel.selectedIndex = i; existed = true; break; }
      }
      if (!existed && port) {   // 端口是新的：临时插进去，refreshPorts 会补全
        var option = document.createElement('option');
        option.value = port; option.textContent = port;
        sel.appendChild(option);
        sel.value = port;
      }
      sel.classList.remove('flash');
      void sel.offsetWidth;      // 强制重排，动画才能重放
      sel.classList.add('flash');

      reason.className = 'hint ok';
      reason.textContent = '✓ 选中 ' + port + '（' + data.reason + '）' +
        (data.note ? '；' + data.note : '');
      pushEvent('', '自动选择：' + port + '（' + data.reason + '）');

      post('/reconnect', { com: port, baud: parseInt($('baudSelect').value, 10) || 0 })
        .then(function (r) {
          restore();
          if (!r.ok) {
            reason.className = 'hint bad';
            reason.textContent = '✗ 已选中 ' + port + '，但连接失败：' + errLine(r.error);
            pushEvent('bad', '自动选择后连接失败：' + errLine(r.error));
            return;
          }
          onPicked(port);
          refreshPorts(); tick();
        })
        .catch(function () { restore(); });
    }).catch(function (e) { fail({ message: '请求失败：' + (e && e.message || e) }); });
  });

  /* ── 回到自动识别（1.18）────────────────────────────────────
     只清 state 里那个"这次选的串口"，config.json 一个字不动。
     清完可能还是找不到口（拔了/换了口），那也是成功——用户要的是
     "别再用我刚选的那个"，不是"必须马上找到一个"。 */
  $('btnAutoPort').addEventListener('click', function () {
    var btn = $('btnAutoPort');
    var reason = $('autoReason');
    if (btn.disabled) return;
    btn.disabled = true;
    post('/port/auto', {}).then(function (data) {
      btn.disabled = false;
      if (!data.ok) {
        pushEvent('bad', '回到自动识别失败：' + errLine(data.error));
        return;
      }
      if (data.port) {
        reason.className = 'hint ok';
        reason.textContent = '✓ 已回到自动识别：' + data.port +
          (data.source ? '（' + data.source + '）' : '');
        pushEvent('', '回到自动识别：' + data.port);
        // 下拉框也要跟着跳：否则它会停在上次手动选的那一项（COM7），
        // 页头写着 COM10、下拉框标着 COM7 —— 用户一看就懵。
        onPicked(data.port);
      } else {
        reason.className = 'hint bad';
        reason.textContent = '✗ ' + (data.note || '按配置暂时找不到串口');
        pushEvent('bad', '回到自动识别：按配置暂时找不到串口');
      }
      refreshPorts();
      tick();
    }).catch(function (e) {
      btn.disabled = false;
      pushEvent('bad', '回到自动识别失败：' + ((e && e.message) || e));
    });
  });

  /* ── 显式探测串口占用（只有点了才真的 open 一次）────── */
  // 轻模式默认不碰串口，所以状态栏只能显示"状态未知"。
  // 用户想知道"是不是被串口助手占了"，点一下这里，后端才真去开一次。
  $('portOwner').addEventListener('click', function () {
    post('/port/probe', {}).then(function (data) {
      if (!data.ok) { pushEvent('bad', '检测占用失败：' + errLine(data.error)); return; }
      onProbe({ at: Date.now(), owner: data.owner, detail: data.detail });
      var label = { ours: '被本网页占用', other: '被其他程序占用',
                    free: '可用', unknown: '状态未知' }[data.owner] || data.owner;
      pushEvent('', '检测占用：' + label + (data.detail ? '（' + data.detail + '）' : ''));
      tick();
    });
  });

  // 下拉框换口 / 换波特率：立即重连
  ['portSelect', 'baudSelect'].forEach(function (id) {
    $(id).addEventListener('change', function () {
      post('/reconnect', {
        com: $('portSelect').value,
        baud: parseInt($('baudSelect').value, 10) || 0
      }).then(function (data) {
        if (!data.ok) { pushEvent('bad', errLine(data.error)); return; }
        pushEvent('', '已重连 ' + data.port + ' @ ' + data.baud);
        tick();
      });
    });
  });
}
