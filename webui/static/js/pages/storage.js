// 存储与缓存 —— 盘点、预览、执行与任务结果。
//
// 后端只允许清理可重建缓存和受管临时文件；原始聊天、唯一图片、
// workspace 用户文件、长期记忆与备份不会出现在可勾选列表里。

import { h, fill, card, empty, table, modal, formatNumber } from '../dom.js';
import { endpoints } from '../api.js';

function formatBytes(value) {
  const num = Math.max(0, Number(value) || 0);
  if (num >= 1024 ** 4) return `${(num / 1024 ** 4).toFixed(2)} TiB`;
  if (num >= 1024 ** 3) return `${(num / 1024 ** 3).toFixed(2)} GiB`;
  if (num >= 1024 ** 2) return `${(num / 1024 ** 2).toFixed(1)} MiB`;
  if (num >= 1024) return `${(num / 1024).toFixed(1)} KiB`;
  return `${num} B`;
}

function stateLabel(state) {
  return {
    queued: '排队中',
    running: '执行中',
    cancelling: '取消中',
    completed: '已完成',
    partial_success: '部分成功',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '已中断',
  }[state] || state || '未知';
}

function stateBadge(state) {
  const tone = state === 'completed' ? 'ok'
    : state === 'partial_success' ? 'warn'
      : ['failed', 'interrupted'].includes(state) ? 'bad' : '';
  return h('span', { class: `badge ${tone}`.trim() }, stateLabel(state));
}

