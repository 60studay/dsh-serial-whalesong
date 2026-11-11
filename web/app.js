import { $, api, post, errText, errLine, pushEvent, nowText } from '../js/core.js';
import { autoGrow } from '../js/layout.js';
import { loadRules, onRulesSaved } from './js/rules.js';
import { pollChannel } from './js/channel.js';
// 1.19 拆出去的两块（原来都在这个文件里，各 115 / 136 行）
import { startViewerHeartbeat, setProbePorts } from './js/viewer.js';
import { initPortPicker } from './js/pickport.js';
import { initTheme } from './js/theme.js';
import { initSidebar } from './js/sidebar.js';
import { initTips } from './js/tips.js';
import { createConsoleView } from './js/console-view.js';
import { createFrameView } from './js/frames.js';

/* 鲸歌网页逻辑 · 收发区 + 规则 + 启动（R2b 第一步之后剩下的部分）。
 * 基础设施在 js/core.js、配色和拖动在 js/layout.js，这里靠 import 用它们。
 * 1.19：看客心跳/失效自救 → js/viewer.js；选口那组动作 → js/pickport.js。
 */
  var consoleEl = $('console');
  var dispMode = 'text';
  var lastState = null;
  // 「检测占用」的结果：8 秒内优先显示它，之后回到后端的"状态未知"（1.19 从旧位置搬上来）
  var probeResult = null;
  // 当前解析到的口 + 页面是否已失效：徽章（1.16）和窗口标题（1.13）都要用
  var curPort = '';
  var pageDead = false;
  var pollTimer = null;
  var lastAlertsRaw = [];
