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

var current = { bg_image: '', bg_alpha: 1, accent: '' };

export function applyTheme(theme) {
  var t = theme || {};
  var root = document.documentElement;
  var alpha = (t.bg_alpha === undefined || t.bg_alpha === null) ? 1 : t.bg_alpha;
  current = { bg_image: t.bg_image || '', bg_alpha: alpha, accent: t.accent || '' };
  root.style.setProperty('--bg-image', current.bg_image ? 'url("' + current.bg_image + '")' : 'none');
  root.style.setProperty('--bg-alpha', String(alpha));
  if (current.accent) root.style.setProperty('--accent', current.accent);
  else root.style.removeProperty('--accent');
  if ($('themeBg')) $('themeBg').value = current.bg_image;
  if ($('themeAlpha')) $('themeAlpha').value = String(Math.round(alpha * 100));
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
  if (!$('btnThemeSave')) return;      // 页面上没有这批控件（老页面/被裁掉）就别折腾

  $('btnThemeSave').addEventListener('click', function () {
    save({
      bg_image: $('themeBg').value.trim(),
      bg_alpha: (parseInt($('themeAlpha').value, 10) || 0) / 100,
      accent: $('themeAccent').value
    }, '已应用');
  });

  $('btnThemeReset').addEventListener('click', function () {
    $('themeBg').value = '';
    $('themeAlpha').value = '100';
    save({ bg_image: '', bg_alpha: 1, accent: '' }, '已恢复默认');
  });

  // 兜底钩子（对面 B 方案）：web/custom.css 存在就挂上，不存在就什么都不做。
  // 高手能写任意 CSS，我们不用管；文件缺失也能自己摘掉，不在控制台留一堆 404。
  var link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/custom.css';
  link.onerror = function () { link.remove(); };
  document.head.appendChild(link);

  api('/theme').then(function (data) {
    if (data && data.ok) applyTheme(data.theme);
  });
}
