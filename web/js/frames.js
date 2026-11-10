/** 帧渲染 + 历史载入（1.58 / 1.59 / 1.61）。
 *
 *  为什么单独一个文件：app.js 已经逼近 700 行硬上限（1.61 那次改动直接顶到 703）。
 *  这里只放"把 /poll 的帧画出来"和"载入历史"两件事，都是纯渲染，不碰控制权/表单。
 *
 *  去重记忆由本模块持有（key = ts|tx），于是：
 *    · 连点「载入历史」不会重复追加（1.58）
 *    · 「清空」调 reset() 忘掉记忆，历史才能重新画出来（1.59）
 */
export function createFrameView(opts) {
  var appendTx = opts.appendTx;
  var append = opts.append;
  var appendNote = opts.appendNote;
  var pushEvent = opts.pushEvent;
  var api = opts.api;
  var dispMode = opts.dispMode;
  var seen = {};

  function frameKey(f) {
    return String((f && f.ts) || '') + '|' + String((f && f.tx) || '');
  }

  function frameSeen(f) {
    var key = frameKey(f);
    if (seen[key]) return true;
    seen[key] = true;
    return false;
  }

  /** 清空之后重新允许载入（1.59）。 */
  function reset() {
    seen = {};
  }

  function applyPoll(data) {
    if (!data.ok) return;
    (data.frames || []).forEach(function (f) {
      if (frameSeen(f)) return;
      appendTx(f.source || 'auto', f.tx, f.ts);
      if (dispMode() === 'hex') {
        if (f.hex) append(f.hex + '\n', f.ts, f.port);
      } else if (f.rx) {
        append(f.rx, f.ts, f.port);
      } else if (!f.rx_pending) {
        // 轻模式**同步**收发：这一帧空了就是设备真没回话
        appendNote('（设备没回话）');
      }
    });
    if (dispMode() === 'hex') {
      if (data.rx_hex) append(data.rx_hex + '\n');
    } else if (data.rx_text) {
      append(data.rx_text);
    }
  }

  /** 载入最近 N 条历史（走 /poll?since=0）。重复的跳过，时间戳用帧自己的 ts。 */
  function loadHistory(limit) {
    return api('/poll?since=0').then(function (data) {
      if (!data || !data.ok) return;
      var frames = (data.frames || []).slice(-(limit || 50));
      if (!frames.length) { pushEvent('', '没有历史帧'); return; }
      var added = 0;
      frames.forEach(function (f) {
        if (frameSeen(f)) return;
        added += 1;
        appendTx(f.source || 'auto', f.tx, f.ts);
        if (dispMode() === 'hex') { if (f.hex) append(f.hex + '\n', f.ts, f.port); }
        else if (f.rx) append(f.rx, f.ts, f.port);
      });
      pushEvent('', added
        ? '已载入 ' + added + ' 条历史（重复的已跳过）'
        : '没有新的历史可载入（都已在上面）');
    });
  }

  return { applyPoll: applyPoll, loadHistory: loadHistory, reset: reset };
}
