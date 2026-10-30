// KarenBot WebUI 外壳：登录 → （首次配置）→ 仪表盘路由。
//
// 导航结构照搬原 PySide6 仪表盘（ui/dashboard/layout.py 的 NAV_ITEMS），
// 顺序、中文标题一致，末尾追加 WebUI 新增的「黑名单」。

import { h, fill, qs, formatDuration, empty } from './dom.js';
import { api, endpoints, setUnauthorizedHandler } from './api.js';
import { icon } from './icons.js';
import { EventStream } from './ws.js';

import * as overview from './pages/overview.js';
import * as chats from './pages/chats.js';
import * as memory from './pages/memory.js';
import * as mind from './pages/mind.js';
import * as logs from './pages/logs.js';
import * as personas from './pages/personas.js';
import * as slang from './pages/slang.js';
import * as knowledge from './pages/knowledge.js';
import * as models from './pages/models.js';
import * as emoji from './pages/emoji.js';
import * as containerPage from './pages/container.js';
import * as storage from './pages/storage.js';
import * as settings from './pages/settings.js';
import * as blacklist from './pages/blacklist.js';
import * as setup from './pages/setup.js';

// icon 字段是 icons.js 里的键名，不用 emoji（docs/ui_style_guide.md §六）。
const NAV_ITEMS = [
  { key: 'overview', title: '总览', icon: 'overview', page: overview },
  { key: 'chats', title: '对话', icon: 'chats', page: chats },
  { key: 'memory', title: '记忆', icon: 'memory', page: memory },
  { key: 'persona_mind', title: '人格后台', icon: 'persona_mind', page: mind },
  { key: 'logs', title: '日志', icon: 'logs', page: logs },
  { key: 'personas', title: '人格', icon: 'personas', page: personas },
  { key: 'slang', title: '黑话', icon: 'slang', page: slang },
  { key: 'knowledge', title: '知识', icon: 'knowledge', page: knowledge },
  { key: 'models', title: '模型管理', icon: 'models', page: models },
  { key: 'emoji', title: '表情包', icon: 'emoji', page: emoji },
  { key: 'container', title: '容器', icon: 'container', page: containerPage },
  { key: 'storage', title: '存储与缓存', icon: 'storage', page: storage },
  { key: 'blacklist', title: '黑名单', icon: 'blacklist', page: blacklist },
  { key: 'settings', title: '设置', icon: 'settings', page: settings },
];

const TOAST_MS = 3200;

const state = {
  status: null,
  current: null,
  // 正在渲染的页面。location.hash 赋值会异步触发 hashchange，
  // 需要用它挡住“自己触发自己”的重入（否则深链分页会被二次渲染覆盖）。
  navigating: null,
  navigationSeq: 0,
  pendingSettingsSection: null,
  cleanup: null,
  stream: null,
};

// ============================================================
// 提示条
// ============================================================

function toast(message, kind = '') {
  const host = qs('#toast-host');
  const el = h('div', { class: `toast ${kind}` }, message);
  host.append(el);
  setTimeout(() => el.remove(), TOAST_MS);
}

// ============================================================
// 顶栏 / 侧栏状态
// ============================================================

function applyStatus(status) {
  state.status = status;

  const adapterBadge = qs('#adapter-badge');
  const connected = !!status.adapter_connected;
  const adapter = status.adapter;
  // 徽章四态对齐 docs/ui_style_guide.md §五：
  // ok 苔色绿实心 / warn 栀子黄实心 / bad 朱砂红实心 / idle 描边同色字
  if (status.state !== 'running') {
    const failed = status.state === 'error';
    adapterBadge.textContent = failed ? '启动失败' : '未运行';
    adapterBadge.className = `badge ${failed ? 'bad' : 'idle'}`;
  } else if (!adapter) {
    adapterBadge.textContent = '未配置渠道';
    adapterBadge.className = 'badge warn';
  } else {
    adapterBadge.textContent = connected
      ? `已就位 · ${adapter.mode}://${adapter.host}:${adapter.port}`
      : '渠道未连接';
    adapterBadge.className = `badge ${connected ? 'ok' : 'idle'}`;
  }

  qs('#persona-label').textContent = `当前人格：${status.persona || '—'}`;

  const dot = qs('#side-dot');
  const text = qs('#side-conn-text');
  if (status.state === 'running') {
    dot.className = 'dot ok';
    text.textContent = `运行中 · ${formatDuration(status.uptime_seconds)}`;
  } else if (status.state === 'error') {
    dot.className = 'dot bad';
    text.textContent = '启动失败';
  } else {
    dot.className = 'dot warn';
    text.textContent = 'Runtime 未运行';
  }
}

