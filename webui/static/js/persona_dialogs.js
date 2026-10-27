// 人格生成 / 改写 / 复制对话框。
//
// 对应原桌面端的 ui/dashboard/persona_dialogs.py：
//   _PersonaCreatorDialog —— 填简述 → AI 生成 → 预览 → 采用
//   _PersonaRefineDialog  —— 贴一句反馈 → 「生成修改版」→ 可反复出第 N 版 → 覆盖（自动备份）
//
// 生成用的模型取自 agents.persona_gen（缺省回落主聊天模型），由后端决定；
// 前端只负责收集描述、展示草稿、把用户点下的"采用"变成一次落盘请求。
//
// 草稿**只存在浏览器内存里**，用户点「采用」才写盘。这样反复重生成不会留下垃圾文件。

import { h, fill, field, modal } from './dom.js';
import { icon } from './icons.js';
import { endpoints } from './api.js';

// 与原 GUI 的 persona_create 步骤一致：想到什么写什么，空着也能生成
const BRIEF_FIELDS = [
  ['personality', '这个人格是什么样的人', 3,
   '话多话少、急性子慢性子、有什么小怪癖、对什么人什么态度。不用全，AI 会帮你补。'],
  ['background', '过往', 2, '从哪儿来，做过什么。帮你理解 ta 是谁就够了，不用写传记。'],
  ['voice_samples', '说话的样子', 2, '贴几句 ta 常说的话就行。比如「嗯」「可以的」「我想一下」。'],
  ['boundaries', '不会做的事', 2, 'ta 不做什么、不接受什么。几句话划定边界就行。'],
  ['never_say', '绝不会说的话', 2, '比会说什么更能定义一个人。比如绝不叫别人宝贝、绝不主动夸人。'],
  ['relation_matrix', '对不同人，有什么差别', 2, '一个人对死党和对长辈是两种语气。不写 AI 也能推导。'],
  ['sensitive_topics', '什么话题会让 ta 沉默或岔开', 2, '有没有一提就躲的事。可以不填。'],
  ['extra_notes', '还有什么要补充的', 2, '比如「别写成客服」「少说教」。'],
];