var lastChoicePrompt = '';
var controlUntil = null;      // 1.44：人接管到什么时候（epoch 秒）
var controlBy = '';
var countdownTimer = null;

  /* ── 告警条关闭状态（刷新后仍然生效）── */
  var ALERT_DISMISS_KEY = 'sb.alertDismissedTs';
  function getDismissedTs() {
    try { return parseFloat(localStorage.getItem(ALERT_DISMISS_KEY) || '0') || 0; }
    catch (e) { return 0; }
  }
  function setDismissedTs(value) {
    try { localStorage.setItem(ALERT_DISMISS_KEY, String(value)); } catch (e) { /* 隐私模式忽略 */ }
  }
  var lastStatus = { control: null, port: null, owner: null, alerts: 0, running: null };

  // 回显区行渲染（1.61 搬进 js/console-view.js）：徽章/时间/粘包规则都在那里
  var consoleView = createConsoleView({
    consoleEl: consoleEl,
    nowText: nowText,
    getPort: function () { return curPort; }
  });
  var append = consoleView.append;
  var appendNote = consoleView.appendNote;
  var appendTx = consoleView.appendTx;

  // 帧渲染（1.58/1.59/1.61）：去重记忆、历史载入都在 js/frames.js
  var frames = createFrameView({
    appendTx: function (who, cmd, ts) { appendTx(who, cmd, ts); },
    append: function (text, ts, port, fromLog) { append(text, ts, port, fromLog); },
    appendNote: function (text) { appendNote(text); },
    pushEvent: pushEvent,
    api: api,
    dispMode: function () { return dispMode; },
    divider: function (text) { consoleView.divider(text); }
  });

  /* ── 显示 ─────────────────────────────────────────────── */
  function renderPorts(data) {
    var sel = $('portSelect');
    var current = data.port || '';
    sel.innerHTML = '';
    (data.ports || []).forEach(function (p) {
      var opt = document.createElement('option');
      opt.value = p.device;
      opt.textContent = p.device + '  ' + (p.description || '');
      if (p.device === current) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!sel.options.length) {
      var opt = document.createElement('option');
      opt.textContent = '（没有检测到串口）';
      opt.value = '';
      sel.appendChild(opt);
    }
    var bauds = data.bauds || [];
    var bsel = $('baudSelect');
    var curBaud = data.baud || 115200;
    bsel.innerHTML = '';
    bauds.forEach(function (b) {
      var o = document.createElement('option');
      o.value = b; o.textContent = b;
      if (b === curBaud) o.selected = true;
      bsel.appendChild(o);
    });
    if (bauds.indexOf(curBaud) === -1) {
      var o2 = document.createElement('option');
      o2.value = curBaud; o2.textContent = curBaud;
      o2.selected = true;
      bsel.appendChild(o2);
    }
  }

  var STATE_LABEL = { human: '人工', ai: 'AI 托管', paused: '已暂停' };

  function renderStatus(data) {
    $('sceneName').textContent = '· ' + (data.scene || '');
    // 传输类型由后端给（transport），以后加 TCP / AI 通道时这里自动跟着变
    var tp = data.transport || {};
    var kindLabel = { serial: '串口', tcp: 'TCP', file: 'AI 通道' }[tp.type] || (tp.type || '串口');
    // 1.79：解析不出来时，页头**不许**装作"连着某个口"
    if (data.port_unresolved) {
      var cand = (data.resolve_candidates || []).join(' / ');
      var kind2 = (data.resolve_error || {}).kind || '';
      $('portPill').textContent = kind2 === 'ambiguous_device'
        ? ('未解析（歧义：' + (cand || '多个同型号口') + '）')
        : ('未解析（' + (kind2 || '没有可用口') + '）');
      $('portPill').dataset.unresolved = '1';
    } else {
      delete $('portPill').dataset.unresolved;
      $('portPill').textContent = kindLabel + ' ' + (data.port || '未连接') + ' @ ' + (data.baud || '?');
    }
    $('portPill').title = tp.frame ? (kindLabel + ' ' + (tp.frame || '')) : kindLabel;
    $('modePill').textContent = data.mode === 'watch' ? '监视模式（占口）' : '轻模式（不占口）';
    document.querySelector('input[name=wmode][value=' + (data.mode || 'light') + ']').checked = true;

    // 1.18：手动选的串口会活过重启，所以必须让它看得见、也必须能一键撤销。
    // 两块同型号板子（都是 CH340）时，用户最容易"不知道现在到底在用哪个"。
    var lock = $('portLock');
    if (lock) {
      var lockedCom = data.override_com || '';
      if (lockedCom) {
        lock.hidden = false;
        var lockText = $('portLockText');
        if (data.override_stale) {
          lockText.className = 'hint bad';
          lockText.textContent = '已锁定的 ' + lockedCom + ' 现在找不到了';
          lockText.title = '设备拔了或换了 USB 口。插上后重试，或点右边回到自动识别。';
        } else {
          lockText.className = 'hint';
          lockText.textContent = '已锁定 ' + lockedCom + '，重启后仍然生效';
          lockText.title = '这是你在页面上选的，不是 config.json 里写的';
        }
      } else {
        lock.hidden = true;
      }
    }

    // 定时节奏：数字全部来自 /status，前端**不自己算**（否则和真实发送会对不上）。
    // 用户上次就是"凭感觉觉得不对"却拿不出数字，这条就是为了让他能自己判。
    var statsEl = $('autoStats');
    if (statsEl) {
      var sent = Number(data.sent_count) || 0;
      var gap = Number(data.avg_gap_ms) || 0;
      var skipped = Number(data.sent_skipped) || 0;
      if (!sent && !skipped) {
        statsEl.textContent = '';
      } else {
        statsEl.textContent = '已发 ' + sent + ' 次' +
          (gap ? ' · 实际 ' + gap + ' ms' : '') +
          (skipped ? ' · 漏 ' + skipped : '');
      }
    }

    // 谁占着这个口。
    // 轻模式下后端**不探测**（打开 CH340 会拉 DTR/RTS，可能给板子来一次复位），
    // 只显示"状态未知"；想知道真答案就点这个胶囊，走 /port/probe 主动试一次。
    var owner = data.port_owner || {};
    var oe = $('portOwner');
    var OWNER_LABEL = { ours: '被本网页占用', other: '被其他程序占用', free: '可用', unknown: '状态未知' };
    var shown = owner;
    if (probeResult && (Date.now() - probeResult.at) < 8000) shown = probeResult;
    oe.dataset.owner = shown.owner || 'unknown';
    // 1.68：被别的程序占着时，把"从何时起 / 多久了"直接写在胶囊上 ——
    // 只显示"被其他程序占用"看不出是刚发生还是已经挂了一小时。
    var label = OWNER_LABEL[shown.owner] || '—';
    if (shown.owner === 'other') {
      var sinceTxt = shown.since ? new Date(shown.since * 1000) : null;
      var dur = Number(shown.seconds) || 0;
      var durTxt = dur >= 60 ? (Math.floor(dur / 60) + ' 分钟') : (dur + ' 秒');
      label += '（' + (sinceTxt
        ? ('21'.length && (('0' + sinceTxt.getHours()).slice(-2) + ':' + ('0' + sinceTxt.getMinutes()).slice(-2)))
        : '刚才') + ' 起 · ' + durTxt + '）';
    }
    oe.textContent = label;

    // 1.69：占用 → 释放，两行都进状态轨道（带时长；这期间插件 0 条记录就提示可能被绕过）
    var prevOwner = lastStatus.owner;
    if (shown.owner === 'other' && prevOwner !== 'other') {
      pushEvent('bad', (shown.device || '串口') + ' 被其他程序占用'
        + (shown.since ? '（' + new Date(shown.since * 1000).toLocaleTimeString('zh-CN', { hour12: false }) + ' 起）' : '')
        + (shown.bypass_suspect ? '；插件这段时间 0 条收发记录，可能有人绕过了插件' : ''));
    } else if (shown.owner !== 'other' && prevOwner === 'other') {
      var rel = Number(shown.released_after) || 0;
      var mm = Math.floor(rel / 60);
      var ss = rel % 60;
      pushEvent('', (shown.released_device || '串口') + ' 已释放（占用 '
        + (mm ? mm + ' 分 ' : '') + ss + ' 秒）'
        + (shown.released_bypass ? '；这段时间插件 0 条记录 → 可能有人绕过了插件' : ''));
    }
    lastStatus.owner = shown.owner;
    oe.title = (shown.detail || '')
      + (shown.owner === 'other' ? '\n进程名：' + (shown.process_name || '未知（Windows 不提供占用进程名）') : '')
      + (shown.bypass_suspect ? '\n这段时间插件没有收发记录 —— 可能有人绕过了插件直接开这个口' : '')
      + '\n（点一下检测占用）';
    oe.style.cursor = 'pointer';

    // 暂停/恢复按钮
    var running = !!data.watch_running;
    $('btnPausePort').textContent = running ? '暂停并释放串口' : '恢复占用';
    $('btnPausePort').disabled = false;
    $('pauseHint').textContent = data.hold_released_reason
      ? '已释放：' + data.hold_released_reason
      : (running ? '网页仍可用，只是把串口让给串口助手。' : '当前未占用串口，网页可正常使用。');

    var st = data.control || 'human';
    var el = $('controlstate');
    el.dataset.state = st;
    el.textContent = STATE_LABEL[st] || st;
    // 控制权真变了才弹一下（第一次渲染不算）
    if (lastStatus.control !== null && lastStatus.control !== st) pulse(el);
    // 按钮规则：
    //   目标状态 ≠ 当前 → 可用（合法迁移）
    //   目标状态 = 当前 → 也可用，点了给出"已经在你手中"的反馈
    //   其余方向非法 → 禁用并说明原因（不能出现"点了没反应"）
    $('btnAi').disabled     = false;
    $('btnPause').disabled  = st !== 'ai';
    $('btnResume').disabled = st !== 'paused';
    $('btnHuman').disabled  = false;
    $('btnAi').title     = st === 'ai' ? '已经是 AI 托管' : '';
    $('btnHuman').title  = st === 'human' ? '已经是人工控制' : '';
    $('btnPause').title  = st === 'ai' ? '' : '只有 AI 托管时才能暂停 AI';
    $('btnResume').title = st === 'paused' ? '' : '只有已暂停时才能继续 AI';

    var CONTROL_HINT = {
      human: '人工控制：AI 不能发命令，你可以直接用串口助手',
      ai: 'AI 托管：AI 可以发命令，人类发任何命令会立刻收回控制权',
      paused: '已暂停：谁都不发，点[继续AI]或[人类接管]恢复'
    };
    var ch = $('controlhint');
    if (ch) {
      // 1.23：解释改成角标，文字写进 data-tip（气泡内容）和 aria-label（读屏）
      var tipText = CONTROL_HINT[st] || '';
      ch.setAttribute('data-tip', tipText);
      ch.setAttribute('aria-label', tipText);
    }

    var w = data.watch || {};
    // ⚠️ 用户正在编辑表单时**不许用轮询的值覆盖他**。
    // /status 每 700ms 拉一次，以前这里无条件写回，等于"根本改不了"：
    // 删掉一位数字，0.7 秒后它自己跳回来（用户实测报的 bug）。
    // 只跳过**表单控件**；规则列表、告警、状态胶囊这些只读展示照常刷新，
    // 否则用户改完规则看不到变化。
    var ae = document.activeElement;
    var editingForm = ae && /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName);
    if (!editingForm) {
      $('sendEnabled').checked = !!w.send_enabled;
      $('checkEnabled').checked = !!w.check_enabled;
      if (w.command && !$('watchCommand').value) $('watchCommand').value = w.command;
      if (w.interval_ms) $('watchInterval').value = w.interval_ms;
      if (w.check_source) $('checkSource').value = w.check_source;
    }

    var rs = $('ruleState');
    if (data.resolve_error) {
      rs.className = 'rule-state bad';
      rs.textContent = '✕ ' + (data.resolve_error.message || '串口不可用');
    } else if (w.check_enabled) {
      rs.className = 'rule-state ok';
      rs.textContent = '✓ 判定中（' + (w.check_source || 'watch_only') + '）';
    } else {
      rs.className = 'rule-state ok';
      rs.textContent = '— 判定未启用';
    }

    // 告警条：只显示"用户还没关掉"的记录。
    // alerts.jsonl 是历史累积文件，只要有一条就永久显示是不对的（Bug：关不掉）。
    // 关闭时把当前时间记进 localStorage，之后 ts 更旧的都不弹；文件本身不删。
    var rawAlerts = data.alerts || [];
    lastAlertsRaw = rawAlerts;
    var alerts = rawAlerts.filter(function (a) { return (a.ts || 0) > getDismissedTs(); });
    var bar = $('alertbar');
    if (alerts.length) {
      var a = alerts[alerts.length - 1];
      $('alertbarText').textContent = '⚠ ' + (a.time || '') + '  规则「' + (a.rule || '?') + '」触发：' +
        (a.reason || '') + (a.action ? '  → 已执行 ' + a.action : '');
      bar.classList.add('show');
    } else {
      bar.classList.remove('show');
    }

    /* ── 把状态变化写进时间轨道（不混进串口数据区）── */
    if (lastStatus.control !== null && lastStatus.control !== st) {
      pushEvent('ctrl', '控制权 → ' + (STATE_LABEL[st] || st));
    }
    if (lastStatus.port !== null && data.port && lastStatus.port !== data.port) {
      pushEvent('', '串口 → ' + data.port + ' @ ' + (data.baud || '?'));
    }
    var ow = owner.owner || 'unknown';
    if (lastStatus.owner !== null && lastStatus.owner !== ow) {
      var OWNER_TEXT = { ours: '被本网页占用', other: '被其他程序占用', free: '可用', unknown: '状态未知' };
      pushEvent(ow === 'other' ? 'bad' : '', '串口' + (OWNER_TEXT[ow] || ow));
    }
    var alertCount = alerts.length;
    if (alertCount > (lastStatus.alerts || 0)) {
      var latest = alerts[alertCount - 1] || {};
      // blame 是人话（"AI 尝试发送「V」被拒（控制权在 human）"），rule 只作兜底
      pushEvent('warn', '告警：' + (latest.blame || latest.rule || '?'));
    }
    var runNow = !!data.watch_running;
    if (lastStatus.running !== null && lastStatus.running !== runNow) {
      pushEvent('', runNow ? '后台循环已启动' : '后台循环已停止');
    }
    lastStatus = { control: st, port: data.port || null, owner: ow,
                   alerts: alertCount, running: runNow };

    lastState = data;
    controlUntil = (typeof data.control_until === 'number') ? data.control_until : null;  // 1.44
    controlBy = data.control_by || '';
    paintCountdown();
    // 1.61：实例/页面过期就提醒一次（改完 HTML/JS 不重启实例也能知道）
    (function () {
      if (staleHintShown) return;
      var stamp = data.static_stamp || {};
      var loadedAt = (window.performance && performance.timeOrigin ? performance.timeOrigin : Date.now()) / 1000;
      var newest = Math.max(0, Number(stamp["index.html"] || 0), Number(stamp["app.js"] || 0),
        Number(stamp["app.css"] || 0));
      if (newest > loadedAt + 1) {
        staleHintShown = true;
        pushEvent('warn', '网页文件更新过，你这个页面是旧的 —— 刷新一次（F5）就能用上');
      }
    })();
    curPort = data.port || '';
    setProbePorts(data.probe_ports);      // 1.27：探测表由后端给，前端不再写死

    // 解析不出串口、但机器上插着好几个 → **问人**，不要自己猜（用户 22:5x 定的方向）。
    // 只在"需要选择"这件事发生变化时说一次，不然每 700ms 刷一条。
    if (data.needs_port_choice && !lastChoicePrompt) {
      // 1.79/1.83：提示里只列**这个场景匹配到的**口，别把 6 个蓝牙虚拟口也倒出来
    var choiceList = (data.resolve_candidates && data.resolve_candidates.length)
      ? data.resolve_candidates : (data.port_choices || []);
    lastChoicePrompt = choiceList.join('、');
      pushEvent('warn', '请选一个串口：' + lastChoicePrompt + '（同型号分不清，需要你定）');
    } else if (!data.needs_port_choice) {
      lastChoicePrompt = '';
    }
    setPageTitle();
  }

  /** 让某个元素"弹一下"（重置动画再放，否则连续两次不会重播）。 */
  function pulse(el) {
    if (!el) return;
    el.classList.remove('pop');
    void el.offsetWidth;      // 强制重排：动画才能重放
    el.classList.add('pop');
  }

  /** 窗口标题：`鲸歌 · COM7 · 场景名`（1.13）。
   *
   *  为什么要它：用户同时开着好几个鲸歌页面（8898 一块板、8900 蓝牙），
   *  标签栏里一律是"鲸歌"，切窗口时要一个一个点开才知道哪个是哪个。
   *  带上口号和场景，标签栏就能直接认。
   *
   *  `[已失效]` 必须留在**最前面**：死页自救时它是标签栏里唯一的记号。
   */
  function setPageTitle() {
    var parts = ['鲸歌'];
    if (curPort) parts.push(curPort);
    if (lastState && lastState.scene) parts.push(lastState.scene);
    document.title = (pageDead ? '[已失效] ' : '') + parts.join(' · ');
  }

  /* ── 控制权倒计时（1.44）───────────────────────────────
   两种人接管：
     · 发了一条命令 → 10 秒后自动回 ai（显示"剩 N 秒"）
     · 点了「人类接管」→ 永久（显示"不自动恢复"）
   只改文字，不触发轮询；时间到了由后端下一次 /status 翻转，这里只是别显示成负数。 */