function isoLocal(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function idempotencyKey() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `cleanup-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function render(container, ctx) {
  let usage = null;
  let contextStatus = null;
  let jobs = [];
  let selected = new Set();
  let polling = null;
  let destroyed = false;

  const head = h('div', { class: 'page-head' },
    h('h1', { class: 'page-title' }, '存储与缓存'),
    h('p', { class: 'page-desc' },
      '只清理可重建缓存、受管临时文件和超过保留期的入站图片/视频；'
      + '原始聊天、用户文件与备份受保护。'),
  );
  const summaryHost = h('div', { class: 'grid grid-4' });
  const warningHost = h('div', {});
  const usageHost = h('div', {});
  const cleanupHost = h('div', {});
  const contextHost = h('div', {});
  const jobsHost = h('div', {});
  const page = h('div', { class: 'page' },
    head, warningHost, summaryHost, usageHost, cleanupHost, contextHost, jobsHost,
  );
  fill(container, page);

  const refreshButton = h('button', {
    class: 'btn',
    onclick: () => refresh(),
  }, '刷新');

  async function refresh() {
    refreshButton.disabled = true;
    try {
      const [usagePayload, contextPayload, jobsPayload] = await Promise.all([
        endpoints.storageUsage(),
        endpoints.contextStatus().catch(() => null),
        endpoints.storageJobs(10).catch(() => ({ jobs: [] })),
      ]);
      usage = usagePayload;
      contextStatus = contextPayload;
      jobs = jobsPayload.jobs || [];
      const available = new Set(
        (usage.categories || [])
          .filter((category) => category.reclaimable)
          .map((category) => category.key),
      );
      selected = new Set([...selected].filter((key) => available.has(key)));
      if (!selected.size) {
        for (const category of usage.categories || []) {
          if (category.reclaimable && category.bytes > 0) selected.add(category.key);
        }
      }
      renderAll();
    } catch (e) {
      fill(container, card('存储与缓存', '', empty(`读取存储信息失败：${e.message}`)));
    } finally {
      refreshButton.disabled = false;
    }
  }

  function renderAll() {
    renderSummary();
    renderUsage();
    renderCleanup();
    renderContext();
    renderJobs();
  }

  function renderSummary() {
    if (!usage) return;
    const disk = usage.disk || {};
    const maintenance = usage.maintenance || {};
    const history = usage.history || {};
    fill(summaryHost,
      h('div', { class: 'card' },
        h('div', { class: 'stat-label' }, '磁盘可用'),
        h('div', { class: 'stat-value' }, formatBytes(disk.free)),
        h('div', { class: 'field-help' },
          `已用 ${disk.percent ?? 0}% · 共 ${formatBytes(disk.total)}`),
      ),
      h('div', { class: 'card' },
        h('div', { class: 'stat-label' }, '可回收缓存'),
        h('div', { class: 'stat-value' }, formatBytes(usage.reclaimable_bytes)),
        h('div', { class: 'field-help' }, '内存缓存 + 过期临时文件 + 到期入站媒体'),
      ),
      h('div', { class: 'card' },
        h('div', { class: 'stat-label' }, '受保护数据'),
        h('div', { class: 'stat-value' }, formatBytes(usage.protected_bytes)),
        h('div', { class: 'field-help' },
          '聊天/用户文件/其他附件/日志/备份，不在安全清理范围'),
      ),
      h('div', { class: 'card' },
        h('div', { class: 'stat-label' }, '历史分页缓存'),
        h('div', { class: 'stat-value' },
          `${formatNumber(history.page_cache_entries || 0)} 页`),
        h('div', { class: 'field-help' },
          `${formatBytes(history.page_cache_bytes)} / ${formatBytes(history.page_cache_max_bytes)} · `
          + `记录 ${formatNumber(history.record_count || 0)} 条`),
      ),
    );
    const low = usage.low_disk || {};
    fill(warningHost,
      low.warning
        ? card('磁盘空间预警', '',
          h('p', {}, `可用空间 ${formatBytes(low.free_bytes)}`
            + `（${low.free_percent}%），低于配置水位。`
            + '请先清理可重建缓存或扩容；系统不会为此删除聊天与唯一附件。'))
        : null,
      h('div', { class: 'row' },
        h('span', { class: 'field-help' },
          `维护模式：${maintenance.mode || 'inventory_only'} · `
          + `临时文件保留 ${maintenance.temp_hours || 0} 小时 · `
          + `自动清理：${maintenance.automatic_cleanup ? '开启' : '未启用'}`),
        h('div', { class: 'spacer' }),
        refreshButton,
      ),
    );
  }

  function renderUsage() {
    if (!usage) return;
    const rows = (usage.categories || []).map((category) => h('tr', {},
      h('td', {}, category.label),
      h('td', {},
        category.protected
          ? h('span', { class: 'badge' }, '受保护')
          : h('span', { class: 'badge ok' }, '可清理')),
      h('td', {}, category.kind === 'memory' ? '内存' : '磁盘'),
      h('td', {}, formatBytes(category.bytes)),
      h('td', {}, formatNumber(category.items || 0)),
      h('td', { class: 'field-help' }, category.description || ''),
    ));
    fill(usageHost, card('占用分类', `盘点时间：${isoLocal(usage.generated_at)}`, table(
      ['类别', '状态', '介质', '占用', '数量', '说明'], rows,
    )));
  }

  function renderCleanup() {
    if (!usage) return;
    const categories = (usage.categories || []).filter((category) => category.reclaimable);
    const rows = categories.map((category) => {
      const checked = selected.has(category.key);
      const input = h('input', { type: 'checkbox', checked });
      input.addEventListener('change', () => {
        if (input.checked) selected.add(category.key);
        else selected.delete(category.key);
      });
      return h('tr', {},
        h('td', {}, h('label', {}, input)),
        h('td', {}, category.label),
        h('td', {}, formatBytes(category.bytes)),
        h('td', {}, formatNumber(category.items || 0)),
        h('td', { class: 'field-help' }, category.description || ''),
      );
    });
    const previewButton = h('button', {
      class: 'btn btn-primary',
      onclick: async () => {
        const chosen = [...selected];
        if (!chosen.length) {
          ctx.toast('至少选择一个清理类别', 'error');
          return;
        }
        previewButton.disabled = true;
        try {
          const preview = await endpoints.storagePreview({ categories: chosen });
          showPreview(preview);
        } catch (e) {
          ctx.toast(e.message, 'error');
        } finally {
          previewButton.disabled = false;
        }
      },
    }, '清除');
    fill(cleanupHost, card('安全清理',
      '点击“清除”后会先显示影响并确认；内存缓存与过期临时文件可重建，删除项本轮不提供恢复。',
      table(['选择', '类别', '预计释放', '数量', '说明'], rows),
      h('div', { class: 'row' },
        h('div', { class: 'spacer' }),
        previewButton,
      ),
    ));
  }

  function renderContext() {
    if (!contextStatus) {
      fill(contextHost, card('上下文预算', '',
        empty('上下文状态暂不可用')));
      return;
    }
    const budget = contextStatus.budget || {};
    const history = contextStatus.history || {};
    const errors = budget.errors || [];
    fill(contextHost, card('上下文预算',
      '只读快照：窗口、输出预留、安全余量与净输入预算。策略版本 2 目前只展示。',
      h('div', { class: 'grid grid-4' },
        h('div', {}, h('div', { class: 'stat-label' }, '有效窗口'),
          h('div', { class: 'stat-value' }, formatNumber(budget.window_effective || 0))),
        h('div', {}, h('div', { class: 'stat-label' }, '输出预留'),
          h('div', { class: 'stat-value' }, formatNumber(budget.output_reserve || 0))),
        h('div', {}, h('div', { class: 'stat-label' }, '安全余量'),
          h('div', { class: 'stat-value' }, formatNumber(budget.safety_margin || 0))),
        h('div', {}, h('div', { class: 'stat-label' }, '净输入预算'),
          h('div', { class: 'stat-value' }, formatNumber(budget.input_budget || 0))),
      ),
      table(['项目', '值'], [
        h('tr', {}, h('td', {}, 'Provider / 模型'),
          h('td', {}, `${contextStatus.provider || '?'} / ${contextStatus.model || '?'}`)),
        h('tr', {}, h('td', {}, '工作窗口 / 模型限制'),
          h('td', {},
            `${formatNumber(budget.window_requested || 0)} / `
            + `${budget.window_model ? formatNumber(budget.window_model) : '未核实'}`)),
        h('tr', {}, h('td', {}, '阈值基准'),
          h('td', {}, `${budget.threshold_basis || ''}（策略 ${budget.policy_version}）`)),
        h('tr', {}, h('td', {}, 'soft / target / retry / urgent'),
          h('td', {},
            `${formatNumber(budget.soft_trigger || 0)} / `
            + `${formatNumber(budget.target_after || 0)} / `
            + `${formatNumber(budget.retry_target || 0)} / `
            + `${formatNumber(budget.urgent_trigger || 0)}`)),
        h('tr', {}, h('td', {}, '固定开销估算'),
          h('td', {}, formatNumber(contextStatus.fixed_overhead_tokens || 0))),
        h('tr', {}, h('td', {}, '历史条数 / 活跃起点'),
          h('td', {},
            `${formatNumber(history.record_count || 0)} / `
            + `${formatNumber(history.active_start_index || 0)}`)),
      ]),
      errors.length
        ? h('p', { class: 'field-help' }, `预算校验：${errors.join(', ')}`)
        : h('p', { class: 'field-help' }, '预算校验通过；估算值不等于供应商账单。'),
    ));
  }

  function renderJobs() {
    if (!jobs.length) {
      fill(jobsHost, card('清理任务', '', empty('暂无清理任务')));
      return;
    }
    const rows = jobs.map((job) => {
      const actions = [];
      if (['queued', 'running', 'cancelling'].includes(job.state)) {
        actions.push(h('button', {
          class: 'btn',
          onclick: async () => {
            try {
              await endpoints.storageCancel(job.job_id);
              ctx.toast('已请求取消', 'ok');
              await refresh();
            } catch (e) {
              ctx.toast(e.message, 'error');
            }
          },
        }, '取消'));
      }
      const result = job.result || {};
      return h('tr', {},
        h('td', {}, isoLocal(job.created_at)),
        h('td', {}, stateBadge(job.state)),
        h('td', {}, (job.categories || []).join(', ')),
        h('td', {}, formatBytes(result.bytes_freed || 0)),
        h('td', {}, formatNumber(result.entries_freed || 0)),
        h('td', {}, formatNumber(result.skipped || 0)),
        h('td', {}, ...actions),
      );
    });
    fill(jobsHost, card('清理任务', '任务持久化在 maintenance.sqlite3；刷新或重开页面后可查询。',
      table(['创建时间', '状态', '类别', '已释放', '缓存条目', '跳过', '操作'], rows),
    ));
  }

  function showPreview(preview) {
    const counts = preview.category_counts || {};
    const rows = (preview.categories || []).map((key) => {
      const info = counts[key] || {};
      return h('tr', {},
        h('td', {}, key),
        h('td', {}, formatBytes(info.bytes || 0)),
        h('td', {}, formatNumber(info.items || 0)),
      );
    });
    let busy = false;
    const confirm = h('button', {
      class: 'btn btn-primary',
      onclick: async () => {
        if (busy) return;
        busy = true;
        confirm.disabled = true;
        try {
          const job = await endpoints.storageExecute({
            preview_id: preview.preview_id,
            confirmation_token: preview.confirmation_token,
            idempotency_key: idempotencyKey(),
            expected_generation: preview.generation,
          });
          ctx.toast(`清理任务已创建：${stateLabel(job.state)}`, 'ok');
          view.close();
          await refresh();
          pollJob(job.job_id);
        } catch (e) {
          ctx.toast(e.message, 'error');
          confirm.disabled = false;
          busy = false;
        }
      },
    }, '确认执行');
    const body = h('div', {},
      h('p', {}, `预计释放 ${formatBytes(preview.expected_bytes)}，`
        + `候选 ${formatNumber(preview.expected_items)} 项。`),
      table(['类别', '预计释放', '数量'], rows),
      ...(preview.warnings || []).map((text) => h('p', { class: 'field-help' }, text)),
      h('p', { class: 'field-help' },
        `预览过期时间：${isoLocal(preview.expires_at)}；过期后必须重新预览。`),
    );
    const view = modal({
      title: '确认清理影响',
      subtitle: '原始聊天、用户文件与备份不会出现在候选里；'
        + '入站图片/视频删除后原媒体字节不可用。',
      body,
      footer: h('div', { class: 'row' },
        h('div', { class: 'spacer' }),
        h('button', { class: 'btn', onclick: () => view.close() }, '取消'),
        confirm,
      ),
    });
    document.body.append(view.el);
  }

  function pollJob(jobId) {
    if (polling) clearTimeout(polling);
    const tick = async () => {
      if (destroyed) return;
      try {
        const job = await endpoints.storageJob(jobId);
        if (!job) return;
        const terminal = ['completed', 'partial_success', 'failed', 'cancelled', 'interrupted'];
        if (terminal.includes(job.state)) {
          const result = job.result || {};
          ctx.toast(
            `${stateLabel(job.state)}：释放 ${formatBytes(result.bytes_freed || 0)}，`
            + `跳过 ${result.skipped || 0} 项`,
            job.state === 'completed' ? 'ok' : 'error',
          );
          await refresh();
          return;
        }
        polling = setTimeout(tick, 700);
      } catch (e) {
        polling = setTimeout(tick, 1500);
      }
    };
    polling = setTimeout(tick, 500);
  }

  await refresh();
  return {
    destroy() {
      destroyed = true;
      if (polling) clearTimeout(polling);
    },
  };
}
