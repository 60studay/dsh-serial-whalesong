/** 回显区的行渲染（1.61 从 app.js 里搬出来，避免单文件顶到 700 行上限）。
 *
 *  两列网格：左列徽章、右列内容。TX 和 RX 走同一套排版，
 *  所以"设备回的话"会自然对齐到"命令文字"的左边。
 *
 *  为什么不写 ">> 命令"：用户反馈 >> 容易和串口数据里的字符混淆，改成靠颜色 + 徽章区分。
 *  为什么回显要粘在一个块里：串口是字节流，半包/粘包都常见；
 *  逐字节换行会把 "OK PO" / "NG" 拆成两行。
 *  为什么用 grid 而不是给回显加 padding：徽章宽度随字体/缩放变，写死 padding 迟早错位。
 *
 *  时间戳取**帧自己的 ts**（1.57）：以前用渲染时刻，回放的老帧全被盖成"刚才"。
 */

/* source → [徽章样式, 徽章文字]。web/manual 都是"人从网页点的"，算"我"。 */
export var WHO = {
  ai: ['ai', 'AI'],
  human: ['human', '我'],
  web: ['human', '我'],
  manual: ['human', '我'],
  auto: ['auto', '定时'],
  timer: ['auto', '定时'],
  watch: ['auto', '定时'],
  rule: ['rule', '规则'],
  external: ['bad', '外部'],
  session: ['ai', '会话'],
  cli: ['bad', '外部'],
  log: ['auto', '日志'],
  dev: ['dev', '设备'],
};

export function createConsoleView(opts) {
  var consoleEl = opts.consoleEl;
  var nowText = opts.nowText;
  var getPort = opts.getPort;
  var MAX_CHARS = 300000;
  var curRx = null;   // 当前正在追加的回显块（发完命令就置空，下一条回显新起一行）

  function trimConsole() {
    while (consoleEl.textContent.length > MAX_CHARS && consoleEl.firstChild) {
      consoleEl.removeChild(consoleEl.firstChild);
    }
  }

  /** 建一行：左列徽章、中间正文、右列时间。时间只在这一行**新起**时打一次。 */
  function newRow(tagClass, tagText, ts) {
    var row = document.createElement('div');
    row.className = 'ln';
    var tag = document.createElement('span');
    tag.className = 'tag ' + tagClass;
    tag.textContent = tagText;
    // 口号可能很长（COM256 之类），徽章列是定宽的 → 截断 + 悬停看全名，不许撑破布局。
    tag.title = tagText;
    var body = document.createElement('span');
    body.className = 'body';
    var stamp = document.createElement('span');
    stamp.className = 't';
    stamp.textContent = nowText(ts);
    row.appendChild(tag);
    row.appendChild(body);
    row.appendChild(stamp);
    consoleEl.appendChild(row);
    return body;
  }

  /** 设备徽章的文字：带上**这一帧自己的**口号（1.70）。
   *
   *  为什么不用"当前场景的口"：`--com COM10` 发出去的命令以前也标成 COM7，
   *  用户看到"发给了谁"是假的。帧里带了 port 就用它，没有才退回当前口（老帧/老后端）。
   */
  function devLabel(framePort, fromLog) {
    // 1.82/1.85：日志里老格式没有口号时**不许猜** —— 以前写"未知"但工具提示里
    // 仍带着当前口，读起来还是像 COM7。现在直接说清是"老日志没记"。
    if (fromLog && !framePort) return '口未知（老日志）';
    var port = framePort || getPort();
    return port ? '设备 ' + port : '设备';
  }

  /** 追加回显：贴到当前回显行；没有就新起一行（设备徽章）。 */
  function append(text, ts, framePort, fromLog) {
    if (!text) return;
    if (!curRx) {
      curRx = newRow('dev', devLabel(framePort, fromLog), ts);
      curRx.classList.add('rx');
    }
    curRx.appendChild(document.createTextNode(text));
    consoleEl.scrollTop = consoleEl.scrollHeight;
    trimConsole();
  }

  /** 回显位置的淡色说明（1.14）：「回显走读线程」和「设备真没回话」必须分得开。 */
  function appendNote(text) {
    if (!text) return;
    if (!curRx) {
      curRx = newRow('dev', devLabel());
      curRx.classList.add('rx');
    }
    var span = document.createElement('span');
    span.className = 'note';
    span.textContent = text;
    curRx.appendChild(span);
    consoleEl.scrollTop = consoleEl.scrollHeight;
    trimConsole();
  }

  /** 打一行"谁发的 + 命令"，紧跟其后的回显会自动对齐到命令文字下面。 */
  function appendTx(who, command, ts) {
    var meta = WHO[who] || WHO.dev;
    var body = newRow(meta[0], meta[1], ts);
    body.classList.add('tx');
    body.textContent = command;
    curRx = null;
    consoleEl.scrollTop = consoleEl.scrollHeight;
    trimConsole();
  }

  /** 分隔线（1.84）：用来分开"页面打开前（来自日志）"和之后的实时帧。 */
  function divider(text) {
    var row = document.createElement('div');
    row.className = 'ln sep';
    var span = document.createElement('span');
    span.className = 'sep-text';
    span.textContent = '— ' + text + ' —';
    row.appendChild(span);
    consoleEl.appendChild(row);
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  /** 发完命令后清掉"当前回显块"，让下一条回显新起一行。 */
  function resetRx() {
    curRx = null;
  }

  /* 1.92：收发区字号 —— Ctrl+滚轮调，Ctrl+0 复位，存 localStorage。
   *
   * 为什么绑在**收发区容器**上：绑 window 的话，在设置面板上滚也会改字号（用户会骂）。
   * 为什么必须 preventDefault：不然浏览器会整页缩放（默认行为，最容易漏）。
   */
  var FONT_KEY = 'sb.consoleFontPx';
  var FONT_MIN = 10, FONT_MAX = 28;
  function currentFont() {
    var saved = parseInt(localStorage.getItem(FONT_KEY) || '', 10);
    return Number.isFinite(saved) ? saved : 13;
  }
  function applyFont(px) {
    var size = Math.max(FONT_MIN, Math.min(FONT_MAX, px));
    consoleEl.style.fontSize = size + 'px';
    // 行高用 em 跟着字号走，否则字大了会挤在一起
    consoleEl.style.lineHeight = '1.45em';
    try { localStorage.setItem(FONT_KEY, String(size)); } catch (e) { /* 隐私模式 */ }
    return size;
  }
  function setupFontZoom() {
    applyFont(currentFont());
    consoleEl.addEventListener('wheel', function (event) {
      if (!event.ctrlKey) return;
      event.preventDefault();
      applyFont(currentFont() + (event.deltaY < 0 ? 1 : -1));
    }, { passive: false });
    document.addEventListener('keydown', function (event) {
      if (event.ctrlKey && (event.key === '0' || event.key === ')')) {
        event.preventDefault();
        applyFont(13);
      }
    });
  }

  return {
    append: append,
    appendNote: appendNote,
    appendTx: appendTx,
    newRow: newRow,
    trimConsole: trimConsole,
    resetRx: resetRx,
    divider: divider,
    setupFontZoom: setupFontZoom,
  };
}
