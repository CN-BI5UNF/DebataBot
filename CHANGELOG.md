# Changelog

本文件记录 Debata 所有重要变更，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased] — 大肥鱼人格 / 联网知识 / 表情包工具 / 依赖自动安装

### Added

- 内置人格「大肥鱼」：DeepSeek 蓝色鲸鱼娘社区二创，成为默认内置人格；另内置 `deepseek` 完整群聊提示词，旧 `debata` 已移除。
- LLM 表情包工具：`list_emoji`、`save_emoji_from_url`、`save_emoji_from_workspace`、`rename_emoji`、`delete_emoji`。网络下载复用视觉链路的 SSRF/重定向/大小限制，写入复用同一套文件名、符号链接和 magic 校验。
- 联网考究知识库：`core/knowledge.py` + `data/knowledge.json`；LLM 可把 `web_search` 结果整理成短笔记，或调用 `research_web_to_prompt` 让后台小模型搜索、提炼、保存。只有 active 条目能被 `list_knowledge_notes` 按需查询，不再自动注入 `task_context`。
- 后台热梗考究：按 `features.knowledge.research_interval_hours` 定时用 LLM 上网整理当前热梗；`research_search_provider=auto` 时官方搜索可用则优先 DeepSeek Search，否则回退百度。支持 `DEBATA_DISABLE_TREND_RESEARCH=1` 临时关闭。
- WebUI「知识」页与 `/api/knowledge` 管理 API：搜索、筛选、手动添加、编辑、归档、删除、手动考究、获取当前热梗。
- 知识跨人格共享：每条知识/热梗/笔记可勾选 `shared`，其他角色可只读查询并参与查重，所属人格保留编辑/删除权。
- `web_fetch` 工具：按 DeepSeek 官方 Fetch API 读取单个网页的 Markdown 正文；只接受公开 http(s) URL，拒绝账号密码/本机/内网字面量，结果截断后交给模型。
- DeepSeek Search 适配官方文档：query 截到 128 字符、count 限制 1–50、支持 `ori_question`，并优先把 600–800 字的 `summary` 交给模型（而不是短 `snippet`），同时保留站点/日期/相关度/权威度提示。

### Changed

- 「知识」和「黑话」改为按需知识库：新增 `lookup_slang`，`list_knowledge_notes` 只返回 active 条目并受查询上限约束；主对话不再每轮把词表/知识注入 `task_context`。这样模型需要时才查，动态上下文更小、更稳定，减少 KV cache 命中率被知识量拖累的问题。配置键 `inject_max` / `max_prompt_chars` 保留原名，含义改为“单次查询返回上限 / 字符上限”。
- 知识保存前查重：同类型/跨类型同标题、以及去掉“基本介绍/基本资料/简介/盘点”等泛化词后主题相同的条目（如“Phigros基本介绍”与“Phigros基本资料”）不再重复写入；联网考究会把现有标题+短摘要加入考究 prompt，并在落盘前再次校验。新增 `POST /api/knowledge/dedupe`，默认 dry-run，确认后合并保守重复并生成 `knowledge.json.dedupe-*` 备份。
- 知识条目新增 `shared` 共享标记：所属人格可勾选共享，其他角色只读查询并参与查重，不能跨人格编辑/删除。
- 内置人格调整：移除旧版 `debata`，将 `deepseek` 加入内置提示词并纳入仓库；人格页/导航/设置文案统一从“角色”改为“人格”。
- 界面文案调整：知识页“刷新当前热梗”→“获取当前热梗”；黑话页标题改为“群聊黑话”；存储“安全清理”按钮改为“清除”。
- 官方搜索、独立视觉 `describe_image`、OpenAI 兼容 embedding 调用的 token 现在都写入同一个 V2 usage store；knowledge/slang 的辅助调用补充 provider/model/agent 归属。总览页新增「按用途（V2 口径）」分项表。
- 知识考究更激进：默认 5 条查询（知乎/B站/流行语/抖音/微博）、单次最多 8 条笔记、查询返回上限 10 条/2000 字符；prompt 允许在搜索结果不足时用低置信度补全，不再轻易返回空数组。
- 更新器在目标版本修改 `pyproject.toml` / `uv.lock` / `requirements*.txt` 时，默认用当前解释器执行 `pip install -e .`；解释器没有 pip 时回退到 `uv pip install --python <sys.executable> -e .`。安装失败会回滚源码到更新前提交。可在设置里关闭 `update.auto_install_dependencies`。
- 备份包含 `knowledge.json` 与 `knowledge_state.json`。
- 默认 `persona.active`、首次配置默认人格和 `BUILTIN_PERSONAS` 更新为「大肥鱼」。

### Fixed

