/* 鲸歌工具层 · 结构化错误渲染（从 index.js 搬出来，纯搬移）。
 * KIND_ADVICE 是 kind → 给模型的一句话行动建议；renderError 把 BridgeError
 * 渲染成「是什么 + 请检查 + 候选 + 下一步」四段，模型照着就能动。
 */

export const KIND_ADVICE = {
  no_device: '设备没找到：确认 USB 插好、驱动装好；用 serial_list 看当前有哪些串口。',
  ambiguous_device: '匹配到多个设备：让用户选一个，写进场景 config.json 的 com 字段。',
  port_busy: '口被占用：关掉串口助手 / 网页监视模式后重试。',
  no_response: '设备没回话：检查波特率、供电、TX/RX 是否接反、换行结尾。',
  scene_missing: '场景名不对：用 serial_list 看 scenes 里有哪些可用场景。',
  control_blocked: '控制权不在 AI：需要人类在网页上点[交给AI]或[继续AI]。',
  allowlist_blocked: '自动命令不在白名单：把该命令加进场景配置的 watch.allowed_auto_commands。',
  config: '场景配置有问题：按提示改 protocols/<场景>/config.json。',
  internal: '内部错误：把 message 与 detail 一起反馈。',
};


/* 1.36：失败时必须给出**下一条可执行调用**，不能只写"请联系管理员"。
 * next() 返回一个对象：{ tool, args } —— args 拿去就能直接调。
 * 优先用后端给的 candidates（真实口号），没有才用占位。
 */
function firstCom(error) {
  const list = Array.isArray(error?.candidates) ? error.candidates : [];
  for (const item of list) {
    const device = typeof item === 'string' ? item : item?.device;
    if (device) return device;
  }
  return '';
}

export function nextStep(kind, error) {
  const com = firstCom(error);
  switch (kind) {
    case 'ambiguous_device':
      return {
        tool: 'serial_send',
        args: com ? { com, command: 'AT' } : { com: 'COM1', command: 'AT' },
        why: com ? `先给 ${com} 发一条 AT，看是不是你要的那个设备` : '给候选口各发一条 AT 试出来',
      };
    case 'no_device':
      return { tool: 'serial_list', args: {}, why: '先看当前插着哪些口，再按 com 直接发' };
    case 'no_response':
      return {
        tool: 'serial_send',
        args: { ...(com ? { com } : {}), baud: 9600, command: 'AT' },
        why: '波特率可能不对：用 baud 覆盖逐个试 9600 → 115200（不改配置）',
      };
    case 'port_busy':
      return { tool: 'serial_status', args: {}, why: '看谁在占口（网页监视模式 / 串口助手），关掉再发' };
    case 'control_blocked':
      return { tool: 'serial_control', args: { action: 'ai' }, why: '先把控制权交给 AI' };
    case 'scene_missing':
      return { tool: 'serial_list', args: {}, why: '看有哪些场景；也可以不给场景，直接给 com' };
    default:
      return null;
  }
}


export function renderError(kind, error, fallback) {
  const lines = [];
  const advice = KIND_ADVICE[kind];
  lines.push(error?.message || fallback || '鲸歌调用失败');
  if (Array.isArray(error?.hint) && error.hint.length > 0) {
    lines.push('请检查：');
    for (const [index, item] of error.hint.entries()) lines.push(`  ${index + 1}. ${item}`);
  }
  if (Array.isArray(error?.candidates) && error.candidates.length > 0) {
    lines.push('候选设备：');
    for (const item of error.candidates) {
      const device = item?.device ?? '?';
      const extra = [item?.description, item?.vid_pid].filter(Boolean).join('  ');
      lines.push(`  ${device}${extra ? '  ' + extra : ''}`);
    }
  }
  if (advice) lines.push(`下一步：${advice}`);
  const next = nextStep(kind, error);
  if (next) {
    const argText = JSON.stringify(next.args);
    lines.push(`可直接调：${next.tool}(${argText})`);
    if (next.why) lines.push(`为什么：${next.why}`);
  }
  if (error?.detail) lines.push(`细节：${error.detail}`);
  return lines.join('\n');
}
