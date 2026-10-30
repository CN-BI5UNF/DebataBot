// 受控 Docker 容器状态页。配置在 设置 → 容器；本页只做诊断与轻量测试。

import { h, fill, card, empty, table } from '../dom.js';
import { endpoints } from '../api.js';
import { promptStorageSetup } from '../container_setup.js';

function formatBytes(value) {
  const num = Math.max(0, Number(value) || 0);
  if (num >= 1024 ** 3) return `${(num / 1024 ** 3).toFixed(2)} GiB`;
  if (num >= 1024 ** 2) return `${(num / 1024 ** 2).toFixed(1)} MiB`;
  if (num >= 1024) return `${(num / 1024).toFixed(1)} KiB`;
  return `${num} B`;
}

function badge(text, tone = '') {
  return h('span', { class: `badge ${tone}`.trim() }, text);
}

export async function render(container, ctx) {
  let status = null;
  let storageSetup = null;
  let lastError = null;
  let destroyed = false;

  const refreshButton = h('button', { class: 'btn', onclick: () => refresh() }, '刷新');
  const testButton = h('button', {
    class: 'btn btn-primary',
    onclick: () => runTest(),
  }, '一键准备 / 启动');
  const resetButton = h('button', {
    class: 'btn btn-danger',
    onclick: () => resetContainer(),
  }, '重建容器');
  const setupButton = h('button', {
    class: 'btn btn-primary',
    onclick: () => promptStorageSetup(ctx, { force: true, onDone: refresh }),
  }, '初始化 C 存储');
  setupButton.style.display = 'none';
  const configButton = h('button', {
    class: 'btn',
    onclick: () => ctx.navigate('settings', { section: 'container' }),
  }, '打开容器设置');
  const actionHost = h('div', { class: 'row' },
    refreshButton, testButton, setupButton, resetButton, configButton);
  const summaryHost = h('div', { class: 'grid grid-4' });
  const problemHost = h('div', {});
  const detailHost = h('div', {});
  const storageHost = h('div', {});
  const page = h('div', { class: 'page' },
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '受控容器'),
      h('p', { class: 'page-desc' },
        'Docker 容器只提供资源/文件隔离，不是对抗恶意代码的绝对安全边界。'
        + '默认无网络、无特权、非 root、只挂载专用 workspace。'
        + '首次使用直接点「一键准备 / 启动」；遇到问题按下方提示修复。')),
    actionHost, summaryHost, problemHost, detailHost, storageHost);
  fill(container, page);

  async function refresh() {
    refreshButton.disabled = true;
    try {
      status = await endpoints.containerStatus();
      try {
        storageSetup = await endpoints.containerStorage();
      } catch {
        storageSetup = null;
      }
      if (!destroyed) renderAll();
    } catch (e) {
      if (!destroyed) {
        fill(container, card('受控容器', '',
          empty(`读取容器状态失败：${e.message}`)));
      }
    } finally {
      refreshButton.disabled = false;
    }
  }

  function storageMismatch() {
    const storage = status?.status?.storage || {};
    return storage.code === 'container_storage_size_mismatch' && storage.marker_size_mb;
  }

  function renderAll() {
    const info = status?.status || {};
    const docker = info.docker || {};
    const storage = info.storage || {};
    const box = info.container || {};
    const admin = storageSetup?.admin || {};
    const mismatch = storageMismatch();
    setupButton.style.display = (
      storage.mode === 'image_fixed'
      && !storage.ready
      && !mismatch
      && admin.available
    ) ? '' : 'none';
    fill(summaryHost,
      metric('功能', info.enabled ? '已启用' : '未启用',
        info.enabled ? 'ok' : ''),
      metric('就绪', info.ready ? '可用' : '未就绪',
        info.ready ? 'ok' : 'warn'),
      metric('Docker', docker.available ? '可用' : '不可用',
        docker.available ? 'ok' : 'bad'),
      metric('存储', storage.mode === 'image_fixed' ? 'C 固定大小' : 'B 软配额',
        storage.ready ? 'ok' : 'bad'));

    fill(problemHost, renderProblems(info, storage, docker, mismatch));
    const limits = info.limits || {};
    fill(detailHost, card('运行诊断', '',
      table(
        ['项目', '值'],
        [
          ['Docker Server', docker.server_version || docker.error || '—'],
          ['镜像', `${info.image || '—'} ${info.image_present ? '（已存在）' : '（缺失）'}`],
          ['容器名', info.container_name || '—'],
          ['容器状态', box.state || '—'],
          ['内存 / CPU', `${limits.memory_mb ?? '—'} MiB / ${limits.cpus ?? '—'}`],
          ['pids / 单文件', `${limits.pids_limit ?? '—'} / ${limits.file_size_mb ?? '—'} MiB`],
          ['网络', limits.network || '—'],
          ['exec 超时', `${limits.exec_timeout_seconds ?? '—'} 秒`],
          ['存储就绪', storage.ready ? '是' : (storage.error || '否')],
          ['存储大小', storage.mode === 'image_fixed'
            ? `${storage.size_mb ?? '—'} MiB`
            : `${storage.size_mb ?? '—'} MiB 软配额`],
          ['镜像实际大小', storage.marker_size_mb == null
            ? '—' : `${storage.marker_size_mb} MiB`],
          ['剩余空间', storage.free_bytes == null ? '—' : formatBytes(storage.free_bytes)],
        ].map(([label, value]) => h('tr', {},
          h('td', {}, label), h('td', {}, String(value)))),
      )));

    const rows = [];
    if (storage.mountpoint) rows.push(['挂载点', storage.mountpoint]);
    if (storage.image_path) rows.push(['镜像文件', storage.image_path]);
    if (admin.method) {
      rows.push([
        '初始化方式',
        admin.method === 'uac'
          ? 'Windows UAC 管理员确认'
          : (admin.method === 'sudo_password' ? 'sudo 密码（仅本次）' : '手动命令'),
      ]);
    }
    if (storageSetup?.setup?.command) {
      rows.push(['安装命令', storageSetup.setup.command]);
    }
    fill(storageHost, card('存储与安装',
      admin.note || storageSetup?.setup?.note
        || 'B 模式使用 Docker 命名卷；C 模式需要一次性管理员初始化。',
      rows.length
        ? table(['项目', '值'], rows.map(([label, value]) => h('tr', {},
          h('td', {}, label), h('td', {}, String(value)))))
        : empty('暂无存储路径信息')));
  }

  function renderProblems(info, storage, docker, mismatch) {
    const buttons = [];
    let message = '';
    if (mismatch) {
      message = storage.error
        || `存储镜像实际为 ${storage.marker_size_mb} MiB，与当前配置不一致。`;
      buttons.push(h('button', {
        class: 'btn btn-primary',
        onclick: () => adoptStorageSize(Number(storage.marker_size_mb)),
      }, `采用 ${storage.marker_size_mb} MiB`));
      buttons.push(h('button', {
        class: 'btn',
        onclick: () => ctx.navigate('settings', { section: 'container' }),
      }, '修改存储设置'));
      buttons.push(h('button', {
        class: 'btn',
        onclick: () => restartRuntime(),
      }, '重启 Runtime'));
    } else if (!docker.available) {
      message = docker.error || 'Docker 不可用；请先安装并启动 Docker Engine / Docker Desktop。';
    } else if (!info.image_present) {
      message = `镜像 ${info.image || ''} 缺失。点「一键准备 / 启动」会自动拉取；`
        + '如果 Docker Hub 不通，可在设置→容器填写「Docker 镜像加速前缀」。';
      buttons.push(h('button', {
        class: 'btn',
        onclick: () => ctx.navigate('settings', { section: 'container' }),
      }, '打开容器设置'));
    } else if (storage.mode === 'image_fixed' && !storage.ready) {
      message = storage.error || 'C 固定大小存储尚未初始化。';
      if (!mismatch) {
        buttons.push(h('button', {
          class: 'btn btn-primary',
          onclick: () => promptStorageSetup(ctx, { force: true, onDone: refresh }),
        }, '初始化 C 存储'));
      }
    } else if (lastError) {
      const detail = lastError?.detail?.details || {};
      message = lastError.message || '容器准备失败';
      if (detail.output_tail) message += `\n${detail.output_tail}`;
      if (!info.image_present) {
        buttons.push(h('button', {
          class: 'btn',
          onclick: () => ctx.navigate('settings', { section: 'container' }),
        }, '配置镜像加速'));
      }
    }
    if (!message) return null;
    const body = [
      h('p', { style: { whiteSpace: 'pre-wrap' } }, message),
      buttons.length ? h('div', { class: 'row row-wrap' }, ...buttons) : null,
    ];
    return card('需要处理', '先修复下面这一项，再点「一键准备 / 启动」。', ...body);
  }

  async function restartRuntime() {
    if (!globalThis.confirm('重启 Runtime？正在运行的请求会被中断，通常需要几秒到一分钟。')) {
      return;
    }
    try {
      await endpoints.restart();
      ctx.toast('已请求重启 Runtime，请稍候刷新', 'ok');
      setTimeout(() => refresh(), 5000);
    } catch (e) {
      ctx.toast(`重启失败：${e.message}`, 'error');
    }
  }

  async function adoptStorageSize(size) {
    if (!Number.isFinite(size) || size <= 0) return;
    if (!globalThis.confirm(
      `把存储大小改为 ${size} MiB？\n\n` +
      '这是已有镜像的实际大小；修改后需要重启 Runtime 才能生效。'
    )) return;
    try {
      await endpoints.patchSetting('features.container.workspace_size_mb', size);
      lastError = null;
      ctx.toast(`已采用 ${size} MiB；请重启 Runtime 生效`, 'ok');
      await refresh();
    } catch (e) {
      ctx.toast(`修改存储大小失败：${e.message}`, 'error');
    }
  }

  async function runTest() {
    testButton.disabled = true;
    testButton.textContent = '准备中…';
    lastError = null;
    ctx.toast('正在准备容器；首次可能需要下载镜像，请稍候', '');
    try {
      const result = await endpoints.containerTest();
      if (!result.ok) {
        lastError = { message: result.result?.error || '容器测试未通过', detail: {} };
      }
      ctx.toast(result.ok ? '容器已启动并通过测试' : '容器测试未通过', result.ok ? 'ok' : 'error');
      await refresh();
    } catch (e) {
      lastError = e;
      await refresh();
      ctx.toast(`容器准备失败：${e.message}`, 'error');
    } finally {
      testButton.disabled = false;
      testButton.textContent = '一键准备 / 启动';
    }
  }

  async function resetContainer() {
    if (!globalThis.confirm('只重建容器，不删除持久 workspace。确定继续？')) return;
    resetButton.disabled = true;
    try {
      await endpoints.containerReset();
      ctx.toast('容器已重建', 'ok');
      await refresh();
    } catch (e) {
      ctx.toast(`重建失败：${e.message}`, 'error');
    } finally {
      resetButton.disabled = false;
    }
  }

  await refresh();
  return { destroy: () => { destroyed = true; } };
}

function metric(label, value, tone) {
  return h('div', { class: 'card' },
    h('div', { class: 'field-label' }, label),
    h('div', { class: 'stat-value' }, badge(value, tone)));
}
