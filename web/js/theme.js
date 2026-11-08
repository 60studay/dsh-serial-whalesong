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
var appliedExtra = [];
var touched = {};

function setOrDrop(name, value) {
  if (value === undefined || value === null || value === '') {
    document.documentElement.style.removeProperty(name);
  } else {
    document.documentElement.style.setProperty(name, String(value));
  }
}

/* 把 computed 的 rgb()/rgba() 转成 #rrggbb，好回填 <input type="color"> */
function toHex(color) {
  var m = String(color || '').match(/rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
  if (!m) return '';
  return '#' + [1, 2, 3].map(function (i) {
    var v = parseInt(m[i], 10).toString(16);
    return v.length === 1 ? '0' + v : v;
  }).join('');
}

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

  // 自由变量（别人写的皮肤变量）**先套**，官方项后设 —— 冲突时**官方优先**。
  // 以前顺序反了：extra 里的 --accent 会盖掉弹窗里的「主色」，
  // 用户看到的就是"怎么改都是那个颜色"（真实反馈，19:0x 修的）。
  (appliedExtra || []).forEach(function (name) { root.style.removeProperty(name); });
  appliedExtra = [];
  Object.keys(t.extra || {}).forEach(function (name) {
    if (/^--[a-z0-9][a-z0-9-]{0,39}$/.test(name)) {
      root.style.setProperty(name, String(t.extra[name]));
      appliedExtra.push(name);
    }
  });

  root.style.setProperty('--bg-image', current.bg_image ? 'url("' + current.bg_image + '")' : 'none');
  root.style.setProperty('--bg-alpha', String(current.bg_alpha));
  root.style.setProperty('--panel-alpha', String(current.panel_alpha));
  root.style.setProperty('--side-image', current.side_image ? 'url("' + current.side_image + '")' : 'none');
  root.style.setProperty('--side-alpha', String(current.side_alpha));
  if (current.side_color) root.style.setProperty('--side-color', current.side_color);
  else root.style.removeProperty('--side-color');
  if (current.accent) root.style.setProperty('--accent', current.accent);
  else root.style.removeProperty('--accent');


  // 文字颜色：空 = 用主题自带的中性色
  setOrDrop('--fg', t.fg_color);

  // 三个窗口各自的覆盖：空串 = 撤掉变量，回落到 --panel / --panel-alpha
  [['timeline', t.timeline_color, t.timeline_alpha],
   ['console', t.console_color, t.console_alpha],
   ['composer', t.composer_color, t.composer_alpha]].forEach(function (row) {
    setOrDrop('--' + row[0] + '-color', row[1]);
    setOrDrop('--' + row[0] + '-alpha', row[2] === '' || row[2] === undefined ? '' : row[2]);
  });


  // 回填控件（弹窗打开时看到的就是当前值）
  if ($('themeBg')) $('themeBg').value = current.bg_image;
  if ($('themeAlpha')) $('themeAlpha').value = String(Math.round(current.bg_alpha * 100));
  if ($('themePanel')) $('themePanel').value = String(Math.round(current.panel_alpha * 100));
  if ($('themeSideBg')) $('themeSideBg').value = current.side_image;
  if ($('themeSideAlpha')) $('themeSideAlpha').value = String(Math.round(current.side_alpha * 100));
  if ($('themeSideColor') && current.side_color) $('themeSideColor').value = current.side_color;
  if ($('themeAccent') && current.accent) $('themeAccent').value = current.accent;

  var cs = getComputedStyle(document.documentElement);
  // 颜色控件必须有值；没设过就显示"当前实际生效的颜色"
  fillColor('themeFg', t.fg_color, cs.getPropertyValue('--fg'));
  fillColor('themeTimeline', t.timeline_color, cs.getPropertyValue('--panel'));
  fillColor('themeConsole', t.console_color, cs.getPropertyValue('--panel'));
  fillColor('themeComposer', t.composer_color, cs.getPropertyValue('--panel'));
  fillAlpha('themeTimelineA', t.timeline_alpha);
  fillAlpha('themeConsoleA', t.console_alpha);
  fillAlpha('themeComposerA', t.composer_alpha);
}

function fillColor(id, value, fallback) {
  var el = $(id);
  if (!el) return;
  var hex = value || toHex(fallback);
  if (hex) el.value = hex;
}