- 黑话库内存索引未按 `max_entries` 截断，长期运行会无限增长。
- 黑话学习在模型调用期间收到新消息时，可能把未发送的新消息一起从窗口删除；改为按对象身份移除已发送消息。
- 「黑话」「知识」页的“设置”按钮只跳到设置页，但设置页硬编码 `await show('models')`，永远打开模型分页；现在通过 `ctx.section` 打开「功能」分页。另修复 `location.hash` 触发的 hashchange 重入：深链渲染期间会被一次无 `section` 的重复导航覆盖回默认分页，现在用导航序号挡住自身重入。
- 非 systemd 的 fallback 模式下「重启后端」返回 400；现在由独立 `restart-helper` 等待旧进程退出、按当前启动参数拉起新进程，并保留日志。
- 重启模式的 systemd 判定只看 `INVOCATION_ID`，从 VNC/桌面会话启动时会把外层服务的环境变量误当成自己受 systemd 管理，实际执行 `systemctl --user restart debata-agent.service` 静默失败（这正是「重启后端没反应」的根因）；现在按当前进程 cgroup 是否包含 `debata-agent.service` 判定，并区分用户/系统 unit。更新后的自动重启也复用同一套 systemd/fallback 逻辑。
- `features.knowledge.research_queries` 已加入设置声明表：设置 →「功能」可用多行文本编辑 5 条热梗搜索词，保存后重启 Runtime 生效；`list_str` 字段改为 textarea，避免长搜索词/启动参数挤在单行输入框里。
- 「重启后端」的等待逻辑只看第一次 HTTP 200，旧进程优雅退出期间仍会应答，页面可能在旧代码上提前 reload。现在后端在 `/api/auth/state` 和重启响应中返回每次进程启动生成的 `boot_id`，前端必须等到 `boot_id` 变化（或旧版本无 boot_id 时至少观察到一次断连）才认为重启完成。
- 页面/设置分页的慢请求返回后会覆盖用户已经切换到的页面：导航改为每次使用新的 `#content` 节点，设置分页使用独立 body 节点；被取代的页面会调用 `destroy()` 释放定时器/订阅。同时修复在页面加载期间点回上一页被“重复点击”短路的问题。
- 登录或首次配置完成后 `enterApp()` 失败会把三个顶层容器全部隐藏，留下空白页；现在失败时恢复登录/向导界面并显示错误。
- 手动压缩活跃历史沿用 15 秒默认请求超时，摘要模型稍慢就会前端报超时但后端仍在跑；现在给 `/api/context/compact` 180 秒超时。
- 清空数字设置会发送空字符串，后端 `int("")`/`float("")` 抛 ValueError 返回 500；前端现在校验数字并发送 Number，后端非法数字统一返回 400。
- 知识页把 `last_trend_run_at` 的 Unix 秒当成毫秒，显示成 1970 年；现在按秒解析。
- 更新页仍写着“依赖文件变化拒绝自动应用”，与实际自动安装/回滚行为不一致；文档和 UI 文案已同步。
- 官方搜索失败回退成功时，知识考究可能把带“已回退”前缀的结果误判为错误；现在只把单行括号错误当成失败。

### Verification

- 全量测试 2364 passed（Linux/aarch64、Python 3.12，忽略 `tests/test_kv_cache_real.py`）；`ruff check .`、`git diff --check`、`compileall`、bun browser bundle 全部通过。本轮只验证了本地 Linux/aarch64 + Python 3.12，未声称覆盖远端 CI 的 Ubuntu/Windows × 3.11/3.12 矩阵。
- 知识/黑话改为按需查询：`task_context` 不再包含 `【群聊黑话表】` / `<network_knowledge>`；`lookup_slang` 和 `list_knowledge_notes` 返回 active/confirmed 条目、受查询上限约束，并带不可信内容提示。
- 线上 compact profile 构建 53 个工具，`lookup_slang` / `list_knowledge_notes` 直接暴露（非 stub），写入类知识工具仍为 stub；浏览器验证知识页和设置页已显示“按需查询/查询返回上限”文案。
- 知识查重实测：`deepseek` persona 从 79 条合并到 71 条，8 组保守重复被合并，“Phigros 基本介绍 / Phigros 基本资料”只保留一条；再次 dry-run 返回 0 条可合并，清理前已生成 `knowledge.json.dedupe-20261028-001451` 备份。
- 新增查重/去重测试：跨类型近义标题、同标题相同内容不重写、泛化标题不同主题不误合并、手动添加拒绝重复、research 跳过重复并返回数量、dedupe dry-run/合并/备份。
- 跨人格共享测试：共享条目其他人格可查询、非共享不可见、所属人格可写、其他人格修改/删除被拒绝、共享条目参与保存查重。
- 内置人格与文案测试：`BUILTIN_PERSONAS` 为大肥鱼/deepseek、debata 已移除；侧栏/人格页/设置/向导统一“人格”；知识“获取当前热梗”、黑话“群聊黑话”、存储“清除”按钮。
- 真实 Baidu + DeepSeek 整理写入 13 条热梗笔记；随后用官方通道连续跑 5 条查询，45 秒内 5/5 成功、保存 40 条笔记，`/api/knowledge` 总计 49 条。
- DeepSeek 官方 Search/Fetch 用真实 API 实测：Search 返回带 `summary`/权威度的结果，Fetch 返回 Markdown 正文。
- V2 按用途统计实测出现 `knowledge_research`（18 次、40319 tokens）和 `embedding`（86 次、13412 tokens）记录。
- Playwright 真实浏览器验证：黑话/知识页“设置”按钮均打开设置「功能」分页；热梗考究搜索词为 5 行 textarea；知识页上次考究时间显示为 2026 年而非 1970；慢请求期间切换设置分页不会被旧分页覆盖。
- Playwright 完整点击“更新 → 重启后端 → 立即重启后端”：57.1 秒后页面自动 reload 并显示登录门，`/api/auth/state` 的 `boot_id` 已改变；旧进程退出、新进程 PID 312004、WebUI 200、Runtime running、NapCat 重连，且只有一个 `main.py` 进程。
- `uv pip install --dry-run --python .venv/bin/python -e .` 解析 76 个包成功。
- 后端已重启加载本轮代码；活跃人格为用户当前的「大肥鱼」，可在人格页切换到大肥鱼或 deepseek。

