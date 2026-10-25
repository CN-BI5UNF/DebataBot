// 设置页 —— 对应 ui/dashboard/settings_page.py 的 8 个分页。
//
// 后端 /api/settings 返回一张声明式字段表（webui/settings_spec.py），
// 这里据此自动渲染表单，不再手写每个控件。模型（provider / agent）有独立接口。

import { h, fill, card, empty, field, table, formatNumber } from '../dom.js';
import { endpoints } from '../api.js';
import { renderModelsPanel } from '../providers.js';
import { renderReleaseList, formatDateTime, promptBackendRestart } from '../releases.js';

// 视觉字段由专用面板渲染；这里再按路径兜底一次，避免旧进程尚未加载
// settings_spec 的 hidden 标记时，通用表单和专用面板重复出现同一批字段。
const VISION_FIELD_PATHS = new Set([
  'features.vision.main_model_images',
  'features.vision.enabled',
  'features.vision.provider',
  'features.vision.model',
  'features.vision.base_url',
  'features.vision.api_key_id',
  'features.vision.image_token_reserve',
  'features.vision.image_limits.max_images_per_request',
]);

const SEARCH_FIELD_PATHS = new Set([
  'features.web_search.enabled',
  'features.web_search.free_enabled',
  'features.web_search.provider',
  'features.web_search.searxng_base_url',
  'features.web_search.max_results',
  'features.web_search.timeout_seconds',
  'features.web_search.official.enabled',
  'features.web_search.official.provider',
  'features.web_search.official.model',
  'features.web_search.official.timeout_seconds',
]);

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
  // 视觉设置（专用面板）
  // ============================================================

  async function appendVisionPanel(nodes, group) {
    const byPath = new Map(group.fields.map((spec) => [spec.path, spec]));
    const nativeSpec = byPath.get('features.vision.main_model_images');
    const enabledSpec = byPath.get('features.vision.enabled');
    const providerSpec = byPath.get('features.vision.provider');
    const modelSpec = byPath.get('features.vision.model');
    const baseUrlSpec = byPath.get('features.vision.base_url');
    const apiKeySpec = byPath.get('features.vision.api_key_id');
    const tokenSpec = byPath.get('features.vision.image_token_reserve');
    const maxImagesSpec = byPath.get('features.vision.image_limits.max_images_per_request');
    if (!nativeSpec || !enabledSpec || !providerSpec || !modelSpec
        || !baseUrlSpec || !apiKeySpec) {
      return;
    }

    let providerPayload = { providers: [], agents: {} };
    let presets = [];
    let providerLoadError = '';
    try {
      providerPayload = await endpoints.providers();
    } catch (e) {
      providerLoadError = e.message;
    }
    try {
      const data = await endpoints.providerPresets();
      presets = Array.isArray(data.presets) ? data.presets : [];
    } catch {
      // 预设只用于模型候选；读取失败时仍允许手填模型 ID。
    }
    const providerList = Array.isArray(providerPayload.providers)
      ? providerPayload.providers : [];

    function presetOf(provider) {
      const presetId = String(provider?.preset || '').toLowerCase();
      return presets.find((item) => item.id === presetId) || null;
    }

    function chatVisionInfo() {
      const chat = providerPayload.agents?.chat;
      if (!chat) return null;
      const provider = providerList.find((item) => item.id === chat.provider) || null;
      const preset = presetOf(provider);
      const model = preset?.models?.find((item) => item.id === chat.model) || null;
      return {
        chat,
        provider,
        supports: Boolean(model?.capabilities?.includes('vision')),
      };
    }

    // ---- 主模型原生图片输入 ----------------------------------

    const nativeSelect = h('select', { dataset: { visionNative: '1' } });
    for (const [value, label] of nativeSpec.options || []) {
      nativeSelect.append(h('option', {
        value,
        selected: value === nativeSpec.value,
      }, label));
    }
    const nativeHint = h('p', { class: 'field-help' });
    function refreshNativeHint() {
      if (nativeSelect.value === 'off') {
        nativeHint.textContent = '不向主模型附图；独立视觉仍可按需使用。';
        return;
      }
      if (nativeSelect.value === 'on') {
        nativeHint.textContent = '明确声明主模型可接收图片；仍需协议支持图片转换，否则不会附图。';
        return;
      }
      const info = chatVisionInfo();
      if (!info) {
        nativeHint.textContent = '还没有配置聊天模型，auto 无法判断视觉能力。';
        return;
      }
      const label = `${info.provider?.display_name || info.chat.provider} / ${info.chat.model}`;
      nativeHint.textContent = info.supports
        ? `当前聊天模型 ${label} 在预设中标记了 vision，auto 会附图。`
        : `当前聊天模型 ${label} 未被预设标记为 vision，auto 不会附图；`
          + '可以改成“开启”，或使用下方独立视觉。';
    }
    nativeSelect.addEventListener('change', async () => {
      await saveField(nativeSpec, nativeSelect.value);
      refreshNativeHint();
    });
    refreshNativeHint();

    const nativeChildren = [
      field('图片输入策略', nativeSelect,
        'auto：仅预设标记 vision 的聊天模型；on：显式开启；off：不向主模型附图。'),
      nativeHint,
    ];
    if (tokenSpec) {
      nativeChildren.push(control(tokenSpec,
        (value) => saveField(tokenSpec, value)));
    }
    if (maxImagesSpec) {
      nativeChildren.push(control(maxImagesSpec,
        (value) => saveField(maxImagesSpec, value)));
    }
    if (providerLoadError) {
      nativeChildren.push(h('p', { class: 'field-help' },
        `读取 provider/预设失败，无法判断当前聊天模型：${providerLoadError}`));
    }
    nodes.push(card('主模型原生图片输入',
      '让聊天模型直接接收用户图片。预设模型是否支持由能力表决定；'
      + '这条路径不需要独立视觉 provider。',
      ...nativeChildren));

    // ---- 独立视觉识别 ----------------------------------------

    const enabledInput = h('input', {
      type: 'checkbox',
      checked: Boolean(enabledSpec.value),
      dataset: { visionToggle: '1' },
    });
    const enabledRow = h('label', { class: 'switch' }, enabledInput,
      '启用独立视觉识别（describe_image）');

    const currentProvider = String(providerSpec.value || '').trim();
    const providerSelect = h('select', { dataset: { visionProvider: '1' } });
    providerSelect.append(h('option', { value: '' },
      '自定义 OpenAI 兼容端点（不引用已有 provider）'));
    for (const provider of providerList) {
      const preset = presetOf(provider);
      const label = `${provider.display_name || provider.id} (${provider.id})`
        + (preset ? ` · 预设 ${preset.display_name || preset.id}` : '');
      providerSelect.append(h('option', {
        value: provider.id,
        selected: provider.id === currentProvider,
      }, label));
    }
    if (currentProvider
        && !providerList.some((provider) => provider.id === currentProvider)) {
      providerSelect.append(h('option', {
        value: currentProvider,
        selected: true,
      }, `当前配置：${currentProvider}（不在 provider 列表）`));
    }
    providerSelect.value = currentProvider;
    let lastProvider = providerSelect.value;

    const modelInput = h('input', {
      value: String(modelSpec.value || ''),
      placeholder: '例如 glm-4v / qwen-vl-max',
      list: 'vision-model-options',
      dataset: { visionModel: '1' },
    });
    const modelDatalist = h('datalist', { id: 'vision-model-options' });
    function visionModelsFor(providerId) {
      if (providerId) {
        const provider = providerList.find((item) => item.id === providerId);
        const preset = presetOf(provider);
        const models = Array.isArray(preset?.models) ? preset.models : [];
        const visionModels = models.filter(
          (model) => (model.capabilities || []).includes('vision'));
        return visionModels.length ? visionModels : models;
      }
      const seen = new Set();
      const out = [];
      for (const preset of presets) {
        for (const model of preset.models || []) {
          if (!(model.capabilities || []).includes('vision') || seen.has(model.id)) {
            continue;
          }
          seen.add(model.id);
          out.push(model);
        }
      }
      return out;
    }
    function refreshModelOptions() {
      fill(modelDatalist);
      for (const model of visionModelsFor(providerSelect.value)) {
        modelDatalist.append(h('option', { value: model.id },
          `${model.display_name || model.id}（${model.id}）`));
      }
    }

    const baseUrlInput = h('input', {
      value: String(baseUrlSpec.value || ''),
      placeholder: 'https://api.example.com/v1',
      dataset: { visionBaseUrl: '1' },
    });
    const keyInput = h('input', {
      type: 'password',
      placeholder: apiKeySpec.value
        ? `已配置 ${apiKeySpec.value}（留空不修改）`
        : '未设置',
      dataset: { visionKey: '1' },
    });
    const overrideInput = h('input', {
      type: 'checkbox',
      checked: Boolean(currentProvider
        && (baseUrlSpec.value || apiKeySpec.value)),
      dataset: { visionOverride: '1' },
    });
    const overrideRow = h('label', { class: 'switch' }, overrideInput,
      '覆盖该 provider 的地址/密钥（高级）');
    const providerRow = field('视觉 provider', providerSelect,
      '引用“设置→模型”里已添加的 provider；选择已有 provider 后通常不需要再填地址和 Key。');
    const modelRow = field('视觉模型', h('div', {}, modelInput, modelDatalist),
      '独立看图模型 ID，例如 glm-4v / qwen-vl-max；可以和聊天模型不同。');
    const baseUrlRow = field('视觉独立 base_url', baseUrlInput,
      '自定义端点必填；选择已有 provider 时留空即可复用其地址。');
    const keyRow = field('视觉独立 API Key', keyInput,
      '自定义端点必填；选择已有 provider 时留空即可复用其密钥。');
    const providerWarning = h('p', { class: 'field-help' });
    const customNote = h('p', { class: 'field-help' });
    const saveStatus = h('p', { class: 'field-help' });
    const saveBtn = h('button', {
      class: 'btn btn-primary',
      dataset: { visionSave: '1' },
    }, '保存视觉设置');

    saveBtn.addEventListener('click', async () => {
      const wantEnabled = enabledInput.checked;
      const providerValue = providerSelect.value.trim();
      const modelValue = modelInput.value.trim();
      const isCustom = !providerValue;
      const useOverride = isCustom || overrideInput.checked;
      const baseUrlValue = useOverride ? baseUrlInput.value.trim() : '';
      const keyValue = useOverride ? keyInput.value.trim() : '';
      const existingKeyId = String(apiKeySpec.value || '');

      if (wantEnabled) {
        if (!modelValue) {
          saveStatus.textContent = '请填写视觉模型 ID。';
          modelInput.focus();
          return;
        }
        if (isCustom && !baseUrlValue) {
          saveStatus.textContent = '自定义视觉端点需要填写 base_url。';
          baseUrlInput.focus();
          return;
        }
        if (isCustom && !keyValue && !existingKeyId) {
          saveStatus.textContent = '自定义视觉端点需要填写 API Key。';
          keyInput.focus();
          return;
        }
      }

      const payload = { enabled: wantEnabled };
      if (wantEnabled) {
        // 一次请求保存全部依赖字段；后端统一校验后再落盘一次。
        payload.provider = providerValue;
        payload.model = modelValue;
        payload.base_url = baseUrlValue;
        payload.api_key_id = useOverride ? existingKeyId : '';
        if (keyValue) payload.api_key = keyValue;
      }

      saveBtn.disabled = true;
      saveStatus.textContent = '正在保存…';
      try {
        const result = await endpoints.patchVision(payload);
        const saved = result.value || {};
        enabledSpec.value = Boolean(saved.enabled);
        providerSpec.value = saved.provider || '';
        modelSpec.value = saved.model || '';
        baseUrlSpec.value = saved.base_url || '';
        apiKeySpec.value = saved.api_key_id || '';
        lastProvider = providerSpec.value;
        if (result.api_key_saved) keyInput.value = '';
        keyInput.placeholder = apiKeySpec.value
          ? `已配置 ${apiKeySpec.value}（留空不修改）`
          : '未设置';
        saveStatus.textContent = wantEnabled
          ? '视觉设置已保存；重启 Runtime 后生效。'
          : '独立视觉已关闭；provider、模型和密钥配置仍保留。';
        markRestart();
        ctx.toast('视觉设置已保存', 'ok');
      } catch (e) {
        saveStatus.textContent = `保存失败：${e.message}`;
        ctx.toast(`视觉设置保存失败：${e.message}`, 'error');
      } finally {
        saveBtn.disabled = false;
      }
    });

    const independentFields = h('div', {
      dataset: { visionDetails: '1' },
    },
      providerWarning,
      customNote,
      providerRow,
      modelRow,
      overrideRow,
      baseUrlRow,
      keyRow,
    );

    function syncVisibility() {
      independentFields.style.display = enabledInput.checked ? '' : 'none';
      const isCustom = !providerSelect.value;
      const showOverride = isCustom || overrideInput.checked;
      overrideRow.style.display = isCustom ? 'none' : '';
      baseUrlRow.style.display = showOverride ? '' : 'none';
      keyRow.style.display = showOverride ? '' : 'none';

      const provider = providerList.find(
        (item) => item.id === providerSelect.value);
      if (isCustom) {
        customNote.textContent = '未选择已有 provider：需要填写 base_url 和 API Key，'
          + '按 OpenAI 兼容协议连接。';
      } else if (provider) {
        customNote.textContent = `复用 provider ${provider.id} 的协议、地址与密钥；`
          + '只有视觉端点不同时才勾选“覆盖”。';
      } else {
        customNote.textContent = '当前视觉 provider 不在已添加列表中，请重新选择。';
      }
      let warning = '';
      if (providerLoadError) {
        warning = `读取 provider 列表失败：${providerLoadError}`;
      } else if (!providerList.length) {
        warning = '还没有添加 provider。可以到“模型”页添加，或使用自定义端点。';
      }
      providerWarning.textContent = warning;
      providerWarning.style.display = warning ? '' : 'none';
      refreshModelOptions();
    }

    providerSelect.addEventListener('change', () => {
      if (providerSelect.value !== lastProvider) {
        // 换 provider 时不沿用上一个 provider 的地址/密钥覆盖值。
        overrideInput.checked = false;
        baseUrlInput.value = '';
        keyInput.value = '';
      }
      lastProvider = providerSelect.value;
      syncVisibility();
    });
    overrideInput.addEventListener('change', syncVisibility);
    enabledInput.addEventListener('change', syncVisibility);
    syncVisibility();

    nodes.push(card('独立视觉识别（可选）',
      '主模型不能看图，或想把看图交给另一个模型时使用。开启后只提供 '
      + 'describe_image 工具，按需调用，不会每轮自动跑。',
      enabledRow,
      h('p', { class: 'field-help' },
        '关闭时保留配置，不会删除密钥；只有开启后才会构造独立视觉客户端。'),
      independentFields,
      h('div', { class: 'row' },
        h('div', { class: 'spacer' }),
        saveBtn),
      saveStatus));
  }

  // ============================================================
  // 联网搜索（专用面板）
  // ============================================================

  async function appendSearchPanel(nodes, group) {
    const byPath = new Map(group.fields.map((spec) => [spec.path, spec]));
    const enabledSpec = byPath.get('features.web_search.enabled');
    const freeEnabledSpec = byPath.get('features.web_search.free_enabled');
    const providerSpec = byPath.get('features.web_search.provider');
    const searxngSpec = byPath.get('features.web_search.searxng_base_url');
    const maxSpec = byPath.get('features.web_search.max_results');
    const timeoutSpec = byPath.get('features.web_search.timeout_seconds');
    const officialEnabledSpec = byPath.get('features.web_search.official.enabled');
    const officialProviderSpec = byPath.get('features.web_search.official.provider');
    const officialModelSpec = byPath.get('features.web_search.official.model');
    const officialTimeoutSpec = byPath.get(
      'features.web_search.official.timeout_seconds');
    if (!enabledSpec || !freeEnabledSpec || !providerSpec || !searxngSpec
        || !maxSpec || !timeoutSpec || !officialEnabledSpec
        || !officialProviderSpec || !officialModelSpec || !officialTimeoutSpec) {
      return;
    }

    let providerPayload = { providers: [], agents: {} };
    let presets = [];
    let loadError = '';
    try {
      providerPayload = await endpoints.providers();
    } catch (e) {
      loadError = e.message;
    }
    try {
      const data = await endpoints.providerPresets();
      presets = Array.isArray(data.presets) ? data.presets : [];
    } catch {
      // 预设只用于官方通道下拉；失败时仍可配置免费搜索。
    }
    const providerList = Array.isArray(providerPayload.providers)
      ? providerPayload.providers : [];

    function presetOf(provider) {
      const presetId = String(provider?.preset || '').toLowerCase();
      return presets.find((item) => item.id === presetId) || null;
    }

    const officialOptions = [];
    for (const provider of providerList) {
      const preset = presetOf(provider);
      if (!preset) continue;
      if (preset.search_channel) {
        officialOptions.push({
          value: `${provider.id}::`,
          label: `${provider.display_name || provider.id} · `
            + `${preset.search_label || '官方搜索'}（provider 级）`,
        });
      }
      for (const model of preset.models || []) {
        if (!model.search_channel
            || !(model.capabilities || []).includes('web_search')) {
          continue;
        }
        officialOptions.push({
          value: `${provider.id}::${model.id}`,
          label: `${provider.display_name || provider.id} / `
            + `${model.display_name || model.id}`,
        });
      }
    }
    const currentOfficial = `${String(officialProviderSpec.value || '')}::`
      + `${String(officialModelSpec.value || '')}`;
    if (currentOfficial !== '::'
        && !officialOptions.some((item) => item.value === currentOfficial)) {
      officialOptions.push({
        value: currentOfficial,
        label: `当前配置：${currentOfficial}（不在预设搜索列表）`,
      });
    }

    const masterInput = h('input', {
      type: 'checkbox', checked: Boolean(enabledSpec.value),
      dataset: { searchMaster: '1' },
    });
    const masterRow = h('label', { class: 'switch' }, masterInput,
      '启用联网搜索 web_search');

    // ---- 免费搜索 ---------------------------------------------
    const freeInput = h('input', {
      type: 'checkbox', checked: Boolean(freeEnabledSpec.value),
      dataset: { searchFree: '1' },
    });
    const freeRow = h('label', { class: 'switch' }, freeInput,
      '启用免费搜索（优先）');
    const engineSelect = h('select', {
      dataset: { searchEngine: '1' },
    });
    for (const [value, label] of providerSpec.options || []) {
      engineSelect.append(h('option', {
        value, selected: value === providerSpec.value,
      }, label));
    }
    engineSelect.value = providerSpec.value || 'bing_rss';
    const searxngInput = h('input', {
      value: String(searxngSpec.value || ''),
      placeholder: 'https://searx.example.com',
      dataset: { searchSearxng: '1' },
    });
    const searxngRow = field('SearXNG 地址', searxngInput,
      '需要实例开启 JSON 输出；自建实例更适合长期使用。');
    const maxInput = h('input', {
      type: 'number', min: '1', max: '20',
      value: maxSpec.value ?? 5,
    });
    const timeoutInput = h('input', {
      type: 'number', min: '1', max: '60', step: '1',
      value: timeoutSpec.value ?? 10,
    });
    const probeBtn = h('button', {
      class: 'btn', dataset: { searchProbe: '1' },
    }, '测速免费搜索');
    const probeStatus = h('p', { class: 'field-help' });
    const freeDetails = h('div', {
      dataset: { searchFreeDetails: '1' },
    },
      field('免费搜索后端', engineSelect,
        'Bing RSS 国内可直连且 RSS 无广告；百度国内可直连，页面较大时会稍慢；'
        + 'SearXNG 需要自建实例并开启 JSON。'),
      searxngRow,
      field('最多结果数', maxInput, '1–20，建议 5。'),
      field('免费搜索超时（秒）', timeoutInput, '测速和单次请求都会受此限制。'),
      h('div', { class: 'row row-wrap' }, probeBtn),
      probeStatus,
      h('p', { class: 'field-help' },
        'Bing RSS 受 Microsoft 的 RSS 个人非商业使用条款约束；百度网页解析'
        + '请自行确认目标站点条款；商业部署建议自建 SearXNG、使用官方搜索 API '
        + '或取得授权。'),
    );

    // ---- 官方搜索 ---------------------------------------------
    const officialInput = h('input', {
      type: 'checkbox', checked: Boolean(officialEnabledSpec.value),
      dataset: { searchOfficial: '1' },
    });
    const officialRow = h('label', { class: 'switch' }, officialInput,
      '启用官方搜索（仅在免费搜索不可用时回退）');
    const channelSelect = h('select', {
      dataset: { searchChannel: '1' },
    });
    channelSelect.append(h('option', { value: '' }, '未选择官方搜索通道'));
    for (const option of officialOptions) {
      channelSelect.append(h('option', {
        value: option.value, selected: option.value === currentOfficial,
      }, option.label));
    }
    channelSelect.value = officialOptions.some(
      (item) => item.value === currentOfficial) ? currentOfficial : '';
    const officialTimeoutInput = h('input', {
      type: 'number', min: '5', max: '180', step: '1',
      value: officialTimeoutSpec.value ?? 45,
    });
    const officialDetails = h('div', {
      dataset: { searchOfficialDetails: '1' },
    },
      field('官方搜索通道', channelSelect,
        '只列出预设中已确认存在官方搜索 API 的 provider / 模型。'),
      field('官方搜索超时（秒）', officialTimeoutInput,
        '官方搜索可能包含模型生成与搜索，通常比免费搜索慢。'),
      loadError
        ? h('p', { class: 'field-help' }, `读取 provider/预设失败：${loadError}`)
        : null,
      !officialOptions.length
        ? h('p', { class: 'field-help' },
          '当前已添加的 provider 没有已确认的官方搜索通道；'
          + '可以先到「模型」页添加对应预设。')
        : null,
      h('p', { class: 'field-help' },
        '官方搜索会调用模型/搜索 API，可能产生额外 token 或按次搜索费用，'
        + '具体以 provider 官方账单为准。两者都开时免费搜索优先；'
        + '只有免费搜索测速或本次请求失败才回退到官方；只开一个时只用该通道。'),
    );

    // ---- 保存 -------------------------------------------------
    const saveBtn = h('button', {
      class: 'btn btn-primary', dataset: { searchSave: '1' },
    }, '保存搜索设置');
    const saveStatus = h('p', { class: 'field-help' });

    function syncVisibility() {
      const master = masterInput.checked;
      freeInput.disabled = !master;
      officialInput.disabled = !master;
      freeDetails.style.display = master && freeInput.checked ? '' : 'none';
      officialDetails.style.display = master && officialInput.checked ? '' : 'none';
      searxngRow.style.display = engineSelect.value === 'searxng' ? '' : 'none';
      probeBtn.disabled = !freeInput.checked;
    }

    engineSelect.addEventListener('change', syncVisibility);
    freeInput.addEventListener('change', syncVisibility);
    officialInput.addEventListener('change', syncVisibility);
    masterInput.addEventListener('change', syncVisibility);

    probeBtn.addEventListener('click', async () => {
      const provider = engineSelect.value;
      const searxng = searxngInput.value.trim();
      if (provider === 'searxng' && !searxng) {
        probeStatus.textContent = '请先填写 SearXNG 地址。';
        return;
      }
      probeBtn.disabled = true;
      probeStatus.textContent = '测速中…';
      try {
        const result = await endpoints.probeSearch({
          provider,
          searxng_base_url: searxng,
          timeout_seconds: Number(timeoutInput.value) || 10,
        });
        probeStatus.textContent = result.ok
          ? `可用：${result.latency_ms} ms（${result.engine}）`
          : `不可用：${result.error || result.error_code || '未知错误'}`
            + `（${result.latency_ms || 0} ms）`;
      } catch (e) {
        probeStatus.textContent = `测速失败：${e.message}`;
      } finally {
        probeBtn.disabled = false;
      }
    });

    saveBtn.addEventListener('click', async () => {
      const master = masterInput.checked;
      const freeEnabled = freeInput.checked;
      const officialEnabled = officialInput.checked;
      const provider = engineSelect.value;
      const searxng = searxngInput.value.trim();
      if (master && !freeEnabled && !officialEnabled) {
        saveStatus.textContent = '至少开启免费搜索或官方搜索。';
        return;
      }
      if (master && freeEnabled && provider === 'searxng' && !searxng) {
        saveStatus.textContent = '选择 SearXNG 时需要填写实例地址。';
        return;
      }
      const rawChannel = channelSelect.value;
      if (master && officialEnabled && !rawChannel) {
        saveStatus.textContent = '开启官方搜索前请选择一个官方搜索通道。';
        return;
      }
      const sep = rawChannel.indexOf('::');
      const officialProvider = sep >= 0 ? rawChannel.slice(0, sep) : '';
      const officialModel = sep >= 0 ? rawChannel.slice(sep + 2) : '';
      const payload = {
        enabled: master,
        free_enabled: freeEnabled,
        provider,
        searxng_base_url: searxng,
        max_results: Number(maxInput.value) || 5,
        timeout_seconds: Number(timeoutInput.value) || 10,
        official_enabled: officialEnabled,
        official_provider: officialProvider,
        official_model: officialModel,
        official_timeout_seconds: Number(officialTimeoutInput.value) || 45,
      };
      saveBtn.disabled = true;
      saveStatus.textContent = '正在保存…';
      try {
        const result = await endpoints.patchSearch(payload);
        const saved = result.value || {};
        enabledSpec.value = Boolean(saved.enabled);
        freeEnabledSpec.value = Boolean(saved.free_enabled);
        providerSpec.value = saved.provider || 'bing_rss';
        searxngSpec.value = saved.searxng_base_url || '';
        maxSpec.value = saved.max_results ?? 5;
        timeoutSpec.value = saved.timeout_seconds ?? 10;
        officialEnabledSpec.value = Boolean(saved.official_enabled);
        officialProviderSpec.value = saved.official_provider || '';
        officialModelSpec.value = saved.official_model || '';
        officialTimeoutSpec.value = saved.official_timeout_seconds ?? 45;
        saveStatus.textContent = '搜索设置已保存；重启 Runtime 后生效。';
        markRestart();
        ctx.toast('搜索设置已保存', 'ok');
      } catch (e) {
        saveStatus.textContent = `保存失败：${e.message}`;
        ctx.toast(`搜索设置保存失败：${e.message}`, 'error');
      } finally {
        saveBtn.disabled = false;
      }
    });

    syncVisibility();
    nodes.push(card('联网搜索',
      '免费搜索优先；官方搜索只在免费通道测速/请求失败时回退，且需要显式开启。',
      masterRow,
      freeRow,
      freeDetails,
    ));
    nodes.push(card('官方搜索（可选）',
      '调用模型或搜索服务的官方 API，可能产生额外费用。',
      officialRow,
      officialDetails,
      h('div', { class: 'row' },
        h('div', { class: 'spacer' }),
        saveBtn),
      saveStatus,
    ));
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
    const restartBackendBtn = h('button', {
      class: 'btn', dataset: { backendRestart: '1' },
    }, '重启后端');
    restartBackendBtn.addEventListener('click', () => {
      promptBackendRestart(ctx, {
        title: '重启后端？',
        message: '重启会重新加载磁盘上的 Python 代码。只改配置时用“重启 Runtime”即可。',
      });
    });
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
      h('p', { class: 'field-help' },
        '“重启 Runtime”只重建运行时，不会重新导入 Python 代码；'
        + '更新到新版本或改了后端代码后，请用“重启后端”。'),
      h('div', { class: 'row' },
        restartBackendBtn,
        h('div', { class: 'spacer' }),
        checkBtn),
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
    const probeBtn = h('button', {
      class: 'btn', dataset: { updateProbe: '1' },
    }, '测速 GitHub / 镜像');
    const probeStatus = h('p', { class: 'field-help' });
    const probeHost = h('div', {});
    probeBtn.addEventListener('click', async () => {
      probeBtn.disabled = true;
      probeStatus.textContent = '正在测试连接…';
      fill(probeHost);
      try {
        const result = await endpoints.updateProbe({ timeout_seconds: 8 });
        const rows = (result.results || []).map((item) => {
          const useBtn = h('button', { class: 'btn btn-sm' }, '使用');
          useBtn.addEventListener('click', async () => {
            const preset = presets.find(
              (entry) => (entry.value || '') === (item.mirror || ''));
            select.value = preset ? preset.id : 'custom';
            custom.value = item.mirror || '';
            sync();
            try {
              await endpoints.patchSetting('update.mirror', item.mirror || '');
              status.mirror = item.mirror || '';
              saveStatus.textContent = status.mirror
                ? `已保存：${status.mirror}`
                : '已保存：使用 GitHub 官方直连';
              onSaved();
            } catch (e) {
              saveStatus.textContent = `保存失败：${e.message}`;
            }
          });
          return h('tr', {},
            h('td', {}, item.label || item.id),
            h('td', { class: 'mono' }, `${item.latency_ms || 0} ms`),
            h('td', { class: item.ok ? 'ok' : 'bad' },
              item.ok ? '可用' : (item.error || item.error_code || '失败')),
            h('td', {}, item.ok ? useBtn : null));
        });
        probeStatus.textContent = '测速完成；只执行 git ls-remote，不下载代码、不更新。';
        fill(probeHost, rows.length
          ? table(['源', '延迟', '状态', ''], rows)
          : empty('没有可测试的源'));
      } catch (e) {
        probeStatus.textContent = `测速失败：${e.message}`;
      } finally {
        probeBtn.disabled = false;
      }
    });

    return card('更新镜像',
      '预设几个常用源，也可以在下方填写自定义镜像。第三方源会看到代码请求，只使用你信任的源。',
      field('预设镜像', select, ''),
      field('自定义镜像站', custom, ''),
      note,
      h('div', { class: 'row row-wrap' },
        probeBtn,
        h('div', { class: 'spacer' }),
        saveBtn),
      probeStatus,
      probeHost,
      saveStatus,
    );
  }

  const BACKUP_STATE_TEXT = {
    queued: '排队中',
    running: '备份中',
    cancelling: '正在取消',
    cancelled: '已取消',
    interrupted: '已中断',
    done: '已完成',
    partial: '部分完成',
    failed: '失败',
  };
  const BACKUP_STAGE_TEXT = {
    queued: '排队中',
    scanning: '扫描文件',
    writing: '写入压缩包',
    verifying: '验证压缩包',
    hashing: '计算校验值',
    cancelling: '收尾中',
    cancelled: '已取消',
    interrupted: '已中断',
    done: '完成',
    failed: '失败',
  };
  const BACKUP_TERMINAL_STATES = new Set([
    'done', 'partial', 'failed', 'cancelled', 'interrupted',
  ]);

  function formatBytes(value) {
    const num = Math.max(0, Number(value) || 0);
    if (num >= 1024 ** 3) return `${(num / 1024 ** 3).toFixed(2)} GiB`;
    if (num >= 1024 ** 2) return `${(num / 1024 ** 2).toFixed(1)} MiB`;
    if (num >= 1024) return `${(num / 1024).toFixed(1)} KiB`;
    return `${num} B`;
  }

  async function appendBackupPanel(nodes) {
    let status = null;
    try {
      status = await endpoints.backupStatus();
    } catch (e) {
      nodes.push(card('一键备份用户数据', '',
        empty(`读取备份状态失败：${e.message}`)));
      return;
    }
    let pollTimer = null;
    const statusLine = h('p', { class: 'field-help' });
    const progressHost = h('div', {});
    const filesHost = h('div', {});
    const summaryHost = h('div', {});
    const startBtn = h('button', { class: 'btn btn-primary' }, '立即备份');
    const refreshBtn = h('button', { class: 'btn' }, '刷新');

    function clearPoll() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function renderProgress(job) {
      if (!job) {
        fill(progressHost);
        startBtn.disabled = false;
        return;
      }
      const total = Number(job.total_bytes || 0);
      const done = Number(job.processed_bytes || 0);
      const pct = total > 0 ? Math.max(0, Math.min(100, Math.round(done / total * 100))) : 0;
      const stateText = BACKUP_STATE_TEXT[job.state] || job.state;
      const stageText = BACKUP_STAGE_TEXT[job.stage] || job.stage;
      const running = ['running', 'queued', 'cancelling'].includes(job.state);
      const line = running
        ? `${stateText} · ${stageText}`
          + (total > 0 ? ` · ${formatBytes(done)} / ${formatBytes(total)}` : '')
        : `${stateText}${job.filename ? ` · ${job.filename}` : ''}`;
      const cancelBtn = running && job.state !== 'cancelling'
        ? h('button', { class: 'btn btn-sm' }, '取消备份')
        : null;
      if (cancelBtn) {
        cancelBtn.addEventListener('click', async () => {
          cancelBtn.disabled = true;
          statusLine.textContent = '正在请求取消…';
          try {
            const updated = await endpoints.backupCancel(job.id);
            renderProgress(updated);
            pollJob(job.id);
          } catch (e) {
            cancelBtn.disabled = false;
            statusLine.textContent = `取消失败：${e.message}`;
          }
        });
      }
      fill(progressHost,
        h('div', { class: 'row' },
          h('span', {}, line),
          h('div', { class: 'spacer' }),
          cancelBtn,
          job.error ? h('span', { class: 'badge bad' }, '失败') : null),
        running
          ? h('div', { class: 'backup-progress' },
            h('div', {
              class: 'backup-progress-bar',
              style: { width: `${pct}%` },
            }))
          : null,
        job.current ? h('p', { class: 'field-help mono' }, job.current) : null,
        ...(job.warnings || []).map((warning) =>
          h('p', { class: 'field-help' }, warning)),
        job.error
          ? h('p', { class: 'field-help backup-error' },
            `${job.error.code}: ${job.error.message}`)
          : null,
      );
      startBtn.disabled = running;
      if (job.state === 'done') {
        statusLine.textContent = `备份完成：${job.filename}（${formatBytes(job.size)}）`;
      } else if (job.state === 'partial') {
        statusLine.textContent = `备份部分完成：${job.filename}（${formatBytes(job.size)}），请查看警告`;
      } else if (job.state === 'failed') {
        statusLine.textContent = `备份失败：${job.error?.message || '未知错误'}`;
      } else if (job.state === 'cancelled') {
        statusLine.textContent = '备份已取消，未生成可下载文件。';
      } else if (job.state === 'interrupted') {
        statusLine.textContent = '上次备份被服务重启中断；可重新发起备份。';
      }
    }

    function renderFiles() {
      const files = status.files || [];
      if (!files.length) {
        fill(filesHost, empty('还没有备份文件。'));
        return;
      }
      fill(filesHost, h('div', { class: 'backup-file-list' },
        ...files.map((file) => {
          const verifyBtn = h('button', { class: 'btn btn-sm' }, '离线验证');
          verifyBtn.addEventListener('click', async () => {
            verifyBtn.disabled = true;
            statusLine.textContent = `正在验证 ${file.filename}…`;
            try {
              const report = await endpoints.backupVerify(file.filename);
              const problems = [
                ...(report.errors || []).map((item) => `${item.code}: ${item.message}`),
                ...(report.warnings || []),
              ];
              statusLine.textContent = `${report.ok ? '验证通过' : '验证失败'}`
                + ` · 聊天可恢复=${report.chat_restorable ? '是' : '否'}`
                + ` · 凭据可恢复=${report.credential_restorable ? '是' : '否'}`
                + (problems.length ? ` · ${problems.slice(0, 2).join('；')}` : '');
            } catch (e) {
              statusLine.textContent = `验证失败：${e.message}`;
            } finally {
              verifyBtn.disabled = false;
            }
          });
          return h('div', { class: 'backup-file' },
            h('div', { class: 'backup-file-main' },
              h('div', { class: 'backup-file-name mono' }, file.filename),
              h('div', { class: 'field-help' },
                `${formatDateTime(file.created_at)} · ${formatBytes(file.size)}`
                + (file.includes_keys ? ' · 含密钥材料' : '')
                + (file.credential_restorable ? ' · 密钥材料可验证' : '')
                + (file.include_media === false ? ' · 未含入站媒体' : '')
                + (file.validation_status === 'verified'
                  ? ' · 已验证' : ' · 未完整验证')
                + (file.partial ? ' · 部分完成' : '')),
            ),
            verifyBtn,
            h('a', {
              class: 'btn btn-sm',
              href: endpoints.backupDownloadUrl(file.filename),
              download: file.filename,
            }, '下载'),
          );
        }),
      ));
    }

    function renderSummary() {
      const active = (status.jobs || []).find(
        (job) => ['running', 'queued', 'cancelling'].includes(job.state),
      ) || null;
      startBtn.disabled = !!active;
      fill(summaryHost, table(['项目', '值'], [
        h('tr', {}, h('td', {}, '备份目录'),
          h('td', { class: 'mono' }, status.directory)),
        h('tr', {}, h('td', {}, '入站媒体'),
          h('td', {}, status.include_media ? '包含' : '不包含')),
        h('tr', {}, h('td', {}, '上次备份'),
          h('td', {}, status.files?.[0]
            ? `${status.files[0].filename}（${formatBytes(status.files[0].size)}）`
            : '还没有备份')),
      ]));
      renderFiles();
      renderProgress(active);
      if (active && !pollTimer) pollJob(active.id);
    }

    async function refreshStatus() {
      try {
        status = await endpoints.backupStatus();
        renderSummary();
      } catch (e) {
        statusLine.textContent = `读取备份状态失败：${e.message}`;
      }
    }

    async function pollJob(jobId) {
      clearPoll();
      if (!startBtn.isConnected) return;
      try {
        const job = await endpoints.backupJob(jobId);
        renderProgress(job);
        if (BACKUP_TERMINAL_STATES.has(job.state)) {
          await refreshStatus();
          return;
        }
      } catch (e) {
        statusLine.textContent = `读取备份进度失败：${e.message}`;
      }
      pollTimer = setTimeout(() => pollJob(jobId), 1000);
    }

    startBtn.addEventListener('click', async () => {
      startBtn.disabled = true;
      statusLine.textContent = '正在启动备份…';
      try {
        // 不传 include_media：让后端读取当前配置，避免字段刚保存但面板状态
        // 还是旧值时用旧覆盖值启动备份。
        const job = await endpoints.backupStart({});
        statusLine.textContent = '';
        renderProgress(job);
        pollJob(job.id);
      } catch (e) {
        startBtn.disabled = false;
        statusLine.textContent = `启动备份失败：${e.message}`;
        await refreshStatus();
      }
    });
    refreshBtn.addEventListener('click', refreshStatus);

    nodes.push(card('一键备份用户数据',
      '备份配置、加密密钥、聊天/RAG 数据库、工作区、表情包、自定义预设和人格。'
      + '各 SQLite 库分别一致性快照，不是跨库全局事务；备份后的新消息不会包含在内。',
      h('p', { class: 'field-help backup-warning' },
        '安全提示：备份包含加密 API 密钥库和 RSA 私钥，拿到它就可能解密模型密钥。'
        + 'ZIP 本身未加密，0600 只是本机文件权限；请像密码一样保管，'
        + '不要上传到公共网盘或通过不安全渠道发送。'),
      summaryHost,
      h('div', { class: 'row' },
        h('div', { class: 'spacer' }),
        refreshBtn,
        startBtn),
      statusLine,
      progressHost,
      h('h2', { class: 'card-title', style: { marginTop: '16px' } }, '已有备份'),
      filesHost,
      h('p', { class: 'field-help' },
        '恢复为手动操作：停止服务后按压缩包内 README_RESTORE.txt 操作；'
        + '本页不提供在线一键恢复。'),
    ));
    renderSummary();
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
    const visibleFields = group.fields.filter(
      (spec) => !spec.hidden
        && !VISION_FIELD_PATHS.has(spec.path)
        && !SEARCH_FIELD_PATHS.has(spec.path));
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
    if (key === 'features') {
      await appendVisionPanel(nodes, group);
      await appendSearchPanel(nodes, group);
    }
    if (key === 'update') {
      await appendUpdatePanel(nodes);
    }
    if (key === 'backup') {
      await appendBackupPanel(nodes);
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
