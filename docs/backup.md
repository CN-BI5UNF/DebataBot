# 用户数据一键备份

设置 →「备份」提供一键备份按钮。备份在后台任务中执行，WebUI 显示扫描、写入、
校验阶段和进度；完成后可下载 zip。

## 备份范围

默认包含：

- `config.yaml`
- `secrets.enc`、`secrets.meta`、RSA 公钥，以及 RSA 私钥
  （优先从系统 keyring 导出；否则读取数据目录的 `rsa_private.pem`）
- `memory/`：聊天历史数据库
- `vector/`：RAG 向量数据库
- `workspace/`：用户文件与整个 `workspace/incoming` 入站目录
  （图片、视频、音频和普通入站文件；可用 `backup.include_media` 整体关闭）
- `emoji/`、`providers/presets/`
- 仓库根 `personas/`：人格定义
- `token_calibration.json`、`persona_brief.json`（存在时）

不包含：

- `logs/`、`cache/`、`tmp/`、`models/`
- `config_backups/`
- 维护任务/隔离区（`maintenance/`、`maintenance.sqlite3`）
- WebUI 登录令牌 `webui_token.txt`
- 更新状态 `update_state.json`
- 备份目录本身（避免递归）

## 安全

备份包含加密 API 密钥库 **和** RSA 私钥，两者结合可以解密模型密钥。请把 zip
当成密码保管：

- **ZIP 本身没有加密**；`0600`/`0700` 只是本机文件权限，不是密码保护。
- 压缩包和 sidecar 权限为 `0600`；新建备份目录尝试 `0700`。
- 下载接口走 WebUI 鉴权，响应禁止缓存，并拒绝路径穿越。
- 不要上传到公共网盘、公开仓库或聊天工具。
- 备份不包含明文密钥；manifest 只记录路径、大小和 SHA-256。
- `credential_restorable` 只表示归档内密钥材料存在，且私钥与公钥可解析/匹配；
  它不会在线解密 `secrets.enc`，所以不是“API key 已完整验证可解密”的证明。

## 配置

设置 →「备份」：

- `backup.directory`：备份目录；留空使用 `~/Karen_Backups`。相对路径按用户
  主目录解析。已存在的目录必须属于当前用户，且不能对组/其他用户开放
  （例如 `0755` 会被拒绝，请先 `chmod 700`）；不会擅自修改已有目录权限。
- `backup.include_media`：是否包含 `workspace/incoming` 的入站媒体；默认包含。
  关闭后聊天数据库和文字仍会备份。

## 并发、取消与持久 job

- 同一数据目录只有一个实例级 `BackupCoordinator`。改变备份目录、
  `include_media` 或重建 Runtime 都不会绕过互斥；POSIX 上另有
  `<DATA_DIR>/.backup.lock` 文件锁防止第二个进程并发备份。
- job 元数据（状态/阶段/统计/错误，不含正文或密钥）持久化到
  `<DATA_DIR>/backup_jobs.json`（`0600`）。进程重启后发现未完成 job 会标为
  `interrupted`，不会伪装成 `running` 或 `done`。
- 页面“取消备份”调用 `POST /api/backup/jobs/{id}/cancel`。取消是合作式的：
  扫描、文件块写入、SQLite backup 进度、校验和发布边界都会检查；未完成包
  不会出现在可下载列表。取消请求后状态先为 `cancelling`，收尾后变
  `cancelled`。
- 外层 asyncio Task 被取消时，backup worker 线程不会被 `to_thread` 自动
  停止；协调器会保留 barrier/flock 和 active job，交给 finalizer 等 worker
  结算后再持久化终态、释放资源。已过发布提交点则返回已发布包
  （`cancel_too_late`），不假装零产物。
- 终态 job 在持有实例锁时持久化，之后才解锁；避免旧 owner 覆盖新 job。
  strict 终态持久化失败时保持锁定并进入需人工恢复状态。
- 发布前会重新校验临时 ZIP，并 `fsync` 文件与目录，再做原子 rename。
- 不自动删除旧备份；保留策略/清理需要另行授权，避免磁盘紧张时误删最后
  可用包。

## 离线验证与恢复演练

本功能仍不提供 WebUI 在线一键恢复。可以在隔离目录中做只读验证：

```bash
# 结构/CRC/成员 hash/SQLite quick_check/密钥配对
.venv/bin/python -m core.restore verify \
  ~/Karen_Backups/Karen_Backup_xxx.zip --json

# 解压到全新目录并复验（不会碰生产 DATA_DIR）
.venv/bin/python -m core.restore drill \
  ~/Karen_Backups/Karen_Backup_xxx.zip \
  --destination /tmp/karen-drill-1 \
  --protected-root ~/.local/share/KarenBot/instances/default \
  --json
```

设置页每个已有备份也有“离线验证”按钮（只读验证，不解压到生产目录）。
`verify_backup`/`drill_backup` 会拒绝绝对路径、`..`、反斜杠、符号链接、
重复成员、异常压缩比和超限成员；解压使用 `O_EXCL`，拒绝覆盖非空目录，不执行
包内脚本。`drill` 默认使用系统临时目录并在验证后清理，`--keep` 可保留。

## 恢复（手动）

建议先运行上面的离线验证/drill，再在隔离环境或另一台设备验证。

1. 停止 `karenbot` 服务。
2. 解压 zip。
3. 把 `data/` 内容复制回当前实例数据目录（默认
   `~/.local/share/KarenBot/instances/default/`）。