## [snapshot-20261025] — 开发快照：备份/兼容/账本与视觉设置 UI

开发快照，不发布正式版本号。详见 `releases/snapshot-20261025.md`。

### Added

- 视觉设置 UI：原生看图与独立 `describe_image` 分成独立卡片；独立视觉关闭时折叠，开启后复用已有 provider，只有自定义端点/高级覆盖才要求 base_url 和 API Key。
- `PATCH /api/settings/vision`：一次校验、一次落盘，自定义端点密钥写入 `vision_key`。
- 严格迁移快照、离线 verify/drill、bootstrap permit、受信 launcher、build identity、维护控制器和兼容门禁。
- 隔离 D01 v1→v2（M001）稳定账本、ledger-aware 快照、恢复重放计划和墓碑重放。

### Changed

- 默认分支统一为 `main`；`refactor/main` 作为应用内更新跟踪分支；README/README_EN、贡献指南、CI 和文档链接同步。
- 更新器 git 超时处理跨平台：Windows 清理进程树，POSIX-only 测试在 Windows 跳过。
- Windows 无 `flock` 时备份/维护使用进程内实例锁；修复 Windows 下 SQLite 测试文件占用和 git 子进程输出解码。
- 视觉字段不再由通用设置表单平铺渲染，避免把“视觉 provider / base_url”误认为主模型看图的前置条件。

### Verification

- 本地 Linux/aarch64、Python 3.12 全量测试 2182 passed；ruff、diff check、node check 通过。
- 视觉设置 DOM smoke test 通过；远端 CI 在 Ubuntu / Windows × Python 3.11 / 3.12 上运行。

### Known limitations

- 开发快照，未安装 launcher、未改 systemd、未重启服务；未执行生产迁移/恢复。
- M00 真实 writer、权威 inventory、R01/P01/S01–S03 等仍未完成。

## [Unreleased] — 2.6 L00/B00/M00/B02-E0/D01：统一门禁与账本基础

### Added
- `tests/test_compat_parity.py`：core 与 stdlib launcher 对同一合成状态做逐库/epoch/phase/feature/first-run golden 差分；统一 `supported_read/write` 为上界，统一 `data_contract_epoch`。
- core/launcher 真首次运行与恢复区分：无 config 但已有 DB/state 返回 `recovery_required`；空实例返回 `setup_required`，launcher 需显式 `--allow-setup`。
- launcher 绑定 project root/data dir/config/入口；只允许 project root 内 Python + `main.py`，校验命令 `--config`，exec 前固定 `DEBATA_DATA_ROOT`。
- `core/maintenance.py`：持久维护状态、POSIX flock、writer 注册/结算、`ControllerBarrier`、startup gate；RuntimeHost 的 start/restart 同时检查维护和当前代码兼容 gate。
- `core/bootstrap.py`：epoch0 资格检查、bootstrap_epoch0 严格快照、verify/drill、绑定源 inventory/目标 build/manifest/允许迁移 ID/期限的一次性 permit；不造空 ledger。
- `core/d01.py`：隔离 `debata.db` v1->v2（M001）迁移，稳定 UUID/seq/life_epoch、import ledger、origin/copy/summary/outbox/tombstone/checkpoint schema，同库 append/tombstone 事务；生产路径默认拒绝。
- WebUI RuntimeHost 启动门禁：维护 phase 或当前数据版本不兼容时进入 maintenance，不允许从 WebUI 绕过 launcher。

### Changed
- 备份 worker 生命周期：外层 Task.cancel 不再提前释放 barrier/flock；等待 worker 结算后持锁持久化终态再解锁；发布提交点后取消返回已发布包，不假装无产物。
- build identity 使用源码目录白名单、lstat/no-follow/root 校验，不扫描/读取数据、密钥、personas 或外部 symlink；新增 `identity_kind=startup_source_snapshot` 语义说明。

### Known limitations
- M00 writer registry 尚未接入全部 Runtime writer；旁路 SQL/文件写入仍可能绕过。
- D01 只在 tmp 合成库验证，未在生产迁移；`DEBATA_DB_SCHEMA_VERSION` 仍为 1。
- bootstrap permit 未接入生产迁移工具；未安装 launcher/systemd，未重启。
- R01/P01/S01–S03 及资源/删除/分页/RAG/配置安全仍未开始；不声称 2.6 完成。

## [Unreleased] — 2.5 W00/B01/B02/U01-L：构建身份、备份协调与独立启动

