// 设置页 —— 对应 ui/dashboard/settings_page.py 的 8 个分页。
//
// 后端 /api/settings 返回一张声明式字段表（webui/settings_spec.py），
// 这里据此自动渲染表单，不再手写每个控件。模型（provider / agent）有独立接口。

import { h, fill, card, empty, field, table, formatNumber } from '../dom.js';
import { endpoints } from '../api.js';
import { renderModelsPanel } from '../providers.js';
import { renderReleaseList, formatDateTime } from '../releases.js';

export async function render(container, ctx) {
  let payload;
  try {
    payload = await endpoints.settings();
  } catch (e) {
    fill(container, card('设置', '', empty(`读取配置失败：${e.message}`)));
    return {};
  }

  const groups = payload.groups || [];
  const pathsInfo = payload.paths || {};
  let activeKey = 'models';
  let restartHint = null;

  const nav = h('div', { class: 'settings-nav' });
  const body = h('div', {});
  // 重启提示放在独立容器里：body 每次切分页都会被 fill() 清空，
  // 提示挂在 body 里会被一起冲掉。
  const hintHost = h('div', {});

  const tabs = [
    { key: 'models', title: '模型' },
    ...groups.map((g) => ({ key: g.key, title: g.title })),
    { key: 'paths', title: '路径与诊断' },
  ];

  function markRestart() {
    if (restartHint) return;
    restartHint = card(null, '',
      h('div', { class: 'row' },
        h('span', {}, '有配置需要重启 Runtime 才能生效。'),
        h('div', { class: 'spacer' }),
        h('button', {
          class: 'btn btn-primary',
          onclick: async (event) => {
            event.currentTarget.disabled = true;
            try {
              await endpoints.restart();
              ctx.toast('Runtime 已重启', 'ok');
              clearRestart();
            } catch (e) {
              ctx.toast(e.message, 'error');
              event.currentTarget.disabled = false;
            }
          },
        }, '立即重启'),
      ),
    );
    fill(hintHost, restartHint);
  }

  function clearRestart() {
    restartHint = null;
    fill(hintHost);
  }

  // ============================================================
  // 通用字段渲染
  // ============================================================

  function control(fieldSpec, onSave) {
    const value = fieldSpec.value;
    const kind = fieldSpec.type;

    if (kind === 'bool') {
      const input = h('input', { type: 'checkbox', checked: !!value });
      input.addEventListener('change', () => onSave(input.checked));
      return h('label', { class: 'switch' }, input, fieldSpec.label);
    }

    if (kind === 'enum') {
      const select = h('select', {});
      for (const [val, label] of fieldSpec.options || []) {
        select.append(h('option', { value: val, selected: val === value }, label));
      }
      select.addEventListener('change', () => onSave(select.value));
      return field(fieldSpec.label, select, fieldSpec.help || '');
    }

    if (kind === 'list_int' || kind === 'list_str') {
      const input = h('input', {
        value: (value || []).join(', '),
        placeholder: kind === 'list_int' ? '逗号分隔（中英文均可），留空表示不限制' : '逗号或换行分隔',
      });
      const commit = () => onSave(input.value);
      input.addEventListener('blur', commit);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); commit(); }
      });
      return field(fieldSpec.label, input, fieldSpec.help || '');
    }

    if (kind === 'json') {
      // 结构型配置（字典 / 规则表）声明式表单展开会淹没页面，用一个 JSON 文本框整体编辑。
      // 后端会 json.loads 校验，写错会带着行号报回来。
      const area = h('textarea', {
        rows: 6,
        spellcheck: 'false',
        style: { fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-small)' },
        value: value === null || value === undefined
          ? '' : JSON.stringify(value, null, 2),
      });
      const commit = () => onSave(area.value);
      let dirty = false;
      area.addEventListener('input', () => { dirty = true; });
      area.addEventListener('blur', () => { if (dirty) { dirty = false; commit(); } });
      area.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          dirty = false;
          commit();
        }
      });
      return field(fieldSpec.label, area,
        (fieldSpec.help || '') + '（JSON；Ctrl+Enter 保存）');
    }

    if (kind === 'secret_ref') {
      const input = h('input', {
        type: 'password',
        placeholder: value ? '已保存（留空则不修改）' : '未设置',
      });
      const button = h('button', {
        class: 'btn',
        onclick: async () => {
          const text = input.value.trim();
          if (!text && !confirm('清空这个密钥？')) return;
          try {
            await endpoints.putSecret(fieldSpec.secret_id, text);
            ctx.toast(text ? '密钥已保存' : '密钥已清空', 'ok');
            input.value = '';
          } catch (e) {
            ctx.toast(e.message, 'error');
          }
        },
      }, '保存');
      return field(fieldSpec.label, h('div', { class: 'row' }, input, button),
        fieldSpec.help || '密钥只写入加密的 secrets.enc，不落 config.yaml');
    }

    const type = (kind === 'int' || kind === 'int_or_none') ? 'number'
      : (kind === 'float' ? 'number' : 'text');
    const input = h('input', {
      type,
      value: value === null || value === undefined ? '' : value,
      step: kind === 'float' ? 'any' : null,
      min: fieldSpec.min,
      max: fieldSpec.max,
      placeholder: kind === 'int_or_none' ? '留空 = 自动' : '',
    });
    const commit = () => {
      const raw = input.value.trim();
      if (kind === 'int_or_none' && raw === '') return onSave(null);
      onSave(raw);
    };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); commit(); }
    });
    return field(fieldSpec.label, input, fieldSpec.help || '');
  }

  async function saveField(fieldSpec, value) {
    try {
      const result = await endpoints.patchSetting(fieldSpec.path, value);
      fieldSpec.value = result.value;
      ctx.toast(`${fieldSpec.label} 已保存`, 'ok');
      if (result.needs_restart) markRestart();
    } catch (e) {
      ctx.toast(`${fieldSpec.label} 保存失败：${e.message}`, 'error');
    }
  }

  // ============================================================
  // 模型分页
  // ============================================================

  async function renderModels() {
    // 提供商增删改、测试连接、拉取模型列表都在 providers.js 里，
    // 首次向导也复用同一套表单，避免两处各写一份。
    await renderModelsPanel(body, ctx, { markRestart });
  }

  // ============================================================
  // 路径与诊断
  // ============================================================

  function renderPaths() {
    const secrets = payload.secrets_index || [];
    fill(body,
      card('运行路径', '', h('table', { class: 'table' },
        h('tbody', {}, ...Object.entries(pathsInfo).map(([key, value]) =>
          h('tr', {}, h('td', {}, key), h('td', { class: 'mono' }, value)))),
      )),
      card('已保存的密钥 ID', '只列 ID，不显示密钥内容。',
        secrets.length
          ? h('div', { class: 'chips' }, ...secrets.map((id) => h('span', { class: 'chip' }, id)))
          : empty('还没有保存任何密钥')),
      card('危险操作', '',
        h('div', { class: 'row row-wrap' },
          h('button', {
            class: 'btn',
            onclick: async () => {
              try { await endpoints.stopRuntime(); ctx.toast('Runtime 已停止', 'ok'); }
              catch (e) { ctx.toast(e.message, 'error'); }
            },
          }, '停止 Runtime'),
          h('button', {
            class: 'btn',
            onclick: async () => {
              try { await endpoints.startRuntime(); ctx.toast('Runtime 已启动', 'ok'); }
              catch (e) { ctx.toast(e.message, 'error'); }
            },
          }, '启动 Runtime'),
          h('button', {
            class: 'btn btn-danger',
            onclick: async () => {
              if (!confirm('退出 WebUI 进程？Runtime 会一并关闭。')) return;
              try { await endpoints.quit(); } catch { /* 进程正在退出 */ }
              document.body.replaceChildren(
                h('div', { class: 'gate' },
                  h('div', { class: 'gate-card' },
                    h('div', { class: 'gate-brand' }, '已退出'),
                    h('p', { class: 'gate-hint' }, 'WebUI 服务已关闭，可以关闭此页面。'))));
            },
          }, '退出 WebUI'),
        ),
      ),
    );
  }

  // ============================================================
  // 分页切换
  // ============================================================

  async function appendUpdatePanel(nodes) {
    let status = null;
    try {
      status = await endpoints.updateStatus();
    } catch (e) {
      nodes.push(card('更新', '', empty(`读取更新状态失败：${e.message}`)));
      return;
    }
    const notification = status.notification || {};
    const statusLine = h('div', { class: 'field-help' });
    const resultHost = h('div', {});
    let releaseList = null;
    const checkBtn = h('button', { class: 'btn btn-primary' }, '检查更新');
    checkBtn.addEventListener('click', async () => {
      checkBtn.disabled = true;
      statusLine.textContent = '正在连接 GitHub / 镜像…';
      try {
        const checked = await endpoints.updateCheck();
        resultHost.replaceChildren(renderUpdateResult(checked));
        if (checked.available) {
          statusLine.textContent = `发现新版本：${checked.available_release?.name
            || checked.available_release?.tag || checked.target_label}`;
        } else if (checked.can_apply) {
          statusLine.textContent = `可以切换到 ${checked.target_label}`;
        } else {
          statusLine.textContent = '当前没有可应用的新版本。';
        }
        if (releaseList) releaseList.refresh();
      } catch (e) {
        statusLine.textContent = `检查失败：${e.message}`;
        resultHost.replaceChildren();
      } finally {
        checkBtn.disabled = false;
      }
    });
    const short = (value) => (value ? String(value).slice(0, 10) : '—');
    const lastCheck = notification.last_check_at
      ? formatDateTime(notification.last_check_at) : '尚未检查';
    const nextCheck = notification.next_check_at
      ? formatDateTime(notification.next_check_at) : '待定';
    const rows = [
      h('tr', {}, h('td', {}, '当前版本'),
        h('td', {}, `${notification.version_label || status.version_label || '开发版'}`
          + ` · ${short(status.current_commit)}`)),
      h('tr', {}, h('td', {}, '当前分支'), h('td', {}, status.current_branch)),
      h('tr', {}, h('td', {}, '更新仓库'), h('td', { class: 'mono' }, status.repo)),
      h('tr', {}, h('td', {}, '更新目标'), h('td', { class: 'mono' }, status.target_ref)),
      h('tr', {}, h('td', {}, '自动检查'),
        h('td', {}, `每 ${status.check_interval_days || notification.interval_days || 9} 天`
          + ` · 上次 ${lastCheck} · 下次 ${nextCheck}`)),
      h('tr', {}, h('td', {}, '生效远程'),
        h('td', { class: 'mono' }, status.effective_remote || status.mirror_error || '—')),
      h('tr', {}, h('td', {}, '工作树'), h('td', {}, status.worktree_dirty
        ? `有 ${status.dirty_file_count} 个未提交文件，自动更新会被拒绝`
        : '干净')),
      h('tr', {}, h('td', {}, '数据目录'), h('td', { class: 'mono' }, status.data_dir)),
    ];
    if (notification.last_error) {
      rows.push(h('tr', {}, h('td', {}, '上次检查错误'),
        h('td', { class: 'field-help' }, notification.last_error)));
    }
    nodes.push(card('版本与更新',
      `版本来自 ${status.repo} 的 GitHub Releases/tag；只更新源码，不覆盖用户数据。`,
      table(['项目', '值'], rows),
      h('p', { class: 'field-help' },
        '应用版本会备份当前提交并重启服务；依赖文件变化时会拒绝自动应用。'
        + '镜像站会提供将执行的代码，请只使用可信 https 镜像。'),
      h('div', { class: 'row' }, h('div', { class: 'spacer' }), checkBtn),
      statusLine,
      resultHost,
    ));

    nodes.push(renderMirrorCard(status, ctx, () => {
      if (releaseList) releaseList.refresh();
    }));

    const releaseHost = h('div', {});
    const refreshBtn = h('button', { class: 'btn btn-sm' }, '刷新版本列表');
    nodes.push(card('版本列表',
      '每个版本都可以展开/收起更新日志；更新与回退都会先备份当前提交。',
      h('div', { class: 'row' },
        h('span', { class: 'field-help' }, '勾选“接收更新提醒”可控制总览页绿色标志。'),
        h('div', { class: 'spacer' }),
        refreshBtn),
      releaseHost));
    // 版本列表需要联网；不要 await，否则整个“更新”分页会被 git fetch 卡住。
    // renderReleaseList 会先同步写入“正在读取版本列表…”，页面可以立即显示。
    refreshBtn.addEventListener('click', () => {
      if (releaseList) releaseList.refresh();
    });
    renderReleaseList(releaseHost, ctx, { showNotifyToggle: true })
      .then((handle) => { releaseList = handle; })
      .catch((e) => fill(releaseHost, empty(`读取版本列表失败：${e.message}`)));
  }

  function renderMirrorCard(status, ctx, onSaved) {
    const presets = (status.mirror_presets || []).filter((item) => item && item.id);
    const current = status.mirror || '';
    const matched = presets.find((item) => (item.value || '') === current);
    const initial = matched ? matched.id : 'custom';
    const select = h('select', {});
    for (const preset of presets) {
      select.append(h('option', {
        value: preset.id,
        selected: preset.id === initial,
      }, preset.label));
    }
    select.append(h('option', { value: 'custom', selected: initial === 'custom' }, '自定义…'));
    const custom = h('input', {
      value: current,
      placeholder: 'https://your-mirror.example/CN-BI5UNF/DebataBot.git',
      disabled: initial !== 'custom',
    });
    const note = h('p', { class: 'field-help' });
    const saveStatus = h('p', { class: 'field-help' });
    function sync() {
      const preset = presets.find((item) => item.id === select.value);
      custom.disabled = select.value !== 'custom';
      if (preset) {
        custom.value = preset.value || '';
        note.textContent = preset.note || '';
      } else {
        note.textContent = '填写完整 https 仓库 URL 或 https://…/ 前缀；只接受同一 DebataBot 仓库，'
          + '不接受账号密码、file:// 或内网地址。';
      }
    }
    select.addEventListener('change', sync);
    sync();
    const saveBtn = h('button', { class: 'btn btn-primary' }, '保存镜像');
    saveBtn.addEventListener('click', async () => {
      const preset = presets.find((item) => item.id === select.value);
      const value = select.value === 'custom'
        ? custom.value.trim()
        : (preset?.value || '');
      saveBtn.disabled = true;
      try {
        await endpoints.patchSetting('update.mirror', value);
        status.mirror = value;
        saveStatus.textContent = value
          ? `已保存：${value}`
          : '已保存：使用 GitHub 官方直连';
        ctx.toast('镜像设置已保存', 'ok');
        if (onSaved) onSaved();
      } catch (e) {
        saveStatus.textContent = `保存失败：${e.message}`;
      } finally {
        saveBtn.disabled = false;
      }
    });
    return card('更新镜像',
      '预设几个常用源，也可以在下方填写自定义镜像。第三方源会看到代码请求，只使用你信任的源。',
      field('预设镜像', select, ''),
      field('自定义镜像站', custom, ''),
      note,
      h('div', { class: 'row' }, h('div', { class: 'spacer' }), saveBtn),
      saveStatus,
    );
  }

  function renderUpdateResult(checked) {
    const commits = (checked.commits || []).map((line) => h('li', {}, line));
    const warnings = (checked.warnings || []).map((line) =>
      h('p', { class: 'field-help' }, line));
    const deps = checked.dependency_changes?.length
      ? h('p', { class: 'field-help' },
        `依赖文件变化（需手动安装，禁止自动应用）：${checked.dependency_changes.join(', ')}`)
      : null;
    return h('div', {},
      h('p', {}, `目标：${checked.target_name || checked.target_label || '—'}`
        + `（${checked.target_commit_short || '—'}）`),
      commits.length ? h('div', {}, h('div', {}, '目标提交：'), h('ul', {}, ...commits)) : null,
      deps,
      ...warnings,
    );
  }

  async function show(key) {
    activeKey = key;
    for (const btn of nav.querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.key === key);
    }
    if (key === 'models') return renderModels();
    if (key === 'paths') return renderPaths();

    const group = groups.find((g) => g.key === key);
    if (!group) return fill(body, empty('未知分页'));

    const nodes = [h('p', { class: 'card-sub' }, group.desc || '')];
    const visibleFields = group.fields.filter((spec) => !spec.hidden);
    if (!visibleFields.length) {
      if (group.fields.length) {
        nodes.push(empty('这个分页的选项在下方专用面板中设置'));
      } else {
        nodes.push(empty('这个分页没有可视化字段，请直接编辑 config.yaml'));
      }
    } else {
      const section = h('div', { class: 'card' });
      for (const spec of visibleFields) {
        section.append(control(spec, (value) => saveField(spec, value)));
      }
      nodes.push(section);
    }
    if (key === 'update') {
      await appendUpdatePanel(nodes);
    }
    if (key === 'token_budget') {
      try {
        const status = await endpoints.contextStatus();
        const budget = status.budget || {};
        const compaction = status.compaction || {};
        const manualStatus = h('p', { class: 'field-help' });
        const compactBtn = h('button', { class: 'btn' }, '立即压缩活跃历史');
        compactBtn.addEventListener('click', async () => {
          if (!confirm('确认立即压缩当前活跃历史？原文会归档，活跃窗口起点前移。')) return;
          compactBtn.disabled = true;
          manualStatus.textContent = '正在调用摘要模型，请稍候…';
          try {
            const payload = await endpoints.contextCompact();
            const result = payload.result || {};
            manualStatus.textContent = payload.ok
              ? `压缩完成：归档 ${result.archived_count || 0} 条，活跃 ${result.active_records || 0} 条。`
              : `未压缩：${result.reason || result.status || '无可用结果'}`;
          } catch (e) {
            manualStatus.textContent = `压缩失败：${e.message}`;
          } finally {
            compactBtn.disabled = false;
          }
        });
        nodes.push(card('当前预算快照',
          `有效窗口 = min(工作窗口, 模型限制)；净输入预算 = 窗口 - 输出预留 - 安全余量。`
          + '该预算已用于发送前硬 guard；估算值不是供应商账单。',
          table(['项目', '值'], [
            h('tr', {}, h('td', {}, 'Provider / 模型'),
              h('td', {}, `${status.provider || '?'} / ${status.model || '?'}`)),
            h('tr', {}, h('td', {}, '工作窗口 / 模型限制'),
              h('td', {},
                `${formatNumber(budget.window_requested || 0)} / `
                + `${budget.window_model ? formatNumber(budget.window_model) : '未核实'}`)),
            h('tr', {}, h('td', {}, '输出预留 / 安全余量'),
              h('td', {},
                `${formatNumber(budget.output_reserve || 0)} / `
                + `${formatNumber(budget.safety_margin || 0)}`)),
            h('tr', {}, h('td', {}, '净输入预算'),
              h('td', {}, formatNumber(budget.input_budget || 0))),
            h('tr', {}, h('td', {}, 'soft / target / retry / urgent'),
              h('td', {},
                `${formatNumber(budget.soft_trigger || 0)} / `
                + `${formatNumber(budget.target_after || 0)} / `
                + `${formatNumber(budget.retry_target || 0)} / `
                + `${formatNumber(budget.urgent_trigger || 0)}`)),
            h('tr', {}, h('td', {}, '固定开销 / 活跃历史预算'),
              h('td', {},
                `${formatNumber(status.fixed_overhead_tokens || 0)} / `
                + `${formatNumber(status.working_history_budget || 0)}`)),
            h('tr', {}, h('td', {}, '压缩状态'),
              h('td', {},
                `${compaction.last_action || 'idle'}`
                + (compaction.last_skip_reason
                  ? `（跳过：${compaction.last_skip_reason}）` : '')
                + (compaction.backoff_seconds_remaining
                  ? ` · 退避剩余 ${compaction.backoff_seconds_remaining}s` : ''))),
            h('tr', {}, h('td', {}, '增长预测 / 冷却 / 最少新增'),
              h('td', {},
                `${compaction.growth_ewma ?? '—'} / `
                + `${formatNumber(compaction.cooldown_seconds || 0)}s / `
                + `${formatNumber(compaction.min_new_tokens || 0)}`)),
          ]),
          (budget.errors || []).length
            ? h('p', { class: 'field-help' },
              `预算校验失败：${(budget.errors || []).join(', ')}`)
            : null,
          h('div', { class: 'row' },
            h('span', { class: 'field-help' },
              '手动压缩会等待摘要完成；后台压缩进行中时返回“忙碌”，不会重复调用模型。'),
            h('div', { class: 'spacer' }),
            compactBtn),
          manualStatus,
        ));
      } catch (e) {
        nodes.push(card('当前预算快照', '', empty(`读取失败：${e.message}`)));
      }
    }
    fill(body, ...nodes);
  }

  fill(nav, ...tabs.map((t) =>
    h('button', { dataset: { key: t.key }, onclick: () => show(t.key) }, t.title)));

  fill(container,
    h('div', { class: 'page-head' },
      h('div', {},
        h('h1', { class: 'page-title' }, '设置'),
        h('p', { class: 'page-desc' }, '改动即时写入 config.yaml；需要重启的会给出提示。'),
      ),
    ),
    hintHost,
    h('div', { class: 'settings-layout' }, nav, body),
  );

  await show('models');
  return {};
}