/** 生成结果的 XML 预览。按顶层标签拆成小节，比一整块 XML 好读。 */
function previewNode(xml) {
  const tags = ['identity', 'past', 'personality', 'voice', 'boundaries',
                'relation_with_user', 'consistency_anchors'];
  const sections = [];
  for (const tag of tags) {
    const match = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`).exec(xml);
    if (match) sections.push({ tag, body: match[1].trim() });
  }
  if (!sections.length) {
    // 模型没按规范输出时也要能看——原样展示，不装作解析成功
    return h('pre', { class: 'draft-raw' }, xml);
  }
  return h('div', { class: 'draft-sections' },
    ...sections.map((s) => h('section', { class: 'draft-section' },
      h('div', { class: 'stat-label' }, s.tag),
      h('div', { style: { whiteSpace: 'pre-wrap' } }, s.body),
    )),
  );
}

function draftToolbar(onRegenerate, extra = []) {
  return [
    h('button', { class: 'btn', onclick: onRegenerate },
      icon('refresh', { size: 16 }), '再生成一次'),
    ...extra,
  ];
}

// ============================================================
// 新建人格
// ============================================================

export async function openPersonaCreator(ctx, { onDone } = {}) {
  const state = { name: '', xml: '', refined: 0, busy: false };

  const nameInput = h('input', {
    placeholder: 'linwan（目录名，字母开头）',
    oninput: (e) => { state.name = e.target.value.trim(); },
  });
  const displayName = h('input', {
    placeholder: '林晚',
    oninput: (e) => { state.displayName = e.target.value.trim(); },
  });

  const inputs = {};
  const directBtn = h('button', {
    class: 'btn btn-sm',
    onclick: () => {
      dialog.close();
      openPersonaDirectEditor(ctx, null, { onDone });
    },
  }, '不用 AI，直接写提示词');
  const bodyNodes = [
    h('p', { class: 'card-sub' },
      '写几句你想要的人格的样子，AI 会补成一份完整人设。生成后可以反复重写，满意了再采用。'
      + '已经有完整提示词？可以直接写。'),
    h('div', { class: 'row' }, directBtn),
    h('div', { class: 'grid grid-2' },
      field('人格名（目录名）', nameInput, '字母开头，它会成为 personas/ 下的目录名'),
      field('显示名', displayName, '人格自称的名字，可以中文'),
    ),
  ];
  for (const [key, label, rows, help] of BRIEF_FIELDS) {
    inputs[key] = h('textarea', { rows, placeholder: help });
    bodyNodes.push(field(label, inputs[key], help));
  }

  const statusLine = h('div', { class: 'field-help' }, '');
  const previewHost = h('div', {});

  const generateBtn = h('button', { class: 'btn btn-primary' },
    icon('check', { size: 16 }), '生成');

  const cancelBtn = h('button', { class: 'btn' }, '算了');
  const dialog = modal({
    title: '新建人格',
    subtitle: '回答几个问题，让模型帮你写一份完整的人格设定',
    wide: true,
    body: h('div', {}, ...bodyNodes,
      h('div', { class: 'row', style: { marginTop: '4px' } }, generateBtn),
      h('div', { style: { marginTop: '8px' } }, statusLine),
      h('div', { style: { marginTop: '16px' } }, previewHost),
    ),
    footer: [cancelBtn],
  });
  cancelBtn.onclick = () => dialog.close();

  function collectBrief() {
    const brief = { name: state.displayName || state.name, admins: [] };
    for (const [key] of BRIEF_FIELDS) brief[key] = inputs[key].value.trim();
    return brief;
  }

  function setStatus(text, kind = '') {
    statusLine.textContent = text;
    statusLine.style.color = kind === 'error' ? 'var(--error)'
      : kind === 'ok' ? 'var(--success)' : '';
  }

  function renderPreview() {
    fill(dialog.el.querySelector('.modal-foot'),
      h('button', { class: 'btn', onclick: () => dialog.close() }, '算了'),
      ...draftToolbar(generate, [
        h('button', {
          class: 'btn btn-primary',
          onclick: () => save(false),
        }, '采用并保存'),
      ]),
    );
    fill(previewHost,
      h('div', { class: 'stat-label', style: { marginBottom: '6px' } }, '预览'),
      previewNode(state.xml),
    );
  }

  async function generate() {
    if (state.busy) return;
    if (!state.name) { setStatus('先给人格起个目录名（字母开头）', 'error'); return; }
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(state.name)) {
      setStatus('目录名用字母开头，只能含字母、数字、下划线、点和短横线', 'error');
      return;
    }
    state.busy = true;
    generateBtn.disabled = true;
    setStatus('正在生成，大概要十几秒…');
    try {
      const result = await endpoints.personaDraft(collectBrief());
      state.xml = result.xml;
      state.refined = result.refined_count || 0;
      setStatus('已生成。不满意就改改上面的描述再点「再生成一次」', 'ok');
      renderPreview();
    } catch (e) {
      setStatus(e.message, 'error');
    } finally {
      state.busy = false;
      generateBtn.disabled = false;
    }
  }

  async function save(overwrite) {
    if (!state.xml) return;
    try {
      const result = await endpoints.savePersona({
        name: state.name,
        xml: state.xml,
        overwrite,
        ...collectBrief(),
      });
      ctx.toast(result.message, 'ok');
      if (result.backup) ctx.toast(`原人设已备份到 ${result.backup}`, 'ok');
      dialog.close();
      if (onDone) await onDone();
    } catch (e) {
      if (!overwrite && /已存在/.test(e.message)) {
        const backupHint = confirm(`${e.message}。\n\n要覆盖它吗？覆盖前会自动备份。`);
        if (backupHint) await save(true);
        return;
      }
      setStatus(e.message, 'error');
    }
  }

  generateBtn.onclick = generate;

  // 向导里填过人格描述的话，替用户把它填进来（只在首次新建时取，取完清掉）
  try {
    const stashed = await endpoints.personaBrief();
    if (stashed && stashed.brief) {
      if (stashed.brief.personality) inputs.personality.value = stashed.brief.personality;
      if (stashed.brief.name) displayName.value = stashed.brief.name;
      setStatus('已带入你在首次配置里写的人格描述。补个目录名就能生成。');
      await endpoints.clearPersonaBrief();
    }
  } catch {
    // 取不到就当没有，不影响手动填写
  }

  document.body.append(dialog.el);
  nameInput.focus();
  return dialog;
}

// ============================================================
// 修改人格（AI 改写）
// ============================================================

export async function openPersonaRefine(ctx, name, { onDone } = {}) {
  const state = { xml: '', refined: 0, busy: false, history: [] };

  const statusLine = h('div', { class: 'field-help' }, '正在读取当前人设…');
  const previewHost = h('div', {});
  const feedbackInput = h('textarea', {
    rows: 2,
    placeholder: '比如「再冷淡一点」「少说反问句」「她的猫呢，提一句」',
  });

  const dialog = modal({
    title: `修改 ${name}`,
    subtitle: '贴一句想改的地方，AI 会出一版修改稿。确认前不会动现有文件。',
    wide: true,
    body: h('div', {},
      field('想改哪里', feedbackInput),
      h('div', { class: 'row' },
        h('button', { class: 'btn', onclick: () => refine() },
          icon('refresh', { size: 16 }), '生成修改版'),
      ),
      h('div', { style: { marginTop: '8px' } }, statusLine),
      h('div', { style: { marginTop: '16px' } }, previewHost),
    ),
    footer: [h('button', { class: 'btn', onclick: () => dialog.close() }, '算了')],
  });

  const setStatus = (text, kind = '') => {
    statusLine.textContent = text;
    statusLine.style.color = kind === 'error' ? 'var(--error)'
      : kind === 'ok' ? 'var(--success)' : '';
  };

  async function loadSource() {
    try {
      const data = await endpoints.personaSource(name);
      state.xml = data.xml || '';
      setStatus('下面是当前人设。写一句想改的地方，点「生成修改版」。');
    } catch (e) {
      setStatus(e.message, 'error');
    }
  }

  async function refine() {
    const feedback = feedbackInput.value.trim();
    if (!feedback) { setStatus('先说说想改哪里', 'error'); return; }
    if (state.busy || !state.xml) return;
    state.busy = true;
    setStatus(`正在生成修改版（第 ${state.refined + 1} 版）…`);
    try {
      const result = await endpoints.personaRefine({
        xml: state.xml,
        feedback,
        refined_count: state.refined,
        edit_history: state.history,
      });
      state.xml = result.xml;
      state.refined = result.refined_count || state.refined + 1;
      state.history.push({ role: 'user', content: feedback });
      state.history.push({ role: 'assistant', content: result.xml });
      feedbackInput.value = '';
      setStatus(`修改版已生成（第 ${state.refined} 版）。确认后会覆盖当前人设，覆盖前自动备份。`, 'ok');
      renderPreview();
    } catch (e) {
      setStatus(e.message, 'error');
    } finally {
      state.busy = false;
    }
  }

  function renderPreview() {
    fill(previewHost,
      h('div', { class: 'stat-label', style: { marginBottom: '6px' } },
        `修改版（第 ${state.refined} 版）`),
      previewNode(state.xml),
    );
    fill(dialog.el.querySelector('.modal-foot'),
      h('button', { class: 'btn', onclick: () => dialog.close() }, '算了'),
      h('button', { class: 'btn btn-danger', onclick: () => accept() }, '采用并覆盖'),
    );
  }

  async function accept() {
    if (!confirm(`覆盖 ${name} 的人设？\n\n覆盖前会自动备份到 personas/${name}/.backups/。`)) return;
    try {
      const result = await endpoints.savePersona({
        name,
        xml: state.xml,
        overwrite: true,
      });
      ctx.toast(result.message, 'ok');
      if (result.backup) ctx.toast(`原人设已备份到 ${result.backup}`, 'ok');
      dialog.close();
      if (onDone) await onDone();
    } catch (e) {
      setStatus(e.message, 'error');
    }
  }

  document.body.append(dialog.el);
  await loadSource();
  feedbackInput.focus();
  return dialog;
}

// ============================================================
// 复制人格
// ============================================================

export function openPersonaDuplicate(ctx, name, { onDone } = {}) {
  const input = h('input', { value: `${name}_copy` });
  const statusLine = h('div', { class: 'field-help' }, '');

  const dialog = modal({
    title: '复制人格',
    subtitle: `以 ${name} 为模板建一个新人格`,
    body: h('div', {},
      field('新人格名（目录名）', input, '字母开头；重复的名字会被拒绝'),
      statusLine,
    ),
    footer: [],
  });

  const copyBtn = h('button', { class: 'btn btn-primary' }, '复制');
  copyBtn.onclick = async () => {
    const newName = input.value.trim();
    if (!newName) { statusLine.textContent = '先填个名字'; return; }
    copyBtn.disabled = true;
    try {
      const result = await endpoints.duplicatePersona(name, newName);
      ctx.toast(result.message, 'ok');
      dialog.close();
      if (onDone) await onDone();
    } catch (e) {
      statusLine.textContent = e.message;
      statusLine.style.color = 'var(--error)';
      copyBtn.disabled = false;
    }
  };

  fill(dialog.el.querySelector('.modal-foot'),
    h('button', { class: 'btn', onclick: () => dialog.close() }, '算了'),
    copyBtn,
  );

  document.body.append(dialog.el);
  input.focus();
  input.select();
  return dialog;
}

// ============================================================
// 直接写提示词 / 直接编辑
// ============================================================

/**
 * 不经 AI 生成，直接把用户写好的长文本保存为人格提示词。
 *
 * 这条路径用于用户已经有完整人设、或从别处导入一段 prompt 的场景。
 * 保存走 POST /api/personas 的 raw 模式：后端无损序列化、自动备份、原子写。
 */
export async function openPersonaDirectEditor(ctx, name = null, { onDone } = {}) {
  const editing = Boolean(name);
  let busy = false;
  let existingVars = {};

  const nameInput = h('input', {
    value: name || '',
    placeholder: 'xiaowan（目录名，支持中文）',
    disabled: editing,
  });
  const displayName = h('input', { placeholder: '小晚（人格自称）' });
  const gender = h('input', { placeholder: 'female / male / 其他（可选）' });
  const age = h('input', { type: 'number', min: '0', max: '1000', placeholder: '可选' });
  const adminQq = h('input', { placeholder: '管理员 QQ（可选）' });
  const adminName = h('input', { placeholder: '管理员称呼（可选）' });
  const prompt = h('textarea', {
    rows: 18,
    placeholder: '直接粘贴完整的人格提示词。可以是 Markdown、XML 或自由文本；'
      + '不需要按原来的 XML 标签格式。',
    style: { fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-small)' },
  });
  const counter = h('p', { class: 'field-help' }, '0 / 20000 字符');
  const statusLine = h('div', { class: 'field-help' }, '');

  const updateCounter = () => {
    const length = prompt.value.length;
    counter.textContent = `${length} / 20000 字符`;
    counter.style.color = length > 20000 ? 'var(--error)' : '';
  };
  prompt.addEventListener('input', updateCounter);

  const setStatus = (text, kind = '') => {
    statusLine.textContent = text;
    statusLine.style.color = kind === 'error' ? 'var(--error)'
      : kind === 'ok' ? 'var(--success)' : '';
  };

  const dialog = modal({
    title: editing ? `直接编辑 ${name}` : '直接写提示词',
    subtitle: '不经过 AI；原样保存为 PERSONA_PROMPT。保存前会自动备份旧版本。',
    wide: true,
    body: h('div', {},
      h('div', { class: 'grid grid-2' },
        field('人格目录名', nameInput,
          editing ? '编辑时不可改目录名；要改名请用「复制」' : '支持中文；不能含空格、斜杠或点'),
        field('显示名', displayName, '人格自称的名字；留空则用目录名'),
      ),
      h('div', { class: 'grid grid-2' },
        field('性别', gender, '可选，只影响 PERSONA_VARS'),
        field('年龄', age, '可选，0–1000'),
      ),
      h('div', { class: 'grid grid-2' },
        field('管理员 QQ', adminQq, '可选；用于管理员身份识别'),
        field('管理员称呼', adminName, '可选'),
      ),
      field('人格提示词', prompt,
        '写“你是谁、怎么说话、性格、边界、示例”即可；工具用法和分条规则由系统负责，不要写进人格。'
        + '长提示词会占用每轮上下文预算，建议控制在 3000–5000 字符。'),
      counter,
      statusLine,
    ),
    footer: [],
  });

  const cancelBtn = h('button', { class: 'btn', onclick: () => dialog.close() }, '算了');
  const saveBtn = h('button', { class: 'btn btn-primary' }, editing ? '保存覆盖' : '保存人格');
  fill(dialog.el.querySelector('.modal-foot'), cancelBtn, saveBtn);

  async function loadExisting() {
    if (!editing) {
      updateCounter();
      return;
    }
    try {
      const data = await endpoints.personaSource(name);
      prompt.value = data.xml || '';
      existingVars = data.vars || {};
      displayName.value = existingVars.name || '';
      gender.value = existingVars.gender || '';
      age.value = existingVars.age ?? '';
      const admin = Array.isArray(existingVars.admins) ? existingVars.admins[0] : null;
      if (admin) {
        adminQq.value = admin.qq ?? '';
        adminName.value = admin.name || '';
      }
      setStatus('已读取当前提示词。修改后点「保存覆盖」；旧版会自动备份。');
      updateCounter();
    } catch (e) {
      setStatus(e.message, 'error');
    }
  }

  async function save(overwrite = editing) {
    if (busy) return;
    const promptText = prompt.value;
    if (!nameInput.value.trim()) { setStatus('先填人格目录名', 'error'); return; }
    if (!promptText.trim()) { setStatus('提示词不能为空', 'error'); return; }
    if (promptText.length > 20000) {
      setStatus(`提示词太长（${promptText.length} 字符），上限 20000 字符`, 'error');
      return;
    }
    const admins = [];
    if (adminQq.value.trim()) {
      admins.push({
        qq: adminQq.value.trim(),
        name: adminName.value.trim(),
        role: 'owner',
      });
    }
    busy = true;
    saveBtn.disabled = true;
    setStatus('正在保存…');
    try {
      const result = await endpoints.savePersona({
        name: nameInput.value.trim(),
        xml: promptText,
        raw: true,
        overwrite,
        display_name: displayName.value.trim(),
        gender: gender.value.trim(),
        age: age.value.trim() ? Number(age.value) : null,
        admins,
      });
      ctx.toast(result.message, 'ok');
      if (result.backup) ctx.toast(`原人设已备份到 ${result.backup}`, 'ok');
      dialog.close();
      if (onDone) await onDone();
    } catch (e) {
      if (!overwrite && /已存在/.test(e.message)) {
        const confirmed = confirm(`${e.message}。\n\n要覆盖它吗？覆盖前会自动备份。`);
        if (confirmed) {
          busy = false;
          saveBtn.disabled = false;
          await save(true);
          return;
        }
      }
      setStatus(e.message, 'error');
      busy = false;
      saveBtn.disabled = false;
    }
  }

  saveBtn.onclick = () => save(editing);
  document.body.append(dialog.el);
  updateCounter();
  await loadExisting();
  prompt.focus();
  return dialog;
}
