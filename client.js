/**
 * dsh-whalesong（鲸歌）—— 浏览器半端。
 *
 * 只做一件事：在 DSH 左侧 → 插件 → 鲸歌串口 → 插件详情页的配置区，放一个
 * 「自动打开网页」开关。改了立刻保存，没有"保存"按钮。
 *
 * 为什么要有这一页：轻度用户不想被弹窗打扰，重度用户又懒得每次自己开——
 * 那就把选择权交给用户，默认关。
 *
 * 三条自我约束（吃过白屏的亏）：
 *   ① 整个工厂 + apply 全包 try/catch：客户端挂掉只该让"设置页消失"，绝不能拖垮 DSH。
 *   ② 不碰 plugins.bundle.activation：不做首次引导、不弹任何提示。
 *   ③ 不在这一页放端口/场景/连接状态——那是 8898 网页的事，混进来就长成第二个后台了。
 *
 * 数据从 /api/whalesong/prefs 来（host 侧转给 Python 内核，单一实现、原子写）。
 */
window.__ModuleLoader__.load({
  id: 'dsh-whalesong',
  factory: (require) => {
    try {
      const React = require('react');
      const h = React.createElement;

      // 挂载 key = 包名（DSH 按包名把这一格派发到插件详情页）
      const PKG = 'dsh-whalesong';
      const API = '/api/whalesong';

      const jget = async (p) => (await fetch(API + p, { credentials: 'same-origin' })).json();
      const jpost = async (p, body) => (await fetch(API + p, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body ?? {}),
      })).json();

      const CSS = `
[data-ws-page]{display:flex;flex-direction:column;gap:8px;padding:4px 2px 8px;max-width:640px;font-size:13px;}
[data-ws-title]{font-size:14px;font-weight:600;}
[data-ws-note]{font-size:12px;line-height:18px;color:var(--dsw-alias-label-tertiary,#8a9099);}
[data-ws-row]{display:flex;align-items:center;gap:10px;min-height:26px;flex-wrap:wrap;}
[data-ws-row] input[type='checkbox']{width:16px;height:16px;accent-color:#3c9a5f;cursor:pointer;flex:none;}
[data-ws-row] input[type='checkbox']:disabled{cursor:default;opacity:.5;}
[data-ws-state]{font-size:12px;min-width:28px;color:var(--dsw-alias-label-secondary,#6b7280);}
[data-ws-saving]{font-size:11px;color:var(--dsw-alias-label-tertiary,#8a9099);}
[data-ws-saved]{font-size:11px;color:#3c9a5f;}
[data-ws-error]{font-size:12px;line-height:18px;color:#d9534f;word-break:break-all;}
`;

      function Panel() {
        const [value, setValue] = React.useState(null); // null = 还没读出来
        const [busy, setBusy] = React.useState(false);
        const [error, setError] = React.useState('');
        const [saved, setSaved] = React.useState(false);

        React.useEffect(() => {
          let alive = true;
          jget('/prefs')
            .then((json) => {
              if (!alive) return;
              if (json && json.ok && json.data) {
                setValue(json.data.auto_open_browser === true);
              } else {
                // 读不到就说清楚，不要假装是"关"
                setError((json && json.error) || '读不到当前的设置');
              }
            })
            .catch((err) => { if (alive) setError(String(err?.message || err)); });
          return () => { alive = false; };
        }, []);

        const onToggle = async (next) => {
          setBusy(true);
          setError('');
          setSaved(false);
          try {
            const json = await jpost('/prefs', { auto_open_browser: next });
            if (json && json.ok) {
              setValue(next);
              setSaved(true);
            } else {
              // 保存失败必须原地说原因（bili-watch 有过静默丢 patch 的历史）
              setError((json && json.error) || '保存失败（后端没有返回原因）');
            }
          } catch (err) {
            setError(String(err?.message || err));
          } finally {
            setBusy(false);
          }
        };

        return h('div', { 'data-ws-page': '' },
          h('div', { 'data-ws-title': '' }, '自动打开网页'),
          h('div', { 'data-ws-note': '' },
            '关 = AI 起网页时只给你链接，不弹窗口；开 = 自动弹出来，省一次点击'),
          h('div', { 'data-ws-row': '' },
            h('input', {
              type: 'checkbox',
              'data-ws-switch': '',
              checked: value === true,
              disabled: busy || value === null,
              onChange: (event) => { onToggle(event.target.checked); },
            }),
            h('span', { 'data-ws-state': '' },
              value === null ? '读取中…' : (value ? '开' : '关')),
            busy ? h('span', { 'data-ws-saving': '' }, '保存中…') : null,
            saved && !busy ? h('span', { 'data-ws-saved': '' }, '已保存') : null,
          ),
          error ? h('div', { 'data-ws-error': '' }, error) : null,
        );
      }

      return {
        inject: ['slots'],
        apply(ctx) {
          // 这一层单独 try：样式/槽位出问题也只丢设置页
          try {
            const style = document.createElement('style');
            style.setAttribute('data-plugin-css', 'dsh-whalesong');
            style.textContent = CSS;
            document.head.appendChild(style);
            if (typeof ctx.effect === 'function') {
              ctx.effect(() => () => { try { style.remove(); } catch { /* 已移除 */ } }, 'dsh-whalesong: 设置页样式');
            }

            const slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots;
            if (!slots) return;
            slots.inject('plugins.bundle.config', () => slots.register(
              { name: 'plugins.bundle.config', key: PKG },
              Panel,
            ));
          } catch (err) {
            console.warn('[dsh-whalesong] 设置页注册失败（串口功能不受影响）：', err);
          }
        },
      };
    } catch (error) {
      // 兜底：宁可没有设置页，也不能让整个界面白屏
      console.warn('[dsh-whalesong] 客户端加载失败，设置页不可用（串口功能不受影响）：', error);
      return { apply() {} };
    }
  },
});
