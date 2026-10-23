# Changelog

本文件记录 Debata 所有重要变更，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

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