function paintCountdown() {
  var label = document.getElementById('controlstate');
  if (!label) return;
  var base = STATE_LABEL[lastState && lastState.control] || (lastState && lastState.control) || '';
  // 永久接管**不加括号说明**（用户 20:1x："不用拿括号标出来"），就是基础文案本身
  if (!controlUntil) {
    label.textContent = base;
    return;
  }
  var left = Math.max(0, Math.round(controlUntil - Date.now() / 1000));
  if (left <= 0) { label.textContent = base; return; }
  label.textContent = '人类控制（剩 ' + left + ' 秒）';
}

function startCountdown() {
  if (countdownTimer) return;
  countdownTimer = setInterval(paintCountdown, 1000);
}

/* 输入框有字且聚焦 → 续期，别让人"打一半被 AI 插队"（判据 8）。
   只在真的会过期（human-command）时续，显式接管是永久的。 */
function renewWhileComposing() {
  var box = document.getElementById('cmd');
  if (!box) return;
  var composing = document.activeElement === box && box.value.trim() !== '';
  if (!composing || !controlUntil) return;
  if (controlBy !== 'human-command') return;
  post('/control', { action: 'renew', actor: 'human' }).catch(function () {});
}

/* ── 轮询 ─────────────────────────────────────────────── */
  // 页面刚做过一次"选口 / 回到自动识别"：下一次重建下拉框要用这个值。
  // 不能用 lastState.port —— 它要等下一次 /status 轮询才更新，最多晚 700ms，
  // 于是下拉框会把刚选好的项又改回去（用户看到"点了又跳回旧口"）。
  var preferredPort = null;

  function refreshPorts() {
    return api('/ports').then(function (data) {
      if (!data.ok) return;
      renderPorts({ ports: data.ports, bauds: data.bauds,
                    port: preferredPort || (lastState && lastState.port),
                    baud: lastState && lastState.baud });
      preferredPort = null;
    });
  }

  // 帧游标（1.17）：页面按 since 读，**不消费**帧，别的看客（测试脚本/另一个标签）
  // 也能各自看到同样的事件。以前 /poll 取走即清空，谁先轮询谁拿到。
  // 1.57：首屏用 -1 = "只从现在开始"，不要把整圈历史当成刚发生的事画出来。
