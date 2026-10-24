# Releases 版本管理

当前开发阶段暂不发布正式版本号。版本使用日期快照 tag，例如
`snapshot-20261023`；GitHub Releases 与 tag 使用同一个名字。

## 发布仓库

- 仓库：`CN-BI5UNF/DebataBot`（当前私有，后续可公开）。
- 更新目标分支：`refactor/main`。
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

# 2) 打 tag 并推送（DebataBot 是发布仓库 remote）
git tag -a snapshot-20261024 -m "开发快照 2026-10-24"
git push bot refactor/main
git push bot snapshot-20261024

# 3) 创建 GitHub Release，body 与仓库内日志一致
gh release create snapshot-20261024 \
  --repo CN-BI5UNF/DebataBot \
  --title "开发快照 2026-10-24" \
  --notes-file releases/snapshot-20261024.md
```

如果还没有 `bot` remote：

```bash
git remote add bot https://github.com/CN-BI5UNF/DebataBot.git
```

## 兼容声明

每个发布 tag 必须包含根目录 `compatibility.json`（与 `releases/<tag>.md` 一起提交）。
更新器只读取该纯 JSON，不执行目标代码；`check`/`apply` 会比较本地 config/DB
版本、data contract epoch、required features 和 manifest hash。没有 manifest 的
旧 tag 可以展示，但不能自动应用。详见 `docs/compatibility.md`。

## App 侧行为

- 设置 →「更新」列出远端 tag（按 tag 时间倒序），可展开/收起日志。
- 选择任意版本：先检查脏工作树、依赖文件变化和数据目录位置，再创建
  `refs/backups/update/*` 备份并执行版本切换；回退/切换需要额外确认。
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
- 只接受指向 `CN-BI5UNF/DebataBot` 的地址。
- 第三方镜像能看到代码请求；私有仓库通常无法通过公共镜像拉取，
  建议使用官方直连并配置本机 git 凭据（例如已登录的 `gh`）。

## 回退边界

回退只移动 `refactor/main` 到目标 tag 的提交，不会：

- 回退数据库 schema 或执行迁移；
- 自动安装依赖（依赖文件变化会拒绝自动应用）；
- 重放或撤销已发送的 QQ 消息、工具调用；
- 覆盖用户数据目录。

更早的 schema 若与当前数据不兼容，应先备份数据并按迁移文档处理，
不要只依赖代码回退。
