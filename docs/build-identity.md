# 构建/源码身份（W00）

本项目的 WebUI 没有构建步骤，静态 JS/CSS 可能已经更新，而 Python 进程仍在
运行旧模块。仅查询 `git HEAD` 会把“磁盘上的代码”误当成“线上已加载的代码”。

## 四种身份

| 身份 | 含义 |
|---|---|
| base commit | 进程启动时 git HEAD；工作树可能另有 WIP |
| dirty/source bundle | 受控源码/文档/测试（含未跟踪文件）的指纹；不读取数据目录、日志、密钥或聊天 |
| loaded | 进程启动时捕获的身份，之后不随磁盘变化 |
| released | 远端 tag/Release；只在 `git show`/Release API 中查询，不冒充本地已加载 |

`webui/server.py` 在加载 Runtime 前调用
`core.build_identity.capture_build_identity(project_root)`。WebUI 提供：

```text
GET /api/system/build
```

返回：

- `loaded`：本进程启动时捕获的身份（`base_commit`、`branch`、`dirty`、
  `source_bundle_sha256`、文件数、捕获时间）；
- `source`：当前磁盘的同类身份；
- `loaded_matches_source`：两者 base commit 与源码包指纹是否一致；
- `note`：解释静态资源可能已更新而 Python 进程仍旧。

## 指纹范围与边界

- 优先使用 `git ls-files -z --cached --others --exclude-standard`，因此新增
  但未 `git add` 的源码也会被计入。
- 使用**明确目录白名单**（`core`、`webui`、`app_config`、`agents`、`tests`、
  `docs`、`deploy` 等）和根目录约定文件；不做任意非 ignored `.yaml/.json`
  扫描。
- 排除 `.git`、虚拟环境、缓存、`data/`、`logs/`、`memory/`、`vector/`、
  `workspace/`、`personas/` 等。
- 不哈希 `.env`、`webui_token.txt`、`secrets.enc`、PEM、`update_state.json`
  等敏感/本地文件，也不把它们的路径放进 `wip_paths`。
- 所有文件先 `lstat` 确认普通文件、`resolve()` 确认仍在 project root 内；
  symlink/reparse 逃逸直接跳过，不跟随读取。
- 未跟踪文件只记录受控源码路径；正文哈希仅用于判断源码是否变化，不会出现在
  日志或 API 的错误信息中。

`identity_kind=startup_source_snapshot` 明确表示：这是启动时磁盘源码快照，
不是所有已加载模块字节的证明，也不是代码签名/可信来源认证。测试结果必须
绑定受控 source fingerprint；release 身份单独记录。

## 测试/线上边界

工作树测试报告与 live 进程必须分别报告身份。测试通过不等于当前 live 进程
加载了新代码；部署后应重启并再次查询 `/api/system/build`，确认
`loaded_matches_source=true`（或明确记录不重启的原因）。静态资源已刷新也不能
证明后端逻辑生效。

## 常见排查

- `loaded_matches_source=false` 且刚改了 Python：重启服务后再验证。
- `dirty=true`：预期存在开发中的 WIP；不是自动发布的依据。
- `loaded` 为空：进程不是通过 `webui.server.run_webui` 启动（例如测试直接
  构造 app），此时只能看 `source`，不能宣称已加载身份。
