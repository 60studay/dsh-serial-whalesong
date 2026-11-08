/**
 * 角标（1.26 从 app.js 摘出来）：把"一行常显解释"批量收成 ⓘ，并挂上展开逻辑。
 *
 * 规则：点角标 = 开/关；点别处 = 收起；Esc = 收起。
 * 气泡是绝对定位的，展开时不挤邻居（撑开布局是最容易做错的地方）。
 *
 * 为什么在 JS 里搬文字而不是直接在 HTML 写 data-tip：
 * HTML 里留着原文，改文案时不用两头对照；要还原某个说明，删掉 `data-tipify` 一个属性就行。
 */

export function initTips() {
  // ① 批量转换：`.hint[data-tipify]` → 16px 的 ⓘ
  document.querySelectorAll('[data-tipify]').forEach(function (el) {
    var text = (el.textContent || '').trim();
    if (!text) { el.remove(); return; }
    el.setAttribute('data-tip', text);
    el.setAttribute('aria-label', text);
    el.classList.remove('hint');
    el.classList.add('tip');
    el.textContent = '';
    el.tabIndex = 0;
    // 只把**字段级**的说明挪到标签后面（"显示模式 ⓘ"）。
    // 小组级的说明就地留一个 ⓘ —— 否则会全挤到同一个标题后面（试过，一排三个角标，很难看）。
    var host = el.closest('.field');
    var label = host ? host.querySelector(':scope > span, :scope > .title') : null;
    if (label) {
      label.appendChild(document.createTextNode(' '));
      label.appendChild(el);
    }
  });

  // ② 触屏没有 hover，所以点一下也要能展开
  document.addEventListener('click', function (ev) {
    document.querySelectorAll('.tip.open').forEach(function (tip) {
      if (!tip.contains(ev.target)) tip.classList.remove('open');
    });
  });
  document.querySelectorAll('.tip').forEach(function (tip) {
    tip.addEventListener('click', function (ev) {
      ev.stopPropagation();
      tip.classList.toggle('open');
    });
    tip.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') tip.classList.remove('open');
    });
  });
}