var frameCursor = -1;
// 1.61：页面是不是旧的（网页静态文件在"我这次加载之后"被改过）
var staleHintShown = false;
var startedOnce = false;

  // 把一次 /poll 的结果画出来并推进游标。tick 和「读一次」共用一份，
  // 免得两处各写一遍、后来只改了一处（这类"一半对一半错"最费排查时间）。
  function tick() {
    // startedOnce 之前 frameCursor 是 -1 → 只取从现在起的帧；回来后服务器给 cursor

    api('/status').then(function (data) {
      if (data.ok) renderStatus(data);
    });
    api('/poll?since=' + frameCursor).then(function (data) {
      // 游标推进必须留在这里（1.61 把渲染搬进 frames.js 时丢了这段，
      // 于是 frameCursor 永远是 -1、每轮都"只从现在开始"→ 一条都不显示）
      if (data && typeof data.cursor === 'number' && data.cursor > frameCursor) {
        frameCursor = data.cursor;
      }
      frames.applyPoll(data);
    });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(tick, 700);
  }

  /* 服务死了：标题加 `[已失效]` 前缀（最前面，标签栏里才认得出）。
     标题的拼法只有 setPageTitle 一处，所以这里改状态再重画，不手拼字符串。 */
  function markPageDead() {
    pageDead = true;
    setPageTitle();
  }

  /* ── 事件 ─────────────────────────────────────────────── */
  function send() {
    var raw = $('cmd').value;
    if (!raw.trim()) return;
    // 输入框是多行的，但**换行不原样发出去**：串口命令的结尾由场景的 newline
    // 统一加，手工塞进去的换行会变成命令的一部分。
    // hex 模式本来就允许用空格分隔字节，所以这里统一按空格合并。
    var text = raw.replace(/[\r\n]+/g, ' ').trim();
    $('cmd').value = '';
    autoGrow();
    // 先发，成功了才把这条命令打到回显区。
    // 否则被拒绝（控制权/被占用/格式错）时界面也显示成"发过了"，看起来像成功了（Bug 9b）。
    post('/send', {
    client: 'page', text: text, mode: dispMode, actor: 'human' }).then(function (data) {
      if (!data.ok) {
        pushEvent('bad', '发送失败：' + errLine(data.error));
        return;
      }
      appendTx('human', text);
      if (data.rx_text || data.rx_hex) {
        if (dispMode === 'hex') append(data.rx_hex + '\n');
        else if (data.rx_text) append(data.rx_text);
      }
    });
  }

  $('cmd').addEventListener('input', autoGrow);

  $('sendBtn').addEventListener('click', send);

  // 轻模式默认不主动读串口（保证不占口），需要时手动读一帧。
  // 1.17 的尾巴：这里以前是 `/poll?force=1`（不带 since = 取走语义），
  // 点一下就把攒着的帧抢走了，另一个标签/测试脚本再也看不到那几行。
  // 现在和 tick 一样带游标读，force 只表示"额外真读一次串口"。
  $('btnReadOnce').addEventListener('click', function () {
    api('/poll?force=1&since=' + frameCursor).then(function (data) {
      if (!data.ok) { pushEvent('bad', '读取失败：' + errText(data.error)); return; }
      frames.applyPoll(data);
      pushEvent('', '手动读取一帧（' + (data.bytes || 0) + ' 字节）');
    });
  });

  // 清空：回显 + 状态轨道 + 告警，一起清。
  // 用户测试时告警一直弹，"清空"只清回显等于没清（14:35 需求 3）。
  if ($('btnHistory')) {
    $('btnHistory').addEventListener('click', function () { frames.loadHistory(50); });
  }

  $('btnClear').addEventListener('click', function () {
    consoleEl.textContent = '';
    consoleEl.scrollTop = 0;
    consoleView.resetRx();
    // 1.59：清空 = 忘掉"已经画过哪些帧"，否则再点「载入历史」会被去重挡成
    // "没有新的历史可载入"（用户实测：清空后载入历史什么都不出来）。
    frames.reset();
    $('timelineList').textContent = '';
    $('alertbar').classList.remove('show');
    post('/alerts/clear', {}).then(function (data) {
      if (!data.ok) { pushEvent('bad', '清空告警失败：' + errLine(data.error)); return; }
      pushEvent('', '已清空回显/状态轨道/告警' +
        (typeof data.cleared === 'number' ? '（告警 ' + data.cleared + ' 条）' : ''));
    });
  });

  // 关闭告警条：记下"已看到这里"，比它旧的不再弹（日志文件不动）
  $('alertbarClose').addEventListener('click', function () {
    var latestTs = 0;
    (lastAlertsRaw || []).forEach(function (a) { latestTs = Math.max(latestTs, a.ts || 0); });
    var nowTs = Date.now() / 1000;
    setDismissedTs(Math.max(latestTs, nowTs));
    $('alertbar').classList.remove('show');
    pushEvent('', '已关闭告警提示（日志仍保留）');
  });
  $('cmd').addEventListener('keydown', function (e) {
    // 回车发送、Shift+回车换行 —— 聊天软件的通用约定，不用教。
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  /* ── 选口那组动作（1.19 移进 js/pickport.js）──────────────
     那边只负责"点按钮之后发什么请求"；刷新下拉框和跑轮询留在这里，
     因为它们要读 lastState —— 那是本文件的状态，不该跨文件去拿。 */
  // 侧栏控件 / 角标：都在 js/ 里（1.26 拆的）
  initTips();
  initSidebar({
    getRunning: function () { return !!(lastState && lastState.watch_running); },
    onDispMode: function (mode) { dispMode = mode; },
    tick: tick,
    stopPolling: function () { if (pollTimer) clearInterval(pollTimer); }
  });

  initPortPicker({
    onPicked: function (port) { preferredPort = port; },
    onProbe: function (result) { probeResult = result; },
    refreshPorts: refreshPorts,
    tick: tick
  });

  /* ── 启动 ─────────────────────────────────────────────── */
  // AI 通道（AI ⇄ AI 留言板）已冻结（用户 11-07 决定"到时候再用"）。
  // 标签藏起来之后还每 2 秒轮询就是纯浪费，还会在标题上跳未读数 —— 一起停掉。
  var CHANNEL_ENABLED = false;

  function startChannel() {
    if (!CHANNEL_ENABLED) return;
    pollChannel();
    setInterval(pollChannel, 2000);
  }

  api('/status').then(function (data) {
    if (data.ok) renderStatus(data);
    return api('/ports');
  }).then(function (data) {
    if (data && data.ok) renderPorts({ ports: data.ports, bauds: data.bauds,
                                       port: lastState && lastState.port,
                                       baud: lastState && lastState.baud });
    onRulesSaved(tick);
    loadRules();
    tick();
    startPolling();
  consoleView.setupFontZoom();
    startCountdown();
    setInterval(renewWhileComposing, 4000);
    startViewerHeartbeat({ onDead: markPageDead });
    initTheme();
    startChannel();
  }).catch(function () {
    onRulesSaved(tick);
    loadRules();
    tick();
    startPolling();
    startViewerHeartbeat({ onDead: markPageDead });
    initTheme();
    startChannel();
  });