4. 把 `personas/` 复制回项目根目录的 `personas/`。
5. 确认 `data/rsa_private.pem` 权限为 `0600`。如果原环境使用系统 keyring，
   可把 PEM 留在数据目录作为兜底；确认密钥可解密后再决定是否删除。
6. 重启服务，检查人格、聊天、模型密钥和 RAG。

注意：

- 恢复是整文件覆盖，可能覆盖备份之后的新消息；需要合并新数据时应使用专门的
  迁移流程，不要直接覆盖生产目录。
- D01 之后的恢复必须先比较快照与当前源：
  `core.d01.plan_restore_replay(snapshot_db, source_db)` 返回 `exact`、
  `replay_tombstones` 或 `blocked`。只有前两种可继续；有新消息/墓碑回退/
  epoch 分叉时必须合并或人工处理。`apply_tombstone_replay` 只重放新增墓碑
  和删除操作，不改写 raw JSON，并把受影响 summary 标为 stale、要求重建。
  该函数目前是受控 core API，**尚未**接入一键在线恢复按钮。
- 已发送的 QQ 消息、已执行工具动作不会被“恢复”撤销或重放。
- 数据 schema 升级后回退源码不等于回退数据；代码回退前要确认旧代码能理解
  当前数据。

## 备份类型与状态

- WebUI 按钮创建 `backup_type=user_export`；`migration_snapshot` 只能由可信
  维护控制器/`SnapshotBarrier` 通过 core API 触发，不能从普通按钮冒充。旧包
  显示为 `legacy_user_export`。
- manifest 记录 `format_version`、`backup_type`、`validation_status`、
  `partial`、`missing`，以及能力：
  - `chat_restorable`
  - `media_complete`
  - `credential_restorable`
  - `migration_safe`
  - `cross_plane_consistent`
  - `ledger_aware` / `ledger_complete`（仅严格快照）
- 严格快照会对 `data/memory/*` 的每个 SQLite 快照做**语义**账本探测：
  D01 schema、`M001` 迁移标记、per-persona 水位、tombstone/outbox 指纹和
  迁移后不变量；manifest 的 `ledger` 块绑定这些证据的 SHA-256。路径名含
  `ledger/tombstone/deletion` 只记为 `ledger_artifact_present`，不能证明
  `migration_safe`。
- `migration_safe=true` 需要同时满足：可信屏障已冻结、所有 SQLite 一致、
  ledger 证据完整，且维护屏障的 `contract_epoch`/`migration_set_id` 与
  `compatibility_state.json` 一致（当前 D01 为 epoch 1 / `M001`）。缺任何
  一项都保持 false 并给出 `migration_blocked_reason`。
- job 状态：`queued/running/done/partial/failed`。SQLite raw-copy 兜底、
  文件在备份期间变化、必需项缺失或密钥材料无法验证时为 `partial`，不是
  “完整成功”。页面轮询会正确停在 `partial`，不会无限刷新。
- `done` 仍不等于 `migration_safe`；生产迁移还需要真实 writer 冻结、D01
  迁移、隔离恢复演练和用户授权。

## ZIP 校验

写完 ZIP 后会重新打开验证：

- `testzip()` 校验 CRC；
- 成员集合与 manifest 一致，拒绝重复/缺失/多余成员；
- 逐成员 size/SHA-256 与 manifest 一致；
- SQLite 成员检查文件头；SQLite 快照写入前还会执行 `PRAGMA quick_check`。

验证失败时临时包不会发布为可下载完成包。sidecar 只是索引缓存；sidecar 缺失
或损坏时列表会从包内 `manifest.json` 重建，单个坏包不会让整个列表 500。

## 一致性

- SQLite 数据库通过 `sqlite3.Connection.backup()` 生成一致性快照，不直接
  复制 WAL 状态；`mode=ro` 禁止因源文件消失创建空库。
- 如果某个数据库无法快照，会退回文件复制并在 `manifest.json` 中标记
  `consistent=false`，job 变为 `partial`。
- 多个 SQLite 库、配置文件和媒体是**分别**处理的；本功能不承诺跨库/跨文件的
  全局原子快照。备份期间继续写非 SQLite 文件可能产生混合时间点。
- `user_export` 不是迁移快照。严格 `migration_snapshot` 额外包含
  `maintenance.sqlite3`、`maintenance/` 和 `compatibility_state.json`，并对
  D01 账本做语义证据校验。即便如此，它也不替代真实维护控制器、D01 迁移和
  生产恢复授权；未部署 launcher、未获生产迁移授权时 `migration_safe` 不会
  成为放行标志。

## 空间

启动前会估算原始数据、SQLite 临时快照和 64 MiB 余量；空间不足时任务失败，
显示所需/可用空间，不会先写一半再假装成功。旧备份不会自动删除。

备份目录不能是数据目录、人格目录或工作区的**父目录**（否则扫描会把源目录整体
排除）；放在数据目录内部是允许的，备份目录自身会被排除，但更推荐独立目录或
外置存储，避免和原数据一起损坏。

## API

- `POST /api/backup`：启动任务，返回 job。
- `GET /api/backup/status`：目录、配置、最近 job 和备份文件。
- `GET /api/backup/jobs/{id}`：轮询任务。
- `POST /api/backup/jobs/{id}/cancel`：请求合作式取消。
- `POST /api/backup/verify`：对指定已存在包做只读验证（`deep_sqlite` 默认
  开启），返回 members/hash/SQLite/密钥报告，禁止缓存。
- `GET /api/backup/files/{name}`：下载 zip（鉴权、no-store）。