### Added
- `core/build_identity.py` 与 `GET /api/system/build`：进程启动时捕获 loaded build identity，与当前磁盘 source identity 分开；源码指纹不含 `data/`、聊天正文、密钥或日志。
- `BackupCoordinator`：按数据目录共享实例级互斥和 job 注册表；POSIX `flock` 跨进程租约；job 元数据持久化到 `backup_jobs.json`（0600），重启后非终态标 `interrupted`，另一进程持锁时不覆盖其权威状态。
- 备份合作式取消：`cancelling/cancelled` 状态、API `POST /api/backup/jobs/{id}/cancel`、页面取消按钮；扫描/块写入/SQLite 进度/校验/发布边界响应；未完成临时包不发布。
- 备份发布 fsync 文件与目录、原子 rename；临时目录带 owner/job/boot 标记，只在本实例持锁时清理。
- `core/restore.py`：离线 `verify` 和隔离 `drill`，校验 CRC/成员集合/路径/hash/SQLite quick_check/密钥配对，拒绝穿越、符号链接、重复成员、异常压缩比和超限包；不解压到生产目录、不执行包内脚本。
- 备份 API 新增 `POST /api/backup/verify`；设置页已有包新增“离线验证”按钮。
- 严格迁移快照框架：`SnapshotBarrier` 协议 + `backup_type=migration_snapshot`，额外包含 maintenance 库/目录与 compatibility state；缺 barrier/必需项/一致性/密钥验证时失败，不 raw-copy 兜底。当前 epoch 无 deletion ledger 时仍写 `migration_safe=false`。
- 独立受信 launcher `deploy/trusted_launcher.py`（仅标准库、不 import 项目代码）和 systemd 样例；失败退出 78、不启动目标。代码与合成测试已准备，未安装、未改 unit。

### Changed
- 备份目录已存在时检查 owner/权限；组或其他用户可访问的目录 fail-closed（不擅自 chmod 用户目录）。
- 备份 manifest 升级为 `format_version=2`，新增 `backup_type=user_export`、`validation_status`、`partial`、`missing` 和能力字段：`chat_restorable`、`media_complete`、`credential_restorable`、`migration_safe=false`、`cross_plane_consistent=false`。
- ZIP 关闭后重新打开校验 CRC、成员集合、逐成员 size/SHA-256 和 SQLite 成员头；SQLite 快照写入前执行 `quick_check`。验证失败不发布为可下载完成包。
- SQLite raw-copy 兜底、文件备份期间变化、必需项缺失或密钥材料无法验证时，job 标为 `partial`，不再一律 `done`。
- sidecar 只是索引缓存；缺失/损坏时列表从包内 `manifest.json` 重建；created_at 统一为 epoch 数字，避免 ISO 字符串/浮点混排 TypeError。
- 备份 UI 明确 ZIP 未加密、验证状态、密钥材料可验证性和部分完成；`backup.include_media` 文案明确覆盖整个 `workspace/incoming`。
- `core/compat.py` 改为只读 URI/query_only 探针；逐库登记 DB 版本/schema_version，逐项评估，不能用同平面 max 掩盖旧库或新增异版本库。
- `compatibility_state.json` 增加 state_format/phase/generation；planes 不能升降实测版本（projection 除外）；迁移中/需恢复状态阻止普通更新。
- `reasoning:null` 恢复为 no-change，不因字段出现把 reasoning 关闭；六档/reset 契约保留。

### Known limitations
- 普通一键备份仍是 `user_export`；`credential_restorable` 只验证私钥与公钥匹配，不证明 `secrets.enc` 可解密。
- 备份仍无包级加密、无在线一键恢复；严格迁移快照只有框架/合成屏障，真实维护控制器、D01 deletion ledger 和生产恢复演练未完成。
- 独立 launcher 未安装、未修改 systemd、未重启；当前 live 进程未证明加载本轮代码。
- 备份保留策略未实现；默认不自动删除旧包。
- D01/R01/P01/S01–S03/A01–A02/X01–X02/Q01–Q02/C12-R 及后续合同仍未完成；当前不上传/不发布。

## [Unreleased] — 2.4 P0：兼容门禁、CI 与六档 PATCH

### Added
- 新增仓库根 `compatibility.json`（manifest_format=1）与 `core/compat.py`：纯数据解析、本地 schema/epoch/feature 快照、fail-closed 评估，不执行目标代码。
- 更新器 `check`/`apply` 接入兼容门禁：legacy 无 manifest、migration_required、unsupported_data_contract、feature_missing、data_state_changed 都会阻止自动 apply；计划绑定 manifest hash 与数据快照。
- Release 列表返回每个版本的兼容结果；WebUI 显示“兼容性未通过”并禁用自动应用按钮。
- 新增 `docs/compatibility.md`，说明 manifest、数据快照、apply 重验和受信 launcher 边界。
- `.github/workflows/test.yml` 的 push/PR 显式加入 `refactor/main`，并支持 `workflow_dispatch`；默认分支不变。

### Changed
- Agent PATCH 的思考强度 DTO 从三档扩展为 `minimal/low/medium/high/xhigh/max`；旧客户端省略/`null`=不改，新增 `reasoning_budget_reset=true` 显式恢复供应商 auto；关闭 reasoning 不清空已选档位。
- Release 自动绿标只提示可向前推进且通过兼容门禁的版本；分叉/回退仍只允许在版本列表手动选择。

## [Unreleased] — 用户数据一键备份

