// Releases 版本选择器。
//
// 后端把远端 tag 当作版本目录，更新日志来自仓库里的 releases/<tag>.md，
// GitHub Releases 使用同一组 tag。这里同时服务两处：
//   - 总览页绿色更新标志 → 弹窗；
//   - 设置 → 更新 → 内嵌版本列表。
//
// 应用版本会重启服务；所有版本都先备份当前 HEAD，用户数据目录不受影响。

import { h, fill, modal, empty } from './dom.js';
import { endpoints } from './api.js';
import { icon } from './icons.js';

const RELATION_TEXT = {
  current: '当前版本',
  newer: '可更新',
  older: '可回退',
  diverged: '另一条历史',
};

const REASON_TEXT = {
  up_to_date: '当前已经是最新版本',
  dependency_change_requires_install: '目标版本修改了依赖文件，需要手动安装依赖',
  worktree_dirty: '工作树有未提交改动',
  data_dir_inside_project: '用户数据目录位于代码工作树内',
  git_timeout: '连接远端超时',
  git_failed: 'git 拉取失败',
  release_not_found: '远端找不到这个版本',
  rollback_not_confirmed: '回退/切换操作需要确认',
  legacy_unknown: '目标版本没有兼容声明，不能自动应用',
  migration_required: '目标版本需要数据迁移，自动更新已阻止',
  downgrade_required: '目标版本不能安全读取当前数据版本',
  data_contract_too_new: '目标版本不认识当前较新的数据契约',
  unsupported_data_contract: '目标版本不支持当前数据版本',
  feature_missing: '目标版本需要本地尚不支持的数据能力',
  data_unreadable: '本地数据版本不可读',
  launcher_protocol_too_old: '启动保护协议过旧',
  data_state_changed: '检查后本地数据已变化，请重新检查',
  compat_manifest_changed: '目标兼容声明已变化，请重新检查',
  manifest_invalid: '目标兼容声明无效',
  unsupported_manifest_format: '目标兼容声明格式不受支持',
  unknown_plane: '目标兼容声明包含未知数据平面',
};

function pad(value) {
  return String(value).padStart(2, '0');
}

