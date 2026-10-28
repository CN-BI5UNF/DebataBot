# Releases 版本管理

当前开发阶段暂不发布正式版本号。版本使用日期快照 tag，例如
`snapshot-20261025`；GitHub Releases 与 tag 使用同一个名字。

## 发布仓库

- 仓库：`CN-BI5UNF/KarenBot`（公开）。
- 默认分支：`main`。
- 更新目标分支：`refactor/main`（与 `main` 同步）。
- 用户数据目录不在代码仓库内；更新只切换源码。

## 一个版本需要什么

1. `releases/<tag>.md`：该版本的更新日志。App 会从 tag 对应的提交里读取
   这个文件，设置页的版本列表和总览绿标都使用它。
2. 一个指向提交的 tag（建议 annotated tag）。
3. 一个 GitHub Release，body 使用同一份 `releases/<tag>.md`。

发布示例（在干净、已通过测试的工作树上）：

```bash
# 1) 写更新日志并提交
$EDITOR releases/snapshot-20261024.md
git add releases/snapshot-20261024.md
git commit -m "docs: release snapshot-20261024"

# 2) 打 tag 并推送（KarenBot 是发布仓库 remote）
git tag -a snapshot-20261024 -m "开发快照 2026-10-24"
git push bot main refactor/main
git push bot snapshot-20261024

# 3) 创建 GitHub Release，body 与仓库内日志一致
gh release create snapshot-20261024 \
  --repo CN-BI5UNF/KarenBot \
  --title "开发快照 2026-10-24" \
  --notes-file releases/snapshot-20261024.md
```

如果还没有 `bot` remote：

```bash
git remote add bot https://github.com/CN-BI5UNF/KarenBot.git
```

## 兼容声明

每个发布 tag 必须包含根目录 `compatibility.json`（与 `releases/<tag>.md` 一起提交）。
更新器只读取该纯 JSON，不执行目标代码；`check`/`apply` 会比较本地 config/DB
版本、data contract epoch、required features 和 manifest hash。没有 manifest 的
旧 tag 可以展示，但不能自动应用。详见 `docs/compatibility.md`。

## App 侧行为

-「重启 Runtime」只重建运行时，不会重新导入 Python 代码；应用更新后需要
  「重启后端」才会加载新版本。
- 应用更新后，页面会询问是否立即重启后端；选择“稍后”时，可在
  设置 → 更新 中再次点击「重启后端」。
- 后端重启期间 WebUI 将无法连接（实测约 30–60 秒），会话 Cookie 会失效，
  恢复后可能需要重新输入访问令牌。
- systemd 用户服务、系统服务和内置后台模式都支持整进程重启：systemd 模式按
  当前进程 cgroup 确认 unit 后执行 `systemctl restart`；后台模式由独立
  `restart-helper` 等待旧进程退出后按原启动参数拉起新进程，不依赖 systemd。

- 设置 →「更新」列出远端 tag（按 tag 时间倒序），可展开/收起日志。
- 选择任意版本：先检查脏工作树、依赖文件变化和数据目录位置，再创建
  `refs/backups/update/*` 备份并执行版本切换；回退/切换需要额外确认。
  前向更新遇到依赖文件变化时，会用当前解释器执行 `pip install -e .`
  （无 pip 时回退 `uv pip install`），安装失败会把源码回滚到更新前提交。
- 自动检查默认每 9 天一次，配置项是 `update.check_interval_days`。
  检查失败后 6 小时重试；成功后排到 9 天后。
- 状态保存在实例数据目录的 `update_state.json`（权限 0600），不保存聊天正文。
- 总览绿标只读缓存状态，不会因为打开总览页而触发网络请求。
- 从绿标进入版本窗口后关闭，会询问“之后是否不再提示”；勾选并点“是”
  写入 `suppress_badge`，设置页“接收更新提醒”可重新开启。

## 镜像

设置页提供 GitHub 官方、gh-proxy.com、ghfast.top、ghproxy.net 预设，
也可以填写自定义 https 镜像。镜像只用于 git fetch：

- 拒绝 http、账号密码、localhost/.local、内网/回环 IP、查询参数和 fragment。
- 只接受指向 `CN-BI5UNF/KarenBot` 的地址。
- 第三方镜像能看到代码请求；公开仓库可以用公共镜像，若仓库转为私有，
  公共镜像通常无法拉取，建议改用官方直连并配置本机 git 凭据（例如已登录的 `gh`）。

## 回退边界

回退只移动 `refactor/main` 到目标 tag 的提交，不会：

- 回退数据库 schema 或执行迁移；
- 自动安装依赖（回退本身不安装；前向更新遇到依赖变化会安装，失败回滚）；
- 重放或撤销已发送的 QQ 消息、工具调用；
- 覆盖用户数据目录。

更早的 schema 若与当前数据不兼容，应先备份数据并按迁移文档处理，不要只依赖
代码回退。更新器会在应用前读取目标 tag 的 `compatibility.json`，检查数据版本、
契约 epoch、迁移集和必需能力；没有 manifest 的旧 tag 只能展示，不能自动应用。