### Added
- 设置 →「备份」新增一键备份：配置、加密密钥库、RSA 私钥、聊天数据库、RAG 数据库、工作区、表情包、自定义 provider 预设和人格定义。
- 备份在后台 job 中执行，显示扫描/写入/校验阶段、字节进度、警告和失败原因；完成后可在页面下载 zip。
- SQLite 数据库使用 `sqlite3.Connection.backup()` 生成一致性快照；失败时才退回文件复制并在 manifest 标记 `consistent=false`。
- 新增 `backup.directory`（默认 `~/Debata_Backups`）和 `backup.include_media`（默认包含入站媒体）。
- 备份包含 manifest（路径/大小/SHA-256/警告）和 `README_RESTORE.txt`；zip 与 sidecar 权限 0600，下载接口走鉴权且禁止缓存。
- 新增 `POST /api/backup`、`GET /api/backup/status`、`GET /api/backup/jobs/{id}`、`GET /api/backup/files/{name}`。
- 新增 `docs/backup.md`，说明备份范围、敏感信息、空间检查和手动恢复步骤。

### Known limitations
- 只提供备份，不提供在线一键恢复；恢复需停止服务并手动复制，避免覆盖备份后的新数据。
- 日志、缓存、模型文件、配置历史、维护任务/隔离区和 WebUI 登录令牌不在备份范围内。
- 备份包含可以解密 API 密钥的材料，必须像密码一样保管；不会上传到任何第三方。
- 备份是某一时刻的尽力快照；非 SQLite 文件在备份期间被修改时可能不是严格同一时间点。

## [Unreleased] — Releases 版本管理、更新与回退

### Added
- 新增私有发布仓库 `CN-BI5UNF/DebataBot`；版本以 GitHub Releases/tag 为目录，每个 tag 对应 `releases/<tag>.md` 更新日志。
- 设置 →「更新」新增版本列表：可展开/收起每个版本的更新日志，选择更新、回退或切换版本；应用前确认，旧 HEAD 备份到 `refs/backups/update/*`。
- 更新器改为按 Release/tag 解析目标提交，支持任意历史版本切换；依赖文件变化、脏工作树、数据目录位于代码树内仍会拒绝。
- 后台每 9 天检查一次 Releases（可在设置中调整），状态持久化到数据目录 `update_state.json`（0600）；失败后 6 小时重试。
- 总览页发现新版本时显示不显眼的绿色圆形上箭头；没有新版本或已关闭提醒时不显示。
- 从总览绿标进入后关闭窗口会询问“之后是否不再提示更新”；勾选“之后不再提示”并点“是”后不再显示绿标，可在设置 → 更新重新开启。
- 更新镜像新增常用预设（GitHub 官方、gh-proxy.com、ghfast.top、ghproxy.net）和自定义镜像输入；继续拒绝 http、账号密码、内网地址与非 DebataBot 仓库。
- WebUI 右下角新增浅灰色版本占位，目前显示“开发版”，不发布正式版本号。
- 新增 `/api/update/version`、`/api/update/available`、`/api/update/releases`、`/api/update/preferences`；`/api/update/check` 支持 `target_tag`，`/api/update/apply` 支持 `confirm_rollback`。
- 修复前端更新检查/应用请求被 15 秒默认超时提前 abort 的问题（现在使用 330 秒，与后端 300 秒上限匹配）。

### Changed
- 更新源从 `CN-BI5UNF/Debata-Refactor` 切换为 `CN-BI5UNF/DebataBot`；发布仓库目前私有，后续可公开。
- 版本目录读取远端 tag，不依赖 GitHub API；私有仓库通过本机 git 凭据认证。
- 自动检查与 HTTP 请求解耦，只读缓存状态；手动检查会重置下一次到期时间。

### Fixed
- 总览绿标只提示可向前推进的 Release；分叉版本只在版本列表中手动选择，避免自动引导覆盖本地提交。
- 本地稳定分支已经指向目标版本、但工作树在另一条分支时，仍可切回目标版本。
- 设置 →「更新」不再被版本列表的网络请求阻塞；列表在后台加载。
- 前端更新检查/应用请求使用 330 秒超时，避免 15 秒默认超时提前 abort。
- 更新日志折叠同时写内联 `max-height`，即使浏览器缓存了旧 `app.css`，展开/收起功能仍然生效。
- 静态前端改为 `Cache-Control: no-cache, must-revalidate`，并给入口资源加版本查询串；避免无构建 WebUI 升级后继续使用旧 CSS/JS。

### Known limitations
- 本次未实现 C03–C14（cold catch-up、摘要 CAS、工具循环 checkpoint、删除/恢复、聊天分页、RAG 有界、配置事务、迁移回滚等）。
- 更新日志来自仓库内 `releases/<tag>.md`，不从 GitHub Release body 动态读取；发布时需保持两者同步。
- 回退只切换源码，不自动安装依赖、不执行数据库迁移；依赖变化仍会拒绝自动应用。
- 私有仓库的镜像加速通常不可用，需使用官方直连与本机 git 凭据。

## [Unreleased] — 在线更新、思考档位与 C02 压缩状态机