function formatDateTime(value) {
  if (value === null || value === undefined || value === '') return '—';
  let date;
  if (typeof value === 'number') date = new Date(value * 1000);
  else {
    const asNumber = Number(value);
    date = Number.isFinite(asNumber) && String(value).length >= 10
      ? new Date(asNumber * 1000)
      : new Date(value);
  }
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function reasonText(reason) {
  return REASON_TEXT[reason] || reason || '未知原因';
}

function relationBadge(release) {
  const kind = release.is_current ? 'current' : release.relation;
  const text = RELATION_TEXT[kind] || kind;
  const cls = kind === 'newer' ? 'ok' : (kind === 'older' ? 'warn' : '');
  return h('span', { class: `badge ${cls}` }, text);
}

function notesBlock(release) {
  const text = String(release.notes || '').trim();
  const placeholder = text || '该版本没有提供更新日志。';
  const lineCount = String(placeholder).split('\n').length;
  const long = placeholder.length > 260 || lineCount > 4;
  const notes = h('div', {
    class: `release-notes${long ? ' collapsed' : ''}`,
  }, placeholder);
  const box = h('div', { class: 'release-notes-box' }, notes);
  if (long) {
    // 同时写内联样式：WebUI 没有构建哈希，浏览器若缓存了旧 app.css，
    // 只切换 class 会“看起来没反应”。内联样式保证折叠/展开功能本身始终生效。
    notes.style.maxHeight = '5.2em';
    notes.style.overflow = 'hidden';
    const toggle = h('button', { class: 'btn btn-sm release-notes-toggle' },
      h('span', {}, '展开'),
      icon('chevron-down', { size: 14 }));
    toggle.addEventListener('click', () => {
      const collapsed = notes.classList.toggle('collapsed');
      notes.style.maxHeight = collapsed ? '5.2em' : 'none';
      notes.style.overflow = collapsed ? 'hidden' : 'visible';
      fill(toggle,
        h('span', {}, collapsed ? '展开' : '收起'),
        icon(collapsed ? 'chevron-down' : 'chevron-up', { size: 14 }));
    });
    box.append(toggle);
  }
  return box;
}

function releaseCard(release, { busy, onApply }) {
  const title = release.name || release.tag;
  const actionLabel = release.relation === 'older'
    ? '回退到此版本'
    : (release.relation === 'diverged' ? '切换到此版本' : '更新到此版本');
  const compat = release.compat || {};
  // 当前正在使用的版本即使没有 manifest，也不在列表里报“不可应用”。
  const blocked = !release.is_current && compat.compatible === false;
  const action = release.is_current
    ? h('span', { class: 'badge ok' }, '当前版本')
    : h('button', {
      class: 'btn btn-sm btn-primary',
      disabled: busy || blocked,
      title: blocked ? (compat.message || '目标版本与当前数据不兼容') : '',
      onclick: () => onApply(release),
    }, blocked ? '不可自动应用' : actionLabel);
  return h('article', { class: 'release-item' },
    h('div', { class: 'release-head' },
      h('div', { class: 'release-title-row' },
        h('strong', { class: 'release-title' }, title),
        blocked
          ? h('span', {
            class: 'badge warn',
            title: compat.message || '目标版本与当前数据不兼容',
          }, '兼容性未通过')
          : relationBadge(release),
        release.is_current ? h('span', { class: 'badge' }, '正在使用') : null,
      ),
      h('div', { class: 'release-meta' },
        h('span', { class: 'mono' }, release.tag),
        release.commit_short ? h('span', { class: 'mono' }, release.commit_short) : null,
        release.published_at
          ? h('span', {}, formatDateTime(release.published_at))
          : null,
      ),
    ),
    notesBlock(release),
    h('div', { class: 'release-actions' }, action),
  );
}

/**
 * 把版本列表渲染进 host。
 *
 * 返回 { refresh }，便于设置页重新拉取。弹窗关闭时的“之后不再提示”由调用方处理。
 */
export async function renderReleaseList(
  host,
  ctx,
  { showNotifyToggle = false, onApplied = null } = {},
) {
  let busy = false;
  let applied = false;
  let available = null;
  let listing = null;
  const message = h('p', { class: 'field-help release-message' });
  const summaryHost = h('div', { class: 'release-summary' });
  const listHost = h('div', { class: 'release-list' });
  const errorHost = h('div', {});

  function setMessage(text, kind = '') {
    message.textContent = text || '';
    message.className = `field-help release-message${kind ? ` ${kind}` : ''}`;
  }

  function renderSummary() {
    if (!available) return;
    const checked = available.last_check_at
      ? formatDateTime(available.last_check_at) : '尚未检查';
    const next = available.next_check_at
      ? formatDateTime(available.next_check_at) : '—';
    const release = available.release;
    const notice = available.available && release
      ? `发现新版本：${release.name || release.tag}`
      : (available.suppressed
        ? '更新提醒已关闭；可在下方重新开启。'
        : '当前没有待提示的更新。');
    let toggle = null;
    if (showNotifyToggle) {
      const input = h('input', {
        type: 'checkbox',
        checked: !available.suppressed,
      });
      input.addEventListener('change', async () => {
        input.disabled = true;
        try {
          available = await endpoints.updatePreferences({
            suppress_badge: !input.checked,
          });
          ctx.toast(input.checked ? '已开启更新提醒' : '已关闭更新提醒', 'ok');
          renderSummary();
        } catch (e) {
          input.checked = !input.checked;
          ctx.toast(e.message, 'error');
        } finally {
          input.disabled = false;
        }
      });
      toggle = h('label', { class: 'switch release-notify' },
        input, h('span', {}, '接收更新提醒'));
    }
    fill(summaryHost,
      h('p', { class: 'release-summary-line' }, notice),
      h('p', { class: 'field-help' },
        `版本通道：${available.version_label || '开发版'} · `
        + `上次检查 ${checked} · 下次检查 ${next} · 每 `
        + `${available.interval_days || 9} 天`),
      available.last_error
        ? h('p', { class: 'field-help' }, `上次检查失败：${available.last_error}`)
        : null,
      toggle,
    );
  }

  function renderList() {
    if (!listing) return;
    const releases = listing.releases || [];
    if (!releases.length) {
      fill(listHost, empty('远端还没有 Release；可以先创建一个版本 tag。'));
      return;
    }
    fill(listHost, ...releases.map((release) => releaseCard(release, {
      busy,
      onApply: applyRelease,
    })));
  }

  async function applyRelease(release) {
    if (busy) return;
    if (release.compat && release.compat.compatible === false) {
      setMessage(
        `不可自动应用：${release.compat.message || release.compat.code}`,
        'bad',
      );
      return;
    }
    busy = true;
    setMessage('正在检查目标版本…');
    renderList();
    try {
      const checked = await endpoints.updateCheck({ target_tag: release.tag });
      if (!checked.can_apply) {
        throw new Error(reasonText(checked.reason));
      }
      const verb = checked.is_rollback
        ? '回退'
        : (checked.is_diverged ? '切换' : '更新');
      const warnings = (checked.warnings || []).join('\n');
      const needsExtraConfirm = !!(checked.is_rollback || checked.is_diverged);
      const extra = needsExtraConfirm
        ? '\n\n这是回退/切换操作，当前分支会被移动到目标提交；旧 HEAD 会先备份。'
        : '';
      if (!confirm(
        `确认${verb}到“${checked.target_name || release.name || release.tag}”？\n\n`
        + '更新会重启 Debata，用户数据目录不会被覆盖。'
        + extra
        + (warnings ? `\n\n${warnings}` : ''),
      )) {
        setMessage('');
        return;
      }
      const payload = await endpoints.updateApply({
        confirmation_token: checked.confirmation_token,
        target_sha: checked.target_commit,
        confirm_branch_switch: true,
        confirm_rollback: needsExtraConfirm,
      });
      applied = true;
      setMessage(payload.message || '更新完成；服务将重启。', 'ok');
      if (onApplied) onApplied(payload);
    } catch (e) {
      setMessage(`操作失败：${e.message}`, 'bad');
    } finally {
      busy = false;
      if (!applied) renderList();
    }
  }

  async function load(force = false) {
    setMessage(force ? '正在刷新远端版本…' : '正在读取版本列表…');
    fill(errorHost);
    try {
      const [snapshot, data] = await Promise.all([
        endpoints.updateAvailable(),
        endpoints.updateReleases(force),
      ]);
      available = snapshot;
      listing = data;
      setMessage('');
      renderSummary();
      renderList();
    } catch (e) {
      available = null;
      listing = null;
      fill(summaryHost);
      fill(listHost);
      fill(errorHost,
        h('div', { class: 'empty' }, `读取版本列表失败：${e.message}`),
        h('div', { class: 'row' },
          h('div', { class: 'spacer' }),
          h('button', { class: 'btn btn-sm', onclick: () => load(true) }, '重试')));
      setMessage('');
    }
  }

  fill(host, summaryHost, errorHost, listHost, message);
  await load(false);
  return {
    refresh: () => load(true),
    get applied() { return applied; },
  };
}

/**
 * “之后是否不再提示更新？”——勾选并点“是”才写入 suppress_badge。
 * 返回 Promise，在对话框关闭后 resolve，便于调用方刷新总览绿标。
 */
export function promptSuppress(ctx) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    const checkbox = h('input', { type: 'checkbox' });
    const dialog = modal({
      title: '之后是否不再提示更新？',
      subtitle: '你刚刚关闭了更新窗口。',
      body: h('div', {},
        h('p', { class: 'field-help' },
          '勾选“之后不再提示”并点“是”后，总览页不再出现更新标志。'
          + '之后可以在 设置 → 更新 中重新开启。'),
        h('label', { class: 'switch' }, checkbox, h('span', {}, '之后不再提示')),
      ),
      footer: [
        h('button', {
          class: 'btn',
          onclick: () => dialog.close(),
        }, '否'),
        h('button', {
          class: 'btn btn-primary',
          onclick: async () => {
            if (checkbox.checked) {
              try {
                await endpoints.updatePreferences({ suppress_badge: true });
                ctx.toast('之后不再提示更新', 'ok');
              } catch (e) {
                ctx.toast(e.message, 'error');
              }
            }
            dialog.close();
          },
        }, '是'),
      ],
      onClose: finish,
    });
    document.body.append(dialog.el);
  });
}

/** 总览绿标点击后的版本弹窗。 */
export function openUpdateDialog(ctx, { onClosed = null } = {}) {
  let applied = false;
  const bodyHost = h('div', {});
  const dialog = modal({
    title: '版本与更新',
    subtitle: '选择要更新或回退到的版本；应用会重启 Debata。',
    wide: true,
    body: bodyHost,
    footer: [h('button', {
      class: 'btn',
      onclick: () => dialog.close(),
    }, '关闭')],
    onClose: () => {
      if (applied) {
        if (onClosed) onClosed();
        return;
      }
      // 用户从总览绿标进入后退出：询问之后是否不再提示。
      promptSuppress(ctx).finally(() => {
        if (onClosed) onClosed();
      });
    },
  });
  document.body.append(dialog.el);
  renderReleaseList(bodyHost, ctx, {
    onApplied: () => { applied = true; },
  }).catch((e) => {
    fill(bodyHost, h('div', { class: 'empty' }, `版本列表加载失败：${e.message}`));
  });
  return dialog;
}

export { formatDateTime };
