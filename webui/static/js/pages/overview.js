// 总览页 —— 对应 ui/dashboard/overview_page.py 的 4 张统计卡：
// 主模型状态 / 模型健康 / 渠道状态 / 用量统计。

import { h, fill, card, statCard, formatNumber, formatDuration, statusClass } from '../dom.js';
import { endpoints } from '../api.js';
import { icon } from '../icons.js';
import { openUpdateDialog } from '../releases.js';

const REFRESH_MS = 2000;
const UPDATE_REFRESH_MS = 60000;
const RANGES = [['today', '今天'], ['7d', '7 天'], ['30d', '30 天'], ['all', '全部']];

const ACTIVITY_TEXT = {
  idle: '空闲',
  thinking: '思考中',
  tool: '调用工具中',
  error: '出错了',
};

const OPERATION_TEXT = {
  chat: '聊天',
  knowledge_research: '联网考究',
  slang_extract: '黑话学习',
  web_search_official: '官方搜索',
  describe_image: '看图',
  embedding: 'Embedding',
  summary: '摘要',
  persona: '人格后台',
  proactive: '主动思考',
  social: '社交',
  provider_health: '健康检查',
};

function operationLabel(value) {
  const key = String(value || '').trim();
  return OPERATION_TEXT[key] || key || 'unknown';
}

