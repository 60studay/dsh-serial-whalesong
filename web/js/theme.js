/**
 * 外观（2.2）：背景图 / 背景不透明度 / 主色 —— 1.19 之后 app.js 又逼近 800 行硬上限，
 * 这块整体搬出来，逻辑一行没改。
 *
 * 三条硬约束（用户 17:0x 定的，CSS 里也写着）：
 *   ① 背景图只作用于 `body::before` 那一层（z-index: -1），不碰任何面板
 *   ② 页头/侧栏不透明，logo 任何时候都清楚
 *   ③ 底层 `pointer-events: none`，不挡点击
 *
 * 值存在服务端 `.runtime/ui-theme.json`（运行期状态，不进 config、不进仓库），
 * 所以换台浏览器、重启进程都还在。
 */

import { $, api, post, errText } from './core.js';

var current = {};

function pct(value) {
  var n = parseInt(value, 10);
  return (isNaN(n) ? 100 : n) / 100;
}

export function applyTheme(theme) {
  var t = theme || {};
  var root = document.documentElement;
  var a = function (v) { return (v === undefined || v === null) ? 1 : v; };
  current = {
    bg_image: t.bg_image || '',
    bg_alpha: a(t.bg_alpha),
    accent: t.accent || '',
    panel_alpha: a(t.panel_alpha),
    side_image: t.side_image || '',
    side_color: t.side_color || '',
    side_alpha: a(t.side_alpha)
  };

  root.style.setProperty('--bg-image', current.bg_image ? 'url("' + current.bg_image + '")' : 'none');
  root.style.setProperty('--bg-alpha', String(current.bg_alpha));
  root.style.setProperty('--panel-alpha', String(current.panel_alpha));
  root.style.setProperty('--side-image', current.side_image ? 'url("' + current.side_image + '")' : 'none');
  root.style.setProperty('--side-alpha', String(current.side_alpha));
  if (current.side_color) root.style.setProperty('--side-color', current.side_color);
  else root.style.removeProperty('--side-color');
  if (current.accent) root.style.setProperty('--accent', current.accent);
  else root.style.removeProperty('--accent');

  // 回填控件（弹窗打开时看到的就是当前值）
  if ($('themeBg')) $('themeBg').value = current.bg_image;
  if ($('themeAlpha')) $('themeAlpha').value = String(Math.round(current.bg_alpha * 100));
  if ($('themePanel')) $('themePanel').value = String(Math.round(current.panel_alpha * 100));
  if ($('themeSideBg')) $('themeSideBg').value = current.side_image;
  if ($('themeSideAlpha')) $('themeSideAlpha').value = String(Math.round(current.side_alpha * 100));
  if ($('themeSideColor') && current.side_color) $('themeSideColor').value = current.side_color;
  if ($('themeAccent') && current.accent) $('themeAccent').value = current.accent;
}

function note(text, cls) {
  var el = $('themeNote');
  if (!el) return;
  el.className = 'hint' + (cls ? ' ' + cls : '');
  el.textContent = text || '';
}

function save(patch, okText) {
  post('/theme', patch).then(function (data) {
    if (!data.ok) { note('✗ ' + errText(data.error), 'bad'); return; }
    applyTheme(data.theme);
    note('✓ ' + okText, 'ok');
  }).catch(function (e) { note('✗ ' + ((e && e.message) || e), 'bad'); });
}

export function initTheme() {
  var btnOpen = $('btnOpenTheme');
  if (!btnOpen) return;                 // 页面上没有这批控件就别折腾

  btnOpen.addEventListener('click', function () {
    $('themeModal').classList.add('show');
  });
  $('btnThemeClose').addEventListener('click', function () {
    $('themeModal').classList.remove('show');
  });
  // 点弹窗外的遮罩也能关（和规则弹窗一样的手感）
  $('themeModal').addEventListener('click', function (ev) {
    if (ev.target === $('themeModal')) $('themeModal').classList.remove('show');
  });

  $('btnThemeSave').addEventListener('click', function () {
    save({
      bg_image: $('themeBg').value.trim(),
      bg_alpha: pct($('themeAlpha').value),
      panel_alpha: pct($('themePanel').value),
      side_image: $('themeSideBg').value.trim(),
      side_color: $('themeSideColor').value,
      side_alpha: pct($('themeSideAlpha').value),
      accent: $('themeAccent').value
    }, '已应用');
  });

  $('btnThemeReset').addEventListener('click', function () {
    save({
      bg_image: '', bg_alpha: 1, panel_alpha: 1,
      side_image: '', side_color: '', side_alpha: 1, accent: ''
    }, '已恢复默认');
  });

  // 兜底钩子（对面 B 方案）：web/custom.css 存在就挂上，不存在就什么都不做。
  var link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/custom.css';
  link.onerror = function () { link.remove(); };
  document.head.appendChild(link);

  api('/theme').then(function (data) {
    if (data && data.ok) applyTheme(data.theme);
  });
}
