// C 固定大小存储的一次性管理员初始化。
//
// Linux：密码只放进本次请求的 JSON body，后端立即通过 stdin 交给 sudo -S；
// 前端不写浏览器持久存储、不记录日志、失败也不回显密码。
// Windows：后端弹出 UAC，前端不需要也不应该收集 sudo 密码。

import { h, field, modal } from './dom.js';
import { endpoints } from './api.js';

function storageSummary(storage) {
  const rows = [];
  if (storage.mode) rows.push(['模式', storage.mode === 'image_fixed' ? 'C 固定大小' : 'B 命名卷']);
  if (storage.size_mb) rows.push(['大小', `${storage.size_mb} MiB`]);
  if (storage.mountpoint) rows.push(['挂载点', storage.mountpoint]);
  if (storage.image_path) rows.push(['镜像文件', storage.image_path]);
  return h('table', { class: 'table' },
    h('tbody', {}, ...rows.map(([k, v]) => h('tr', {},
      h('td', {}, k), h('td', {}, String(v))))));
}

export function openStorageSetup(ctx, payload = {}) {
  const storage = payload.storage || {};
  const admin = payload.admin || {};
  const method = admin.method || 'manual';

  let passwordInput = null;
  const statusLine = h('p', {
    class: 'field-help',
    style: { whiteSpace: 'pre-wrap' },
  });

  const parts = [
    h('p', {}, admin.note || 'C 模式需要一次性管理员初始化；应用不会静默降级到 B。'),
    storageSummary(storage),
  ];

  if (method === 'sudo_password') {
    passwordInput = h('input', {
      type: 'password',
      autocomplete: 'current-password',
      spellcheck: 'false',
      placeholder: '当前用户的 sudo 密码',
    });
    parts.push(field('sudo 密码', passwordInput,
      '只用于本次 sudo -S，不保存、不写日志、不出现在命令行参数中。'));
    if (globalThis.location?.protocol !== 'https:') {
      parts.push(h('p', { class: 'field-help' },
        '当前 WebUI 不是 HTTPS。若通过局域网访问，密码在传输中可能被监听；'
        + '建议在本机浏览器操作，或使用 HTTPS 反向代理。'));
    }
  } else if (method === 'uac') {
    parts.push(h('p', {},
      '点击“初始化存储”后，Windows 会弹出 UAC 管理员确认；这里不需要 sudo 密码。'
      + '如果远程会话看不到桌面，请改用 B 模式，或以管理员 PowerShell 手动运行安装脚本。'));
  } else {
    parts.push(h('p', {},
      '未找到可用的 sudo / UAC 提权方式。请复制下面的安装命令，'
      + '以管理员身份手动执行，然后回到本页刷新。'));
  }
  if (!admin.available && payload.setup?.command) {
    parts.push(h('pre', { style: { whiteSpace: 'pre-wrap' } },
      payload.setup.command));
  }
  parts.push(statusLine);

  const cancelBtn = h('button', { class: 'btn' }, '取消');
  const submitBtn = h('button', { class: 'btn btn-primary' }, '初始化存储');
  submitBtn.disabled = !admin.available;
  if (!admin.available) submitBtn.title = '当前没有可用的提权方式';

  const dialog = modal({
    title: '初始化 C 固定大小存储',
    subtitle: '一次性操作；只创建/挂载固定的 workspace 文件系统，不删除已有数据。',
    body: h('div', {}, ...parts),
    footer: [cancelBtn, submitBtn],
    onClose: () => {
      if (passwordInput) passwordInput.value = '';
    },
  });
  cancelBtn.onclick = () => dialog.close();

  submitBtn.onclick = async () => {
    if (!admin.available) return;
    const password = passwordInput ? passwordInput.value : undefined;
    if (method === 'sudo_password' && !password) {
      statusLine.textContent = '请输入 sudo 密码。';
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = method === 'uac' ? '等待 UAC 确认…' : '正在初始化…';
    statusLine.textContent = method === 'uac'
      ? '已请求管理员权限；请在弹出的 UAC 窗口确认。'
      : '正在通过 sudo 执行固定安装脚本…';
    try {
      const result = await endpoints.containerStorageSetup(
        password === undefined ? {} : { password },
      );
      if (passwordInput) passwordInput.value = '';
      dialog.close();
      ctx.toast(
        result.already_ready ? '存储已经就绪' : 'C 存储已初始化',
        'ok',
      );
      if (typeof payload.onDone === 'function') await payload.onDone(result);
      return result;
    } catch (e) {
      if (passwordInput) passwordInput.value = '';
      const tail = e?.detail?.details?.output_tail;
      statusLine.textContent = `${e.message}${tail ? `\n${tail}` : ''}`;
      submitBtn.disabled = false;
      submitBtn.textContent = '重试';
    }
  };

  document.body.append(dialog.el);
  if (passwordInput) passwordInput.focus();
  return { dialog, payload };
}

/** 读取当前存储状态；C 未就绪时打开初始化弹窗。 */
export async function promptStorageSetup(ctx, { force = false, onDone = null } = {}) {
  let data;
  try {
    data = await endpoints.containerStorage();
  } catch (e) {
    if (force) ctx.toast(`读取容器存储状态失败：${e.message}`, 'error');
    return false;
  }
  const storage = data.storage || {};
  const admin = data.admin || {};
  if (storage.mode !== 'image_fixed' || storage.ready) {
    if (force) {
      ctx.toast(storage.ready ? 'C 存储已经就绪' : '当前是 B 命名卷模式，不需要 C 初始化',
        storage.ready ? 'ok' : 'warn');
    }
    return false;
  }
  if (storage.code === 'container_storage_size_mismatch') {
    ctx.toast(
      `C 存储实际为 ${storage.marker_size_mb ?? '?'} MiB，与当前配置不一致；`
      + '请到「容器」页点“采用实际大小”',
      'warn',
    );
    return false;
  }
  if (!admin.available) {
    ctx.toast('未找到 sudo/PowerShell；请按容器页显示的安装命令手动执行', 'warn');
    return false;
  }
  openStorageSetup(ctx, { ...data, onDone });
  return true;
}