### Added
- 新增设置 →「更新」：固定检查 `CN-BI5UNF/Debata-Refactor` 的 `refactor/main`，检查/确认后 fast-forward、创建 `refs/backups/update/*`、写 `update_history.jsonl` 并重启服务。
- 更新镜像支持完整 https 仓库 URL、`{url}` 占位或 https 前缀；拒绝 http、账号密码、本机/.local/内网 IP 和指向其它仓库的地址。
- 更新器拒绝脏工作树、数据目录位于代码树内、目标分叉/变化和依赖文件变化；只更新源码，不读取或覆盖用户数据目录。
- 新增 C02 压缩状态机（`core/compaction.py`）：增长 EWMA/冷启动预测、成功冷却、最少新增 token、失败指数退避与进程内 single-flight。
- `/api/context/status` 新增 `compaction` 状态；新增 `POST /api/context/compact` 手动压缩和 Token 预算页按钮。
- 新增 `behavior.summarize` 配置：`background_enabled`、`forecast_enabled`、`cold_growth_tokens`、`cooldown_seconds`、`min_new_tokens`。
- 模型预设新增思考档位与图像元数据：DeepSeek `low/high/max` + 每图 384 token + 不发送 `detail`；OpenAI 按模型提供 `low/medium/high/xhigh` 或 `low/medium/high`；Anthropic 增加 `xhigh/max` 预算档；OpenAI/OpenRouter 保留 `detail`，其它预设保守省略。
- 自定义模型（不在预设模型清单内）思考档位固定回退 `low/medium/high`；设置页切换提供商/模型会即时刷新档位。
- 修复首次配置向导“选择人格”等条件控件点击不重绘的问题（注入 `ctx.rerender()`）。

### Changed
- Anthropic thinking budget 会按 `max_tokens` 收敛到 `max_tokens - 1024`；`max_tokens <= 1024` 时本次不启用思考，避免必然 400。
- DeepSeek 图像 auto 预算使用预设的每图 384 token，而不是全局猜测值。

### Known limitations
- C02 状态随进程重启重置；C03 冷启动追赶、C04 摘要 CAS 原子提交、C05 工具循环内局部压缩仍待实现。
- 自动更新只做 fast-forward，不自动安装依赖、不运行迁移；依赖文件变化会拒绝应用。
- 预设思考档位依据 2026-10 官方文档；中转站/自定义模型是否实际支持仍取决于上游。

## [Unreleased] — 第二阶段 M0–M2：统一预算与硬 guard

### Added
- 新增 `core/model_binding.py`：解析 EffectiveModelBinding 与统一 `BudgetSnapshot`；主模型/摘要模型的窗口、输出上限和安全余量进入同一套计算。
- 主调用预检改为使用 `B = W_effective - O - S`；窗口不合法时返回 `invalid_context_budget`，不再发送已知超限请求。
- AgentRunner 在每轮工具调用前和最终 `tools=None` 收尾前重新估算完整 messages+tools；已知超限返回 `budget_blocked`，provider 调用次数为 0。
- 子 Agent 任务与摘要 Agent 接入预算 guard；摘要使用自己的输入预算，不套主模型窗口。
- 设置页 Agent 卡片新增“思考强度” low/medium/high，读取/保存 `agents.*.reasoning.budget`。
- DeepSeek/OpenAI 预设新增 `reasoning_effort_style`，在开启思考时把档位映射为 `reasoning_effort`；未声明的自定义网关不发送该字段。
- 首次配置向导改用 `setup.js` 注入的 `ctx.rerender()`，修复路径/人格来源/思考开关/TTS/嵌入/白名单等控件点击不更新选中态和条件字段的问题。

### Changed
- 工作历史预算改用统一净输入预算 B，扣除安全余量；`max(1, ...)` 不再伪造可用历史空间。
- `/api/context/status` 与运行时硬 guard 使用同一快照，并返回 binding 信息。
- 主回复/单次回合在 `reply_lock` 内再次核对当前预算，防止锁外预检后配置下调。

### Known limitations
- C02 提前压缩状态机、C03 cold catch-up、C04 摘要原子提交、C05 工具循环持久化/局部压缩、C06–C14 仍未实现。
- 当前硬 guard 只阻止本地已知超限，不做工具循环内自动压缩；等回复锁内的最终重建仍以当前估算为准。
- 只有 DeepSeek/OpenAI 预设确认映射 reasoning_effort；其他供应商保存档位但不保证实际生效。
- 主 provider 401 未修复，真实模型自动回复和压缩质量仍无法验收。

## [Unreleased] — 存储与缓存维护切片

### Added
- 新增 history 分页读 API：`length` / `fetch_suffix` / `fetch_recent` / `iter_records` / `stats` / `clear_page_cache`，主 prompt 与摘要 compaction 只读活跃后缀；全量 `load()`/`records()` 保留为兼容入口。
- 新增有界 history 页缓存（默认 32 MiB / 16 页）和进程内权威计数，避免每次活跃窗口读取都全表 JSON 扫描。
- 新增实例级 `maintenance.sqlite3`：持久化清理预览/任务，只保存运维元数据，不保存聊天正文。
- 新增 `/api/storage/*` 存储盘点与安全清理 API：preview → execute → job，支持确认令牌、盘点代次、幂等键与取消。
- 新增 `/api/context/status` 只读预算快照：有效窗口、输出预留、安全余量、净输入预算与阈值。
- 新增 WebUI「存储与缓存」页面，以及设置页 Token 预算分组的当前预算快照。
- 新增 `storage.retention.inbound_media_hours`（默认 48 小时）：`workspace/incoming` 下的入站图片/视频超过 TTL 后进入可预览清理类别；音频和普通文件仍受保护。
- 新增 `docs/storage_maintenance.md`。

