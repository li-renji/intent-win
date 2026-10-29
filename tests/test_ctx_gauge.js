/* 前端上下文环回归测试：加载真实 ui/index.html，用 jsdom 验证 gauge 口径。 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'ui', 'index.html'), 'utf8');

const PASS = [], FAIL = [];
function check(name, cond, extra) {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? '  ✓ ' : '  ✗ ') + name + (extra !== undefined ? '  -> ' + extra : ''));
}

const dom = new JSDOM(html, {
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  url: 'http://localhost/',          // 给 localStorage 一个可用 origin
});
const win = dom.window;
const doc = win.document;

// ---- 桩掉 pywebview 桥与浏览器 API ----
const pushed = [];
win.pywebview = {
  api: {
    get_status: async () => ({
      engine: 'cloud', llm: true, browser: false, browser_text: '未打开',
      tools: 'fs', ctx: 32768, ctx_base: 1500,
    }),
    list_plugins: async () => ({ ok: true, items: [] }),
    get_settings: async () => ({ profiles: [] }),
    list_conversations: async () => ({ ok: true, items: [] }),
  },
};
win.eval(html.match(/<script[^>]*>([\s\S]*?)<\/script>/g)
  .map(s => s.replace(/<\/?script[^>]*>/g, '')).join('\n;\n'));

const $ = id => doc.getElementById(id);
const num = () => $('ctxNum').textContent;

// ---- 场景 1：初始状态 ----
check('初始显示 0%', num() === '0%', num());

// ---- 场景 2：服务端下发快照后，容量应被采用 ----
win.renderStatus({ engine:'cloud', llm:true, browser:false, browser_text:'未打开',
                   tools:'fs', ctx:32768, ctx_base:1500 });
check('容量取服务端值 32768', win._ctx === 32768, win._ctx);
check('固定开销已记录', win._ctxBase === 1500, win._ctxBase);

// ---- 场景 3：粘贴一篇长论文到输入框，环应立刻上涨（原来恒为 0%）----
const paper = '这是一篇很长的论文内容，'.repeat(4000);   // ≈ 5.2 万 token
const ta = $('input');
ta.value = paper;
ta.dispatchEvent(new win.Event('input', { bubbles: true }));
const pctAfterPaste = parseInt(num(), 10);
check('粘贴长论文后上下文环立即上涨（不再显示 0%）', pctAfterPaste > 50, num());
check('环不会溢出显示超过 100%', pctAfterPaste <= 100, num());

// ---- 场景 4：服务端精确快照到达后，覆盖本地估算 ----
win.__onCtx({ capacity: 32768, used: 30000, pct: 91.5 });
check('服务端快照后 used 采用服务端值', win._ctxUsed === 30000, win._ctxUsed);
check('接近上限时环显示 >=90%', parseInt(num(), 10) >= 90, num());
check('超限变色（danger）',
      $('ctxArc').style.stroke === 'var(--danger)', $('ctxArc').style.stroke);

// ---- 场景 5：清空输入 + 清空快照后回到低值 ----
ta.value = '';
ta.dispatchEvent(new win.Event('input', { bubbles: true }));
win._ctxUsed = 0;
win.recalcCtx();
check('清空后回到低占用', parseInt(num(), 10) < 50, num());

// ---- 场景 6：窗口标题写明容量与占用，便于排查 ----
win.__onCtx({ capacity: 32768, used: 12345, pct: 37.7 });
const tip = $('ctxGauge').getAttribute('title');
check('tooltip 含容量与占用', /32K/.test(tip) && /12/.test(tip), tip);
check('tooltip 说明含系统提示与工具定义', /系统提示/.test(tip), tip);

console.log('\n=== 结果 ===');
console.log('通过 ' + PASS.length + ' / ' + (PASS.length + FAIL.length));
if (FAIL.length) { console.log('失败：' + FAIL.join(', ')); process.exit(1); }
console.log('全部通过');
process.exit(0);
