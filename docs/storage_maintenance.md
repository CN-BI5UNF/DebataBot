# 存储盘点与安全缓存清理

本轮落地的是方案 2.1 的**存储与安全清理切片**：历史分页读、有界热缓存、
存储占用盘点、可重建缓存清理的 preview → execute → job 闭环，以及统一上下文
预算快照与发送前硬 guard。

## 安全边界

可以清理：

- 进程内聊天页合并缓存、历史分页缓存、图片编码缓存、token 估算缓存、
  工具展示缓存。
- `<DATA_DIR>/tmp` 下超过 `storage.maintenance.temp_hours` 的普通文件。
- `<DATA_DIR>/cache` 下超过 `storage.maintenance.derived_cache_days` 的普通文件。
- `workspace/incoming` 下超过 `storage.retention.inbound_media_hours`
  （默认 48 小时）的图片和视频文件。

不会清理：

- 原始聊天记录、event_log、archive、滚动摘要与长期记忆。
- `workspace/incoming` 下的音频、普通文件及其他非图片/视频副本。
- workspace 用户文件、Agent 任务产物、配置备份、运行日志。
- RAG 向量索引（清理后重新 embedding 可能产生费用）。

注意：入站图片/视频的 TTL 清理不做历史引用检查。删除后历史记录仍保留文字，
但原媒体字节不可用；模型若仍需要该图片/视频，必须要求用户重传，不能假装
已经看过。`inbound_media_hours` 可配置为 1–720 小时；本轮没有自动执行删除，
仍必须走 preview → execute。

危险类别在 API 中会明确返回 `category_not_implemented`，不会因为前端传了
类别名就静默降级成删除其他内容。本轮也没有实现隔离区恢复；执行前必须看清
预览，删除项不可恢复。

## 历史分页

`DebataHistoryStore` 保留 `load()` / `records()` 作为显式全量兼容入口，新增：

- `length()`：未建立全量缓存时走 SQL 有效记录计数。
- `fetch_suffix(start)`：只读位置 `start` 之后的活跃后缀。
- `fetch_recent(limit, byte_limit=...)`：有界最近记录。
- `iter_records(batch_size)`：流式遍历，数据变化时提前结束。
- `stats()`：计数、最大 history_index、DB/WAL 大小、分页缓存指标，不返回正文。
- `clear_page_cache()`：清空分页热缓存。

主 prompt 活跃窗口、摘要触发与 compaction 改为读取活跃后缀，不再为了取
最近窗口而加载全部归档历史。页缓存默认上限 32 MiB、16 页，可通过配置调整：

```yaml
storage:
  maintenance:
    history_cache_max_bytes: 33554432
    history_cache_page_size: 256
    history_cache_max_pages: 16
  retention:
    inbound_media_hours: 48   # 入站图片/视频的 TTL；1–720 小时
```

分页缓存按「总条数 + 起点」失效；append/truncate/replace/clear 会递增 revision
并清空页缓存。全量缓存只在显式 `load()` / `records()` 时建立，聊天页合并、
RAG bootstrap 与导出仍保留全量兼容路径。

## API

所有接口沿用 WebUI 全局鉴权；未启用鉴权时只应在可信环境运行。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/storage/usage` | 分类占用、磁盘水位、内存缓存、history 统计 |
| GET | `/api/storage/categories` | 类别与保护/实现状态 |
| POST | `/api/storage/cleanup/preview` | 冻结候选，返回 preview_id/令牌/代次/预计字节 |
| POST | `/api/storage/cleanup/execute` | 202 + job；需要 preview_id、令牌、幂等键、代次 |
| GET | `/api/storage/jobs` | 最近任务 |
| GET | `/api/storage/jobs/{job_id}` | 任务结果；进程重启后 running 标记 interrupted |
| POST | `/api/storage/jobs/{job_id}/cancel` | 请求取消；已提交批次不回滚 |
| POST | `/api/storage/jobs/{job_id}/restore` | 本轮明确返回 `restore_not_supported` |
| GET | `/api/context/status` | 只读预算快照与 history 统计 |

预览默认 5 分钟过期。执行时对每个磁盘候选再次校验：路径仍在受管根目录内、
不是符号链接、大小与 mtime_ns 与预览一致。变化项跳过并在结果中计数，不会
“猜着删”。

## 上下文预算

`GET /api/context/status` 返回：

- `window_requested`：`behavior.context.max_context_tokens` 或自动推荐值。
- `window_model`：运行时已知的模型上下文长度；未知时为 `null`。
- `window_effective = min(window_requested, window_model)`。
- `output_reserve = max(reserve_output_tokens, agents.chat.max_tokens)`。
- `safety_margin = max(safety_margin_min_tokens, ceil(安全余量% × 有效窗口))`。
- `input_budget = max(0, 有效窗口 - 输出预留 - 安全余量)`。
- soft/target/retry/urgent 阈值与 `policy_version`。

窗口小于输出预留时返回 `output_reserve_exceeds_window` 与 0 输入预算，不再用
`max(1024, ...)` 伪造空间。发送前硬 guard 始终使用净输入预算 B；主调用、
工具循环每轮、最终 tools=None 收尾、子 Agent 任务和摘要请求都有本地超限阻止。
`budget_policy_version=2` 目前仍只改变阈值基准展示。C02 的软触发/预测、
冷却、最少新增 token 与失败退避已经接入预检和后台压缩；直接预算抢救仍按
显式目标压缩，不受冷却阻断。

## 验证命令

```bash
.venv/bin/python -m pytest tests/test_history_paging.py \
  tests/test_context_budget_policy.py tests/test_storage_maintenance.py \
  tests/webui_config/test_context_api.py tests/webui_config/test_storage_api.py -q
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_kv_cache_real.py
.venv/bin/ruff check .
git diff --check
```

## 已知限制

- 自动磁盘 GC 定时器未启用；`mode` 仍为 `inventory_only`，页面会如实显示。
- 聊天记录按范围删除、资源引用表/租约、隔离区恢复尚未实现。入站图片/视频
  只有按时间 TTL 的清理，没有按来源引用/租约判断；删除后历史媒体字节不可用。
  音频和普通文件仍默认永久保留。
- 聊天页全量合并仍使用全量兼容读；RAG 历史 bootstrap 已改为分页流式入队并受
  有界队列背压，但 `archive.rag_records()` 仍一次返回归档列表，留待下一阶段分页。
- C02 压缩状态机保存在进程内，重启后重置；C03 冷启动追赶、C04 摘要 CAS 原子
  提交和 C05 工具循环内局部压缩仍留待后续。
- 工具循环已经接入逐步硬 guard，但超限时只阻止发送，不做循环内自动压缩（C05）。
- 供应商硬上下文限制未知时只能显示未核实，不能用本地估算保证绝不超窗。
- 磁盘候选在执行前校验路径、符号链接、大小和 mtime，但本机攻击者仍存在
  极短的 TOCTOU 替换窗口；管理接口必须保持 WebUI 鉴权。
- 内存缓存清理是进程内操作；服务重启后这些缓存自然消失，未完成任务会标记
  `interrupted`，不会假装完成。
- `temp_files` / `derived_cache` / 入站图片视频的保留期按文件的 mtime 判断，
  不把 mtime 冒充“最后访问时间”；受管目录之外的 workspace 用户文件完全不在
  扫描范围。