### Changed
- RAG 历史 bootstrap 改为分页流式入队，索引队列上限 256 批；启动不再通过 `history.records()` 全量加载历史。
- `TokenBudget.total_input_budget` 在窗口小于输出预留时返回 0，不再用 `max(1024, ...)` 伪造输入空间。
- `behavior.context` 新增安全余量字段；`behavior.summarize` 新增紧急线百分比；`storage.maintenance` 新增受管缓存/临时目录策略。

### Fixed
- 修正 `tests/test_provider_error_redaction.py` 导入未声明 `httpx2` 导致全量测试无法收集的问题（当前锁定 anthropic 0.116 使用 httpx）。

### Known limitations
- 自动磁盘 GC 定时器、聊天记录按范围删除、资源引用/租约、隔离区恢复、预算策略 v2 运行时切换与工具循环统一 guard 尚未实现。
- 入站图片/视频的 TTL 清理不做历史引用检查；删除后历史文字仍在，但原媒体字节不可用，模型需要时必须要求重传。音频和普通入站文件不在此策略内。

## [Unreleased] — token 效率重构

### Added
- 新增非破坏性“模型视图”（`core/model_view.py`）：只裁剪发给模型的历史视图，历史/归档/事件库原样保留。默认只保留最近 2 个 `task_context`、2 条发送回执、2 条发送完成快照和 4 组 `no_action`。
- 新增按 provider/model 持久化的 token 校准（`core/token_calibration.py` + 实例目录 `token_calibration.json`），重启后不再退回 1.0。
- 新增 `scripts/benchmark_prompt_tokens.py`：只读对比重构前后的活跃历史、系统提示、工具 schema 与估算 prompt。
- 新增工具 schema `compact` 档位：核心字段直出，高级字段通过 `tool_search detail=full` 查看；默认 11 个 compact + 其余 stub。
- 新增发送成功后自动收尾（`behavior.auto_finish_after_send`），省掉一次 `no_action` 收尾模型调用。
- 用量面板新增“未命中输入 token / 估算误差 / 估算样本”。
- 新增 [后台运行指南](docs/background_running.md)：systemd 用户/系统级服务、tmux、screen、nohup、升级与排错。
- 新增 `scripts/install_systemd_user.sh` 一键安装脚本和 `scripts/systemd/` 单元模板。
- 新增简化服务命令：`debata start` / `stop` / `restart` / `status` / `log` / `enable` / `disable`，底层自动调用 systemd；首次 `debata start` 会自动安装服务。

### Changed
- README 中英文重写：补充 Token 效率、后台运行、数据目录、测试与安全说明；clone 地址更新为本项目仓库。
- 更新 getting_started / features / architecture / kv_cache_benchmark，加入模型视图、compact 工具、自动收尾与 2026-10 基准数据。
- 滚动摘要触发/目标默认从 75%/50%/30% 调整为 50%/30%/20%，并按“模型可见视图 + 固定开销”口径计算；128k 工作预算需要在设置页或实例配置中显式设置。
- KV 诊断默认改为 `summary` 轻量模式，不再每轮对完整消息与工具集做多次全量 JSON 序列化。
- 工具 schema 移除通用 `finish_after_success` 参数；执行层仍兼容旧调用。
- 发送类工具结果不再回显已发送正文，只保留 msg_id/order/状态等定位信息。
- token 估算改用规范 JSON（messages + tools）口径，并为小文本与工具 schema 增加缓存。

### Fixed
- 修正 `str(tools_schema)` 造成的工具 schema token 估算偏差。
- 修正 `cached_tokens > prompt_tokens` 等脏数据导致缓存命中率超过 100% 的问题。
- 避免旧运行时快照触发压缩、以及总结 Agent 重复读取旧快照的问题。
- 修正 `debata start/stop/restart` 中 systemctl 参数顺序错误（服务名必须在命令之后）。
- 修正端口被占用时启动崩溃的问题：现在会提示“可能已在运行”，并给出 `debata status/log` 与换端口命令。
- 修正 WebUI “对话”页加载超时：归档/事件改为一次性快速读取，会话分组去掉 O(n²) 扫描，合并结果增加缓存，大群对话先截取尾部窗口再归一化，前端请求超时提升到 60 秒。
- 修正真实数据下“对话”页大群（8,889 条记录）无法正常显示的问题。
- 修正没有 systemd 用户会话（容器 / WSL / 精简系统）时 `debata` 命令全部失败的问题：现在自动回退到 PID 文件 + 独立后台进程，`start/stop/restart/status/log` 仍可用；`enable` 会启动后台进程并说明开机自启的限制。
- 修正安装脚本在 `systemctl --user` 无法连接 bus 时直接报错退出的问题：现在检测用户 bus，跳过 unit 安装并启动内置后台模式。

### Measured
- 真实实例活跃历史：185,173 → 43,248 估算 token（-76.6%）。
- 工具 schema：full 档位 9,817 → compact 档位 4,740 token。
- 估算单次主 prompt：206,768 → 59,766（-71.1%）。
- WebUI 对话页：归档加载 15.5s → 1.9s；分组 183s → 1.1s；大群展示首次 5.27s → 0.74s（隔离基准）。

## [v0.9.0-alpha] — 2026-07-04

