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
  var divider = opts.divider || function () {};
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

  /** 载入最近 N 条历史：**先内存环，再磁盘日志**（1.80）。
   *
   *  为什么两处都要：内存环只留实时（页面没开就丢），磁盘日志才是留档。
   *  用户实测过"CLI 里配了蓝牙，页面打开什么都没有" —— 那些命令只在日志里。
   */
  function loadHistory(limit, opts) {
    var auto = !!(opts && opts.auto);
    return Promise.all([
      api('/poll?since=0').catch(function () { return { ok: true, frames: [] }; }),
      api('/history?lines=' + (limit || 50)).catch(function () { return { ok: false }; })
    ]).then(function (results) {
      var memory = (results[0] && results[0].frames) || [];
      var disk = (results[1] && results[1].frames) || [];
      var merged = memory.concat(disk).sort(function (a, b) { return (a.ts || 0) - (b.ts || 0); });
      return finishHistory(merged, limit, disk.length, auto);
    });
  }

  function finishHistory(framesAll, limit, diskCount, auto) {
    var frames = (framesAll || []).slice(-(limit || 50));
    var added = 0;
    var firstLogRow = null;
    frames.forEach(function (f) {
      if (frameSeen(f)) return;
      // 自动补的那一段先打一条分隔线，明确"以下是页面打开前发生的"
      if (auto && f.from_log && !firstLogRow) {
        firstLogRow = true;
        divider('以下是页面打开前的记录（来自日志）');
      }
      added += 1;
      appendTx(f.from_log ? 'log' : (f.source || 'auto'), f.tx, f.ts);
      if (dispMode() === 'hex') { if (f.hex) append(f.hex + '\n', f.ts, f.port, f.from_log); }
      else if (f.rx) append(f.rx, f.ts, f.port, f.from_log);
    });
    if (!auto) {
      // 1.83：条数口径写清（去重后 / 原始）
      pushEvent('', added
        ? ('已载入 ' + added + ' 条' + (diskCount ? '（去重后；日志原始 ' + diskCount + ' 条）' : '') )
        : '没有新的历史可载入（都已在上面）');
    }
    return true;
  }

  function _oldLoadHistoryRemoved(limit) {
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