export async function render(container, ctx) {
  let range = 'today';
  let timer = null;
  let disposed = false;

  const activityCard = h('div', { class: 'card' });
  const providerCard = h('div', { class: 'card' });
  const adapterCard = h('div', { class: 'card' });
  const usageCard = h('div', { class: 'card' });
  const updateHost = h('div', { class: 'update-orb-host' });

  const rangeBar = h('div', { class: 'row' },
    ...RANGES.map(([value, label]) =>
      h('button', {
        class: `btn btn-sm ${value === range ? 'btn-primary' : ''}`,
        dataset: { range: value },
        onclick: (event) => {
          range = value;
          for (const btn of rangeBar.querySelectorAll('button')) {
            btn.classList.toggle('btn-primary', btn.dataset.range === range);
          }
          refresh();
        },
      }, label),
    ),
  );

  fill(container,
    h('div', { class: 'page-head' },
      h('div', {},
        h('h1', { class: 'page-title' }, '总览'),
        h('p', { class: 'page-desc' }, '运行状态、模型健康与用量。数据每 2 秒自动刷新。'),
      ),
      h('div', { class: 'row' },
        updateHost,
        h('button', {
          class: 'btn',
          title: '只重建运行时，不加载新代码；更新后请到 设置 → 更新 重启后端',
          onclick: async (event) => {
            event.currentTarget.disabled = true;
            try {
              await endpoints.restart();
              ctx.toast('Runtime 已重启', 'ok');
            } catch (e) {
              ctx.toast(e.message, 'error');
            } finally {
              event.currentTarget.disabled = false;
              refresh();
            }
          },
        }, '重启 Runtime'),
      ),
    ),

    h('div', { class: 'grid grid-2' }, activityCard, adapterCard),
    providerCard,
    usageCard,
  );

  function renderActivity(activity) {
    const state = activity.state || 'idle';
    const cls = state === 'error' ? 'bad' : (state === 'idle' ? '' : 'ok');
    fill(activityCard,
      h('div', { class: 'stat-label' }, '主模型状态'),
      h('div', { class: 'stat-value small' },
        h('span', { class: `badge ${cls}` }, ACTIVITY_TEXT[state] || state)),
      h('div', { class: 'field-help' },
        activity.agent ? `${activity.agent} · ${activity.model || '未指定模型'}` : '—'),
      activity.text ? h('div', { class: 'field-help' }, activity.text) : null,
    );
  }

  function renderAdapter(status) {
    const adapter = status.adapter;
    const connected = !!status.adapter_connected;
    fill(adapterCard,
      h('div', { class: 'stat-label' }, '渠道状态'),
      h('div', { class: 'stat-value small' },
        h('span', { class: `badge ${connected ? 'ok' : (status.state === 'running' ? 'bad' : 'warn')}` },
          connected ? '已就位' : (status.state === 'running' ? '未连接' : '未运行'))),
      h('div', { class: 'field-help' },
        adapter
          ? `${adapter.name} · ${adapter.mode} · ${adapter.host}:${adapter.port}${adapter.path}`
          : '未配置渠道'),
      h('div', { class: 'field-help' }, `已运行 ${formatDuration(status.uptime_seconds)}`),
    );
  }

  function renderProviders(providers) {
    if (!providers.length) {
      fill(providerCard,
        h('h2', { class: 'card-title' }, '模型健康'),
        h('div', { class: 'empty' }, '还没有配置任何模型提供商'));
      return;
    }
    fill(providerCard,
      h('h2', { class: 'card-title' }, '模型健康'),
      h('p', { class: 'card-sub' }, '启动时探测的连通性与延迟。'),
      h('table', { class: 'table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Provider'), h('th', {}, '状态'), h('th', {}, '延迟'), h('th', {}, '说明'))),
        h('tbody', {}, ...providers.map((p) =>
          h('tr', {},
            h('td', { class: 'mono' }, p.id),
            h('td', {}, h('span', { class: `badge ${statusClass(p.status)}` }, p.text)),
            h('td', {}, p.latency_ms != null ? `${Math.round(p.latency_ms)} ms` : '—'),
            h('td', { class: 'field-help' }, p.message || '—'),
          ),
        )),
      ),
    );
  }

  function renderUsage(usage) {
    if (!usage.available) {
      fill(usageCard,
        h('h2', { class: 'card-title' }, '用量统计'),
        h('div', { class: 'empty' }, usage.error || '暂无用量数据'),
        rangeBar);
      return;
    }
    const currentCount = Number(usage.current_semantics_request_count || 0);
    const currentAvailable = currentCount > 0
      && usage.current_semantics_available !== false;
    const ratio = usage.current_cache_hit_rate ?? usage.cache_hit_rate;
    const ratioNumber = ratio === null || ratio === undefined ? NaN : Number(ratio);
    const hitText = currentAvailable && Number.isFinite(ratioNumber)
      ? `${(ratioNumber * 100).toFixed(1)}%`
      : '—';
    const currentValue = (value) => (
      currentAvailable
      && value !== null
      && value !== undefined
      && Number.isFinite(Number(value))
        ? formatNumber(value)
        : '—'
    );
    const estimateSamples = Number(usage.current_estimate_samples || 0);
    const estimateError = usage.current_estimate_error_rate;
    const estimateText = currentAvailable
      && estimateSamples > 0
      && estimateError !== null
      && Number.isFinite(Number(estimateError))
      ? `${(Number(estimateError) * 100).toFixed(1)}%`
      : '—';
    const legacySamples = Number(usage.legacy_request_count || 0);
    const unknownSamples = Number(usage.unknown_semantics_request_count || 0);
    const inconsistentSamples = Number(usage.raw_cache_inconsistent_count || 0);
    const operations = (usage.by_operation || [])
      .filter((row) => Number(row.current_total_tokens || 0) > 0)
      .slice(0, 10);
    fill(usageCard,
      h('h2', { class: 'card-title' }, '用量统计'),
      h('p', { class: 'card-sub' },
        '总消耗包含全部历史记录；缓存命中率只使用当前 V2 口径。'),
      rangeBar,
      h('div', { class: 'grid grid-4', style: { marginTop: '16px' } },
        statCard('请求数（全历史）', formatNumber(usage.request_count)),
        statCard('总 token（全历史）', formatNumber(usage.total_tokens)),
        statCard('V2 缓存命中率', hitText),
        statCard('输出 token（全历史）', formatNumber(usage.completion_tokens)),
      ),
      h('div', { class: 'grid grid-4', style: { marginTop: '16px' } },
        statCard('V2 输入 token', currentValue(usage.current_prompt_tokens)),
        statCard('V2 未命中输入', currentValue(usage.current_uncached_prompt_tokens)),
        statCard('V2 命中缓存', currentValue(usage.current_cached_tokens)),
        statCard('V2 写入缓存', currentValue(usage.current_cache_creation_tokens)),
      ),
      h('div', { class: 'grid grid-4', style: { marginTop: '16px' } },
        statCard('推理 token（全历史）', formatNumber(usage.reasoning_tokens)),
        statCard('V2 估算误差', estimateText),
        statCard('V2 估算样本', currentValue(estimateSamples)),
        statCard('V2 估算输入', currentValue(usage.current_estimated_prompt_tokens)),
      ),
      h('p', { class: 'field-help', style: { marginTop: '12px' } },
        currentAvailable
          ? `V2 样本 ${formatNumber(currentCount)} 条。`
          : '暂无新口径数据；旧记录不会被当成 V2 计算缓存命中率。'),
      legacySamples > 0
        ? h('p', { class: 'field-help' },
            `含 ${formatNumber(legacySamples)} 条旧口径记录（输入 ${formatNumber(usage.legacy_prompt_tokens)}，总 token ${formatNumber(usage.legacy_total_tokens)}）；`
            + '旧口径输入 token 不含 cache 读写，没有可与新口径直接比较的命中率。')
        : null,
      unknownSamples > 0
        ? h('p', { class: 'field-help' },
            `含 ${formatNumber(unknownSamples)} 条未知未来 usage 口径记录，未纳入 V2 主缓存命中率。`)
        : null,
      inconsistentSamples > 0
        ? h('p', { class: 'field-help' },
            `有 ${formatNumber(inconsistentSamples)} 条记录的缓存字段超过输入 token，展示值已做安全夹取。`)
        : null,
      operations.length
        ? h('div', { style: { marginTop: '14px' } },
            h('p', { class: 'field-help' }, '按用途（V2 口径）'),
            h('table', { class: 'table' },
              h('thead', {}, h('tr', {},
                h('th', {}, '用途'),
                h('th', {}, '请求'),
                h('th', {}, 'V2 输入 token'),
                h('th', {}, 'V2 总 token'))),
              h('tbody', {}, ...operations.map((row) =>
                h('tr', {},
                  h('td', {}, h('span', { class: 'badge' }, operationLabel(row.operation))),
                  h('td', {}, formatNumber(row.request_count)),
                  h('td', {}, formatNumber(row.current_prompt_tokens)),
                  h('td', {}, formatNumber(row.current_total_tokens)),
                ),
              )),
            ))
        : null,
    );
  }

  async function refreshUpdate() {
    if (disposed) return;
    try {
      const info = await endpoints.updateAvailable();
      if (disposed) return;
      if (info.available && info.release) {
        const label = info.release.name || info.release.tag || '新版本';
        fill(updateHost, h('button', {
          class: 'update-orb',
          type: 'button',
          title: `发现新版本：${label}`,
          'aria-label': `发现新版本：${label}`,
          onclick: () => openUpdateDialog(ctx, { onClosed: refreshUpdate }),
        }, icon('arrow-up', { size: 14 })));
      } else {
        fill(updateHost);
      }
    } catch {
      // 自动检查失败或 Runtime 未就绪时不显示绿标，但不要把总览页弄红。
      if (!disposed) fill(updateHost);
    }
  }

  async function refresh() {
    if (disposed) return;
    try {
      const data = await endpoints.overview(range);
      if (disposed) return;
      renderActivity(data.activity);
      renderAdapter(data.status);
      renderProviders(data.providers || []);
      renderUsage(data.usage || { available: false });
    } catch (e) {
      if (disposed) return;
      fill(activityCard,
        h('div', { class: 'stat-label' }, '主模型状态'),
        h('div', { class: 'stat-value small' }, h('span', { class: 'badge bad' }, '不可用')),
        h('div', { class: 'field-help' }, e.message),
      );
    }
  }

  await Promise.all([refresh(), refreshUpdate()]);
  timer = setInterval(refresh, REFRESH_MS);
  const updateTimer = setInterval(refreshUpdate, UPDATE_REFRESH_MS);

  // Runtime 状态变化时立刻刷新一次，不用等下一个轮询周期。
  const unsubscribe = ctx.onEvent((event) => {
    if (event.type === 'status') refresh();
  });

  return {
    destroy() {
      disposed = true;
      clearInterval(timer);
      clearInterval(updateTimer);
      unsubscribe();
    },
  };
}