### Added
- PersonaAgent 新增对话后更新、周期维护、睡眠开始、进食开始、睡醒/进食结束结算和启动离线对账链路，让人格状态可以围绕真实运行事件持续推进。
- PersonaContextView / PersonaEpisodeBuffer 落地，向 PersonaAgent 提供事件、聊天现场、当前状态、对象画像、画像审计、短期影响、线索、待办和相关长期记忆组成的结构化上下文。
- episode buffer 增加明确边界：会话切换、闲置超时、进入或离开吃饭/睡觉/崩溃动作、片段数上限和字符数上限都会打开新 episode，避免上下文无限追加。
- 人格更新审计扩展到状态、短期影响、待办、线索、用户画像和关系更新，记录触发来源、会话、用户、前后快照和变更摘要。
- mind SQLite schema 升级到 version 2，补齐 persona_update_audits、人格状态、状态日志、用户画像、短期影响、待办、线索、睡眠记录、进食记录、轨迹和长期重要记忆等存储。
- 睡醒/进食结束时支持模型恢复评估；模型失败或离线对账场景使用公式兜底，并在日志中记录 recovery_source。
- 周期维护会清理过期短期项、线索和待办，过期待办会标记为 missed，并触发日常状态整理。
- 待办、线索和短期影响支持基于 ID 的创建、更新、关闭/删除；未知 ID 的更新会被审计为丢弃，避免错误写入。
- 用户画像和关系更新接入 PersonaAgent，支持好感、摘要、特征、互动次数和最近互动时间的持续维护。
- 人格后台页新增运行概况、实时状态、背景动向、状态日志、短期影响、待办/线索、用户画像、睡眠/进食和整理轨迹查看。
- 设置页新增人格管理开关、当前人格年龄覆盖、精力/饱腹模式、兜底恢复/衰减参数，以及 Persona / Social / Subconscious Agent 的后台配置入口。

### Changed
- 项目命名与数据存储从 Diana 收敛为 Debata，运行时数据库改为 `memory/<persona>/<persona>.db`。
- 人格上下文从零散状态文本扩展为结构化上下文视图，聊天现场改为有边界 episode，不再依赖无限追加的片段。
- 年龄相关描述以年龄档位、年龄提示和当前人格年龄覆盖为准，不再表达为自动年龄增长。
- 睡眠和进食恢复从“动作开始即同步恢复”调整为动作结束或离线结算时评估，并保留公式兜底来源。

### Fixed
- 本地 RAG 缺少 `sentence-transformers` 时改为启动后台依赖安装并提示重启生效，避免启动流程直接失效。
- GUI 模式启用 qasync timer 多路复用，并收敛 wakeup、NapCat 和 proactive 后台循环的重复启动风险，降低 Windows 原生 timer/句柄耗尽概率。
- 修复 PersonaAgent 相关 JSON 输出兼容性问题：复数字段稳定按数组解析，同时兼容 profile、relationship、effect、todo、cue 等单条旧字段。
- 修复短期影响、待办、线索更新时缺少 ID、引用未知 ID、空内容和重复待办导致的数据漂移问题。
- 修复待办状态兼容性问题，统一 complete / done / cancelled / deleted / missed 等状态归一和关闭判断。
- 修复进食记录缺少 record_id 的兼容迁移问题，为旧记录补齐可更新 ID。
- 收敛画像、关系、短期项、审计记录与结构化上下文之间的数据一致性问题。

## [v0.8.5-alpha] — 2026-06-11

### Added
- Linux/CLI 启动配置完善，补齐无头环境启动、路径与密钥处理链路
- 上下文压缩与工作窗口机制落地，提升长对话稳定性和上下文预算控制
- 聊天归档与实时聊天能力增强，私聊和群聊均可进行本地归档总结
- QQ/NapCat 适配与发送工具扩展，覆盖消息发送、回执处理和平台侧历史摘要
- RAG、长期记忆、工具预算与仪表盘能力整合，支持记忆注入、预算观测和图形化管理

### Changed
- 发布状态整理为 v0.8.5-alpha
- 启动配置、上下文管理、聊天工具、记忆系统和仪表盘相关变更按当前发布面重新归类

### Fixed
- 修正 Linux/CLI、NapCat 连接、发送回执、上下文压缩和仪表盘启动链路中的近期问题

## [v0.2.0-alpha] — 2026-05-24

### Added
- 289 项集成测试（pytest + pytest-asyncio）
- KV 缓存命中率实测验证
- NapCat 心跳/重连配置项（`ping_interval_seconds` 等）

### Changed
- `WhitelistConfig.mode` 值 `"all"` 改为 `"open"`
- `ProtocolType` 收窄为 `Literal["openai_compat", "anthropic"]`

### Removed
- V1 兼容代码全部清理（migrate / detect_legacy / `TOOL_USE_PROTOCOL` 别名等 11 处）

## [v0.1.0-alpha] — 2026-05-24

### Added
- V2 全栈重构：Core / Providers / Agents / Adapters / Tools / Memory / Features / AppConfig 七大模块
- NapCat OneBot V11 适配器（WebSocket 直连）
- 多 LLM 提供商：OpenAI 兼容协议 + Anthropic 原生协议
- 工具系统（消息/记忆/平台/控制/功能），Pydantic schema 自动派生 OpenAI tool 格式
- AES-256-GCM + RSA-2048 + keyring 三级加密配置管理
- 消息管线：合并窗口 + 中断检测 + 工具循环 + 总结
- 主动思考定时循环（proactive_loop）
- 人格系统（persona prompt + persona_gen_agent）
- 滑动窗口速率限制 + 好友/群申请验证流程