// ============================================================
// 路由
// ============================================================

function currentRouteKey() {
  const fromHash = location.hash.replace(/^#\/?/, '');
  return NAV_ITEMS.some((x) => x.key === fromHash) ? fromHash : 'overview';
}

async function navigate(key, { force = false, section = null } = {}) {
  const item = NAV_ITEMS.find((x) => x.key === key);
  if (!item) return;
  if (section && key === 'settings') {
    state.pendingSettingsSection = String(section);
  }
  // navigating 期间允许切回上一页：此时 state.current 仍是上一页，
  // 只看 current 会把“返回/再点旧页”误判成重复点击。
  if (!force && state.navigating === null && state.current === key && !section) return;
  if (!force && state.navigating === key && !section) return;

  if (typeof state.cleanup === 'function') {
    try { state.cleanup(); } catch (e) { console.error(e); }
    state.cleanup = null;
  }

  location.hash = `#/${key}`;

  for (const btn of document.querySelectorAll('.nav-item')) {
    btn.classList.toggle('active', btn.dataset.key === key);
  }

  // 每次导航换一个新的 #content 节点：旧页面即使还在 await，
  // 后续写 DOM 也只会写进已经脱离文档的旧节点，不会覆盖新页面。
  const previousContainer = qs('#content');
  const container = previousContainer.cloneNode(false);
  previousContainer.replaceWith(container);
  fill(container);

  const requestedSection = state.pendingSettingsSection;
  state.pendingSettingsSection = null;
  const ctx = {
    toast,
    status: state.status,
    navigate,
    section: requestedSection,
    onEvent: (handler) => state.stream.subscribe(handler),
  };

  state.navigating = key;
  const seq = ++state.navigationSeq;
  try {
    const result = await item.page.render(container, ctx);
    // 渲染成功才认账。之前是进函数就写 state.current，一旦某页渲染抛错，
    // state.current 已经等于该页 key，再点同一个导航项会被
    // `state.current === key` 提前 return，用户只能刷新页面才能重试。
    if (seq !== state.navigationSeq) {
      // 已被更新的导航取代：释放这一页的定时器/订阅，避免后台常驻。
      if (result && typeof result.destroy === 'function') {
        try { result.destroy(); } catch (e) { console.error(e); }
      }
      return;
    }
    state.current = key;
    state.cleanup = result && result.destroy ? result.destroy : null;
  } catch (e) {
    console.error(e);
    if (seq !== state.navigationSeq) return;
    state.current = null;
    state.cleanup = null;
    fill(container, h('div', { class: 'empty' }, `页面加载失败：${e.message}`));
  } finally {
    if (seq === state.navigationSeq) state.navigating = null;
  }
}

function buildNav() {
  const nav = qs('#nav');
  fill(nav, ...NAV_ITEMS.map((item) =>
    h('button', {
      class: 'nav-item',
      dataset: { key: item.key },
      title: item.title,
      'aria-label': item.title,
      onclick: () => navigate(item.key),
    },
      h('span', { class: 'nav-icon' }, icon(item.icon)),
      h('span', {}, item.title),
    ),
  ));
}

// ============================================================
// 侧栏折叠：折叠后只显示图标，状态记在 localStorage
// ============================================================

function applySidebarCollapsed(collapsed, { persist = true } = {}) {
  const sidebar = qs('#sidebar');
  const button = qs('#btn-sidebar');
  if (!sidebar || !button) return;
  sidebar.classList.toggle('collapsed', collapsed);
  fill(button, icon(collapsed ? 'chevron-right' : 'chevron-left', { size: 18 }));
  button.title = collapsed ? '展开侧栏' : '折叠侧栏';
  button.setAttribute('aria-label', button.title);
  button.setAttribute('aria-expanded', String(!collapsed));
  if (persist) localStorage.setItem('karen-sidebar-collapsed', collapsed ? '1' : '0');
}

function initSidebar() {
  const button = qs('#btn-sidebar');
  if (!button) return;
  applySidebarCollapsed(localStorage.getItem('karen-sidebar-collapsed') === '1',
    { persist: false });
  button.addEventListener('click', () => {
    const collapsed = !qs('#sidebar').classList.contains('collapsed');
    applySidebarCollapsed(collapsed);
  });
}

// ============================================================
// 主题
// ============================================================

function applyTheme(theme) {
  const resolved = theme === 'auto'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : theme;
  document.documentElement.dataset.theme = resolved;
  // 图标改成 SVG 后不能再靠 textContent 表示状态，同时更新 title 兜底
  const btn = qs('#btn-theme');
  fill(btn, icon(resolved === 'dark' ? 'sun' : 'moon', { size: 18 }));
  btn.title = resolved === 'dark' ? '切换到浅色主题' : '切换到深色主题';
  localStorage.setItem('karen-theme', theme);
}

function initTheme() {
  applyTheme(localStorage.getItem('karen-theme') || 'auto');
  window.matchMedia('(prefers-color-scheme: dark)')
    .addEventListener('change', () => applyTheme(localStorage.getItem('karen-theme') || 'auto'));
}

// ============================================================
// 启动流程
// ============================================================

let loadingHintTimer = null;

async function boot() {
  initTheme();
  initSidebar();

  // 先把外壳亮出来再问后端。
  // 之前是 await 完 /api/auth/state 和 /api/status 才决定显示哪个界面，
  // 后端只要卡住（探测密钥、换模型、本机代理挂死），两个 await 都不 settle，
  // 三个顶层容器就一直是 hidden —— 页面纯白，且没有任何提示。
  showLoading();

  try {
    const auth = await endpoints.authState();
    if (auth.auth_enabled && !auth.authenticated) {
      showLogin();
      return;
    }
    await enterApp();
  } finally {
    clearTimeout(loadingHintTimer);
  }
}

function showLoading() {
  qs('#login-gate').classList.add('hidden');
  qs('#setup-gate').classList.add('hidden');
  qs('#shell').classList.remove('hidden');

  const content = qs('#content');
  fill(content, h('div', { class: 'page' },
    empty('正在连接 Karen 服务…', {
      subtitle: '启动 Runtime 可能要几秒，稍等一下。',
      iconName: 'overview',
    }),
  ));
  const hint = content.querySelector('.empty-sub');

  // 后端慢到 20 秒还没回应时补一句，让用户知道是后端卡住而不是浏览器死了。
  loadingHintTimer = setTimeout(() => {
    if (hint) hint.textContent = '还在等后端回应。若一直停在这里，请检查启动窗口是否报错。';
  }, 20000);
}

/** 连不上后端时的可见兜底：给出原因和「重试」，而不是留一片白。 */
function showBootError(error) {
  qs('#login-gate').classList.add('hidden');
  qs('#setup-gate').classList.add('hidden');
  qs('#shell').classList.remove('hidden');

  fill(qs('#content'), h('div', { class: 'page' },
    h('div', { class: 'empty' },
      icon('settings', { size: 32, className: 'empty-icon' }),
      h('p', { class: 'empty-title' }, '连不上 Karen 服务'),
      h('p', { class: 'empty-sub' }, error?.message || String(error)),
      h('button', {
        class: 'btn btn-primary',
        style: { marginTop: '20px' },
        onclick: () => location.reload(),
      }, '重试'),
    ),
  ));
}

function showLogin() {
  qs('#login-gate').classList.remove('hidden');
  qs('#shell').classList.add('hidden');
  qs('#setup-gate').classList.add('hidden');
  qs('#version-mark').classList.add('hidden');
  qs('#login-token').focus();

  qs('#login-form').onsubmit = async (event) => {
    event.preventDefault();
    const errorBox = qs('#login-error');
    errorBox.textContent = '';
    try {
      await endpoints.login(qs('#login-token').value.trim());
      qs('#login-gate').classList.add('hidden');
      await enterApp();
    } catch (e) {
      // enterApp 失败时 login-gate 已经隐藏、shell 又还没就绪；
      // 不恢复登录界面的话三个顶层容器全是 hidden，页面直接空白。
      showLogin();
      qs('#login-error').textContent = e.message;
    }
  };
}

function showSetup() {
  qs('#login-gate').classList.add('hidden');
  qs('#shell').classList.add('hidden');
  qs('#setup-gate').classList.remove('hidden');
  qs('#version-mark').classList.add('hidden');
  setup.render(qs('#setup-body'), {
    toast,
    onDone: async () => {
      // 不要提前隐藏 setup-gate：enterApp 失败时错误还要显示在向导里。
      // enterApp 成功后会自己隐藏它；仍处于 setup_required 时它会重新显示。
      try {
        await enterApp();
      } catch (e) {
        qs('#setup-gate').classList.remove('hidden');
        throw e;
      }
    },
  });
}

async function enterApp() {
  const status = await endpoints.status();

  if (status.setup_required) {
    showSetup();
    return;
  }

  qs('#setup-gate').classList.add('hidden');
  qs('#shell').classList.remove('hidden');

  if (!state.stream) {
    state.stream = new EventStream();
    state.stream.onConnectionChange = (connected) => {
      if (!connected && state.status) {
        qs('#side-dot').className = 'dot bad';
        qs('#side-conn-text').textContent = '控制台连接断开，重连中…';
      }
    };
    // 外壳只关心 status；chat / log 由各页面通过 ctx.onEvent 自行订阅。
    state.stream.subscribe((event) => {
      if (event.type === 'status') applyStatus(event.data);
    });
    state.stream.connect();
  }

  buildNav();
  applyStatus(status);
  loadVersionMark();

  if (status.last_error) {
    toast(`Runtime 启动失败：${status.last_error}`, 'error');
  }
  for (const warning of status.warnings || []) {
    toast(warning, 'warn');
  }

  await navigate(currentRouteKey(), { force: true });
}

async function loadVersionMark() {
  const el = qs('#version-mark');
  if (!el) return;
  let info = null;
  try {
    info = await endpoints.updateVersion();
  } catch {
    // 版本接口失败不影响控制台；保留占位但不显示错误的版本号。
  }
  el.textContent = (info && info.version_label) || '开发版';
  el.title = info && info.commit_short
    ? `开发版 · 源码 ${info.commit_short}`
    : '开发版';
  el.classList.remove('hidden');
}

function wireChrome() {
  window.addEventListener('hashchange', () => navigate(currentRouteKey()));

  // 顶栏图标（index.html 里只留空的 button，图标在这里挂，才能复用 icons.js）
  fill(qs('#btn-restart'), icon('restart', { size: 18 }));
  fill(qs('#btn-logout'), icon('logout', { size: 18 }));
  applyTheme(localStorage.getItem('karen-theme') || 'auto');

  qs('#btn-theme').onclick = () => {
    const order = ['auto', 'light', 'dark'];
    const now = localStorage.getItem('karen-theme') || 'auto';
    const next = order[(order.indexOf(now) + 1) % order.length];
    applyTheme(next);
    // 顺手同步到配置，让同一实例的其它客户端/桌面端保持一致。
    if (state.status && state.status.state === 'running') {
      api.patch('/api/settings', { path: 'app.theme', value: next }).catch(() => {});
    }
  };

  qs('#btn-restart').onclick = async (event) => {
    const btn = event.currentTarget;
    if (!confirm('重启 Runtime？正在处理的对话会被中断。\n\n'
      + '只重建运行时，不会重新加载 Python 代码；更新代码后请使用“重启后端”。')) return;
    btn.disabled = true;
    try {
      await endpoints.restart();
      toast('Runtime 已重启', 'ok');
      applyStatus(await endpoints.status());
      await navigate(state.current, { force: true });
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
    }
  };

  qs('#btn-logout').onclick = async () => {
    try { await endpoints.logout(); } catch { /* 已失效也无所谓 */ }
    if (state.stream) { state.stream.close(); state.stream = null; }
    showLogin();
  };

  setUnauthorizedHandler(() => {
    if (state.stream) { state.stream.close(); state.stream = null; }
    showLogin();
  });
}

wireChrome();
boot().catch((e) => {
  console.error(e);
  // 后端已经在跑了（拿到了响应）但启动流程出错：留在控制台里报错，
  // 不要整页替换掉——那样连侧栏都没了，看着也像白屏。
  showBootError(e);
});