function fillAlpha(id, value) {
  var el = $(id);
  if (!el) return;
  el.value = String(Math.round((value === '' || value === undefined ? 1 : value) * 100));
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


/* 从电脑里选图：浏览器不允许页面直接读 `file:///C:/...`（安全模型），
   所以只能"选文件 → 字节发给本地服务 → 服务存进 .runtime/theme/ → 页面读回来"。
   这也是对面本来建议的 C 方案（图不进仓库）。 */
function wirePicker(btnId, fileId, fieldId) {
  var btn = $(btnId), file = $(fileId), field = $(fieldId);
  if (!btn || !file || !field) return;
  btn.addEventListener('click', function () { file.click(); });
  file.addEventListener('change', function () {
    var picked = file.files && file.files[0];
    if (!picked) return;
    if (picked.size > 8 * 1024 * 1024) { note('✗ 图片超过 8 MB', 'bad'); return; }
    var reader = new FileReader();
    reader.onload = function () {
      fetch('/theme/image?name=' + encodeURIComponent(picked.name), {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: reader.result
      }).then(function (resp) { return resp.json(); }).then(function (data) {
        if (!data || !data.ok) {
          var err = (data && data.error) || {};
          note('✗ ' + (err.message || '上传失败'), 'bad');
          return;
        }
        field.value = data.url;
        note('✓ 图已存到本机，点「应用」生效', 'ok');
      }).catch(function (e) { note('✗ ' + ((e && e.message) || e), 'bad'); });
    };
    reader.readAsArrayBuffer(picked);
  });
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
    var patch = {
      bg_image: $('themeBg').value.trim(),
      bg_alpha: pct($('themeAlpha').value),
      panel_alpha: pct($('themePanel').value),
      side_image: $('themeSideBg').value.trim(),
      side_color: $('themeSideColor').value,
      side_alpha: pct($('themeSideAlpha').value),
      accent: $('themeAccent').value
    };
    // 只有"用户真动过"的单独项才提交：没动过就不写，
    // 免得点一次「应用」把当前的深色调写死，之后换浅色主题就错色。
    if (touched.fg) patch.fg_color = $('themeFg').value;
    ['timeline', 'console', 'composer'].forEach(function (name) {
      var colorId = 'theme' + name.charAt(0).toUpperCase() + name.slice(1);
      var alphaId = colorId + 'A';
      if (touched[name]) {
        patch[name + '_color'] = $(colorId).value;
        patch[name + '_alpha'] = pct($(alphaId).value);
      }
    });
    save(patch, '已应用');
  });

  // 「跟随」：清掉某个窗口的单独设置（空串 = 继承 --panel / --panel-alpha）。
  // 为什么需要它：这三个控件一旦被碰过，值就写死了，之后拖"面板不透明度"对它们**完全无效**
  // —— 用户 21:5x 就是这么感觉"逻辑反了 / 没收效"的（收发窗口被设成 100 之后就不动了）。
  [['Timeline', 'timeline'], ['Console', 'console'], ['Composer', 'composer']]
    .forEach(function (pair) {
      var btn = $('btnFollow' + pair[0]);
      if (!btn) return;
      btn.addEventListener('click', function () {
        var patch = {};
        patch[pair[1] + '_color'] = '';
        patch[pair[1] + '_alpha'] = '';
        touched[pair[1]] = false;
        save(patch, '已改回跟随统一设置');
      });
    });

  $('btnResetFg').addEventListener('click', function () {
    touched.fg = true;
    var cs = getComputedStyle(document.documentElement);
    $('themeFg').value = toHex(cs.getPropertyValue('--fg')) || '#d4d4d4';
    save({ fg_color: '' }, '文字颜色回默认');
    touched.fg = false;
    applyTheme(current);
  });

  // 标记"动过"，并给三个窗口滑块的联动按一下预览
  [['fg', 'themeFg'], ['timeline', 'themeTimeline'], ['timeline', 'themeTimelineA'],
   ['console', 'themeConsole'], ['console', 'themeConsoleA'],
   ['composer', 'themeComposer'], ['composer', 'themeComposerA']
  ].forEach(function (pair) {
    var el = $(pair[1]);
    if (el) el.addEventListener('change', function () { touched[pair[0]] = true; });
  });

  $('btnThemeReset').addEventListener('click', function () {
    save({
      bg_image: '', bg_alpha: 1, panel_alpha: 1,
      side_image: '', side_color: '', side_alpha: 1, accent: ''
    }, '已恢复默认');
  });

  wirePicker('btnBgPick', 'fileBg', 'themeBg');
  wirePicker('btnSideBgPick', 'fileSideBg', 'themeSideBg');

  // 完全自由的那条路（对面 B 方案）：页面最后加载一个 custom.css。
  // 先试运行期目录（.runtime/theme/custom.css），没有再退回仓库里的 web/custom.css；
  // 两个都没有就自己摘掉，不留 404 噪音。
  function linkCss(href) {
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    link.onerror = function () { link.remove(); };
    document.head.appendChild(link);
    return link;
  }
  var runtimeCss = linkCss('/theme/custom.css');
  runtimeCss.onload = function () { /* 有就用，不用再挂仓库那份 */ };
  runtimeCss.onerror = function () { runtimeCss.remove(); linkCss('/custom.css'); };

  api('/theme').then(function (data) {
    if (data && data.ok) applyTheme(data.theme);
  });
}
