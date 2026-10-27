// 联网考究知识/热梗笔记本。
//
// 后台小模型把搜索结果整理成短笔记，只有 active 条目会进入动态 task_context。
// 本页负责查看、手动补充、编辑、删除，以及手动触发一次联网考究。

import { h, fill, card, empty, field, table, modal } from '../dom.js';
import { icon } from '../icons.js';
import { endpoints } from '../api.js';

const KIND_TEXT = { meme: '梗', fact: '资料', note: '笔记' };
const KIND_BADGE = { meme: 'warn', fact: 'ok', note: '' };
const STATUS_TEXT = { active: '启用', archived: '归档' };

function kindBadge(kind) {
  return h('span', { class: `badge ${KIND_BADGE[kind] || ''}` }, KIND_TEXT[kind] || kind);
}

function timeText(value) {
  if (!value) return '—';
  // last_trend_run_at 是 Unix 秒；条目时间是 ISO 字符串。数字不能直接
  // 交给 Date()，否则会被当成毫秒，显示成 1970 年。
  let date;
  if (typeof value === 'number') {
    date = new Date(value * 1000);
  } else {
    const asNumber = Number(value);
    date = Number.isFinite(asNumber) && /^\d+(\.\d+)?$/.test(String(value))
      ? new Date(asNumber * 1000)
      : new Date(value);
  }
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function splitList(value) {
  return String(value || '')
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export async function render(container, ctx) {
  let data = null;
  let busy = false;

  const statusLine = h('p', { class: 'field-help' });
  const statsHost = h('div', { class: 'row row-wrap' });
  const listHost = h('div', {});

  const queryInput = h('input', { placeholder: '搜索标题 / 内容 / 标签' });
  const kindSelect = h('select', {},
    h('option', { value: '' }, '全部类型'),
    h('option', { value: 'meme' }, '梗'),
    h('option', { value: 'fact' }, '资料'),
    h('option', { value: 'note' }, '笔记'),
  );
  const statusSelect = h('select', {},
    h('option', { value: '' }, '全部状态'),
    h('option', { value: 'active' }, '启用'),
    h('option', { value: 'archived' }, '归档'),
  );
  const refreshBtn = h('button', { class: 'btn btn-sm' }, '刷新');

  const researchQuery = h('input', {
    placeholder: '例如：本周网络热梗 / 某版本发布时间',
    style: { minWidth: '260px' },
  });
  const researchKind = h('select', {},
    h('option', { value: 'meme' }, '热梗'),
    h('option', { value: 'fact' }, '资料'),
    h('option', { value: 'note' }, '笔记'),
  );
  const researchBtn = h('button', { class: 'btn btn-primary' }, '联网考究');
  const trendsBtn = h('button', { class: 'btn' }, '获取当前热梗');
  const addBtn = h('button', { class: 'btn' }, '手动添加');

  async function load() {
    try {
      data = await endpoints.knowledgeList({
        query: queryInput.value.trim(),
        kind: kindSelect.value,
        status: statusSelect.value,
      });
      renderAll();
    } catch (e) {
      fill(listHost, empty(`读取知识库失败：${e.message}`));
    }
  }

  function renderStats() {
    const stats = data?.stats || {};
    const kinds = stats.kinds || {};
    const settings = data?.settings || {};
    const state = data?.state || {};
    fill(statsHost,
      h('span', { class: 'badge' }, `共 ${stats.total || 0} 条`),
      h('span', { class: 'badge warn' }, `梗 ${kinds.meme || 0}`),
      h('span', { class: 'badge ok' }, `资料 ${kinds.fact || 0}`),
      h('span', { class: 'badge' }, `笔记 ${kinds.note || 0}`),
      stats.shared
        ? h('span', { class: 'badge warn' }, `共享 ${stats.shared} 条`)
        : null,
      h('span', { class: 'field-help' },
        `查询返回上限 ${settings.inject_max ?? '—'} 条 / ${settings.max_prompt_chars ?? '—'} 字符；`
        + `后台考究${settings.auto_research_enabled ? `每 ${settings.research_interval_hours} 小时` : '已关闭'}。`),
      state.last_trend_run_at
        ? h('span', { class: 'field-help' }, `上次热梗考究：${timeText(state.last_trend_run_at)}`)
        : null,
      data?.writable === false
        ? h('span', { class: 'badge bad' }, '知识库只读：上次读取失败')
        : null,
    );
  }

  function openEditor(entry = null) {
    const isNew = !entry;
    const kind = h('select', {},
      h('option', { value: 'meme' }, '梗'),
      h('option', { value: 'fact' }, '资料'),
      h('option', { value: 'note' }, '笔记'),
    );
    kind.value = entry?.kind || 'note';
    const title = h('input', { value: entry?.title || '', placeholder: '简短标题' });
    const content = h('textarea', { rows: 5, value: entry?.content || '' });
    const usage = h('input', { value: entry?.usage || '', placeholder: '可选：什么语境下用' });
    const tags = h('input', { value: (entry?.tags || []).join(', '), placeholder: '可选：用逗号分隔' });
    const sources = h('input', { value: (entry?.sources || []).join(', '), placeholder: '可选：来源 URL' });
    const status = h('select', {},
      h('option', { value: 'active' }, '启用'),
      h('option', { value: 'archived' }, '归档'),
    );
    status.value = entry?.status || 'active';
    const confidence = h('input', {
      type: 'number', min: '0', max: '1', step: '0.1',
      value: String(entry?.confidence ?? 0.8),
    });
    const shared = h('input', { type: 'checkbox', checked: !!entry?.shared });
    const editStatus = h('p', { class: 'field-help' });
    const dialog = modal({
      title: isNew ? '添加知识笔记' : `编辑「${entry.title}」`,
      subtitle: isNew
        ? '手动笔记默认启用；不确定的内容不要写成事实。'
        : `来源：${entry.source || '—'} · 更新：${timeText(entry.updated_at)}`,
      body: h('div', {},
        h('div', { class: 'grid grid-2' },
          field('类型', kind),
          field('置信度 0–1', confidence, '联网整理的笔记通常 0.5–0.8'),
          field('标题', title, '最长 80 字'),
          field('状态', status),
        ),
        field('共享', h('label', { class: 'switch' }, shared,
          '允许其他人格只读引用'), '勾选后其他人格也能在知识库中查到这条；只有所属人格能编辑或删除。'),
        field('内容', content, '最长 1200 字；只写核对过的含义/事实'),
        field('使用语境', usage),
        field('标签', tags),
        field('来源 URL', sources, '只填真实来源；不会直接进入模型 prompt'),
        editStatus,
      ),
      footer: [],
      wide: true,
    });
    const saveBtn = h('button', { class: 'btn btn-primary' }, '保存');
    const deleteBtn = isNew ? null : h('button', { class: 'btn btn-danger' }, '删除');
    fill(dialog.el.querySelector('.modal-foot'),
      h('button', { class: 'btn', onclick: () => dialog.close() }, '取消'),
      deleteBtn,
      saveBtn,
    );

    saveBtn.onclick = async () => {
      if (busy) return;
      if (!title.value.trim() || !content.value.trim()) {
        editStatus.textContent = '标题和内容不能为空。';
        editStatus.style.color = 'var(--error)';
        return;
      }
      busy = true;
      saveBtn.disabled = true;
      const payload = {
        kind: kind.value,
        title: title.value.trim(),
        content: content.value.trim(),
        usage: usage.value.trim(),
        tags: splitList(tags.value),
        sources: splitList(sources.value),
        status: status.value,
        shared: !!shared.checked,
        confidence: Number(confidence.value) || 0,
      };
      try {
        if (isNew) {
          await endpoints.knowledgeCreate(payload);
          ctx.toast('知识笔记已添加', 'ok');
        } else {
          await endpoints.knowledgeUpdate(entry.id, payload);
          ctx.toast('知识笔记已更新', 'ok');
        }
        dialog.close();
        await load();
      } catch (e) {
        editStatus.textContent = e.message;
        editStatus.style.color = 'var(--error)';
        saveBtn.disabled = false;
      } finally {
        busy = false;
      }
    };

    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        if (!confirm(`删除「${entry.title}」？删除后模型将查询不到。`)) return;
        try {
          await endpoints.knowledgeDelete(entry.id);
          ctx.toast('知识笔记已删除', 'ok');
          dialog.close();
          await load();
        } catch (e) {
          editStatus.textContent = e.message;
          editStatus.style.color = 'var(--error)';
        }
      };
    }
    document.body.append(dialog.el);
  }

  function renderList() {
    const entries = data?.entries || [];
    const personaId = data?.persona_id || '';
    if (!entries.length) {
      fill(listHost, empty('知识库还是空的。可以手动添加，或者让 LLM 联网考究。', {
        subtitle: '只有启用的笔记能被模型按需查询；共享条目其他人格也能看到。',
        iconName: 'knowledge',
      }));
      return;
    }
    fill(listHost, table(
      ['类型', '标题 / 内容', '标签', '来源', '共享', '更新时间', '操作'],
      entries.map((entry) => {
        const owned = !personaId || entry.persona_id === personaId;
        let shareCell;
        if (owned) {
          const toggle = h('input', {
            type: 'checkbox',
            checked: !!entry.shared,
            title: '共享给其他人格只读引用',
          });
          toggle.addEventListener('change', async () => {
            toggle.disabled = true;
            try {
              const result = await endpoints.knowledgeUpdate(
                entry.id, { shared: toggle.checked },
              );
              const value = result.entry?.shared ?? toggle.checked;
              ctx.toast(value ? '已共享给其他人格' : '已取消共享', 'ok');
              await load();
            } catch (e) {
              toggle.checked = !toggle.checked;
              ctx.toast(e.message, 'error');
            } finally {
              toggle.disabled = false;
            }
          });
          shareCell = h('label', { class: 'switch' }, toggle, entry.shared ? '共享' : '不共享');
        } else {
          shareCell = h('span', { class: 'badge' },
            `共享 · ${entry.persona_id || '其他人格'}`);
        }
        return h('tr', {},
          h('td', {}, kindBadge(entry.kind)),
          h('td', {},
            h('strong', {}, entry.title || '—'),
            h('div', { class: 'field-help' }, entry.content || ''),
            entry.usage ? h('div', { class: 'field-help' }, `用法：${entry.usage}`) : null,
          ),
          h('td', { class: 'field-help' }, (entry.tags || []).join('、') || '—'),
          h('td', { class: 'field-help' }, entry.source || '—'),
          h('td', {}, shareCell),
          h('td', { class: 'field-help' }, timeText(entry.updated_at)),
          h('td', {}, owned
            ? h('button', {
              class: 'btn btn-sm',
              onclick: () => openEditor(entry),
            }, icon('edit', { size: 14 }), '编辑')
            : h('span', { class: 'field-help' }, '只读')),
        );
      }),
    ));
  }

  function renderAll() {
    renderStats();
    renderList();
  }

  async function research() {
    const query = researchQuery.value.trim();
    if (!query) {
      statusLine.textContent = '先填写要考究的主题或热梗。';
      return;
    }
    if (busy) return;
    busy = true;
    researchBtn.disabled = true;
    trendsBtn.disabled = true;
    statusLine.textContent = '正在联网搜索并让模型整理，可能需要十几秒…';
    try {
      const result = await endpoints.knowledgeResearch({
        query,
        kind: researchKind.value,
        max_notes: 8,
        save: true,
      });
      statusLine.textContent = `考究完成，新增 ${result.saved || 0} 条`
        + (result.duplicates ? `，跳过 ${result.duplicates} 条重复。` : '。');
      await load();
    } catch (e) {
      statusLine.textContent = `考究失败：${e.message}`;
    } finally {
      busy = false;
      researchBtn.disabled = false;
      trendsBtn.disabled = false;
    }
  }

  async function refreshTrends() {
    if (busy) return;
    if (!confirm('立即让 LLM 联网整理当前热梗？会消耗搜索与模型额度。')) return;
    busy = true;
    researchBtn.disabled = true;
    trendsBtn.disabled = true;
    statusLine.textContent = '正在后台考究热梗，可能需要几十秒…';
    try {
      const result = await endpoints.knowledgeTrends({ force: true });
      statusLine.textContent = `热梗考究完成，新增 ${result.saved || 0} 条`
        + (result.duplicates ? `，跳过 ${result.duplicates} 条重复。` : '。');
      await load();
    } catch (e) {
      statusLine.textContent = `热梗考究失败：${e.message}`;
    } finally {
      busy = false;
      researchBtn.disabled = false;
      trendsBtn.disabled = false;
    }
  }

  refreshBtn.onclick = () => load();
  researchBtn.onclick = research;
  trendsBtn.onclick = refreshTrends;
  addBtn.onclick = () => openEditor();
  queryInput.oninput = () => load();
  kindSelect.onchange = () => load();
  statusSelect.onchange = () => load();

  fill(container,
    h('div', { class: 'page-head' },
      h('div', {},
        h('h1', { class: 'page-title' }, '知识 / 热梗'),
        h('p', { class: 'page-desc' },
          'LLM 联网考究后整理的笔记。只有启用的条目能被模型用 list_knowledge_notes 查询；'
          + '可勾选共享给其他人格只读引用，不会每轮注入 task_context。'),
      ),
      h('div', { class: 'row' },
        h('button', {
          class: 'btn',
          onclick: () => ctx.navigate('settings', { section: 'features' }),
        }, '考究设置'),
        addBtn,
        refreshBtn,
      ),
    ),
    card('', '',
      h('div', { class: 'row row-wrap' },
        field('搜索', queryInput, '按标题、内容或标签过滤'),
        field('类型', kindSelect),
        field('状态', statusSelect),
      ),
      statsHost,
      statusLine,
    ),
    card('联网考究', '后台小模型先看搜索结果和已有条目，再写新增笔记；相同/相近条目会跳过，原始网页不会保存。',
      h('div', { class: 'row row-wrap' },
        field('主题 / 热梗', researchQuery, '关键词具体一点，例如“本周 B站 热梗”'),
        field('整理成', researchKind),
        h('div', { class: 'spacer' }),
        trendsBtn,
        researchBtn,
      ),
    ),
    card('笔记列表', '勾选“共享”后其他人格可只读引用；所属人格才能编辑或删除。', listHost),
  );

  await load();
  return {};
}
