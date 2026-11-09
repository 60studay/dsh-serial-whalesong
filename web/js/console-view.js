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

  /** 设备徽章的文字：带上当前口号（两块同型号板子时，这是唯一能区分"谁回的"线索）。 */
  function devLabel() {
    var port = getPort();
    return port ? '设备 ' + port : '设备';
  }

  /** 追加回显：贴到当前回显行；没有就新起一行（设备徽章）。 */
  function append(text, ts) {
    if (!text) return;
    if (!curRx) {
      curRx = newRow('dev', devLabel(), ts);
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

  /** 发完命令后清掉"当前回显块"，让下一条回显新起一行。 */
  function resetRx() {
    curRx = null;
  }

  return {
    append: append,
    appendNote: appendNote,
    appendTx: appendTx,
    newRow: newRow,
    trimConsole: trimConsole,
    resetRx: resetRx,
  };
}
