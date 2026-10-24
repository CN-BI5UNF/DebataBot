<div align="center">

# DebataBot

### 让虚拟角色真正活过来的通用聊天智能体框架

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![WebUI](https://img.shields.io/badge/UI-WebUI%20%28FastAPI%29-blueviolet.svg)](#)
[![CI](https://github.com/CN-BI5UNF/DebataBot/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/CN-BI5UNF/DebataBot/actions/workflows/test.yml)

[快速开始](#快速开始) · [debata 指令](#debata-指令) · [uv 常用指令](#uv-常用指令) · [文档](#文档)

</div>

---

> 「虚拟角色不只是个会回复消息的程序，而是一个**真实的，能和你聊天的，有个性、有内心的**人。」

DebataBot 不绑定任何特定角色。你给它一个人格，它就活成那个人。框架名取自项目第一个被实现的角色 Debata。

- 仓库：<https://github.com/CN-BI5UNF/DebataBot>
- 默认分支：`main`
- 应用内更新跟踪分支：`refactor/main`（与 `main` 同步，供更新器读取 Releases/tag）
- 当前状态：`0.9.0-alpha` 重构快照，接口仍可能调整

> 本项目是 [binglang001/Debata_Agent](https://github.com/binglang001/Debata_Agent) 的重构快照，保留原项目版权与 Apache-2.0 协议。

## 它能做什么

- **人格系统**：人格 prompt、动态状态、关系与记忆独立于主请求缓存。
- **QQ / NapCat 接入**：WebSocket 客户端/服务端模式、私聊/群聊、发送队列。
- **长期记忆**：重要记忆文件与 RAG 两种模式，归档不改写原始历史。
- **多轮工具循环**：发送、撤回、记忆、搜索、天气、定时唤醒、子 Agent 等。
- **WebUI**：FastAPI 后端 + 无构建步骤的静态前端；向导、设置、聊天、记忆、日志、更新。
- **多 Provider**：OpenAI、Anthropic、DeepSeek、GLM、Qwen、Gemini、OpenRouter 等预设，也支持自定义兼容端点。
- **原生视觉 / 独立视觉**：图片可进入主模型请求，也可按需走独立视觉接口。
- **在线更新**：设置 → 更新，从 GitHub Releases/tag 选择更新或回退；只改源码，不覆盖数据。

更完整的能力说明见 [功能列表](docs/features.md) 与 [架构总览](docs/architecture.md)。

## 环境要求

- **Python 3.10+**；CI 覆盖 Python 3.11 / 3.12
- **uv**（推荐的依赖与虚拟环境管理器）
- **NapCat**（可选，接入 QQ 时需要）：[安装指南](https://napneko.github.io/guide/start-install)
- **LLM API Key**（推荐 DeepSeek，中文与成本较友好）
- Linux / Windows / macOS；服务器可 headless 运行

## 快速开始

### 1. 安装 uv

Linux / macOS：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

也可以参考 [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)。

### 2. 克隆并安装依赖

```bash
git clone https://github.com/CN-BI5UNF/DebataBot.git
cd DebataBot

# 安装运行依赖，创建 .venv
uv sync

# 可选：Linux/macOS 性能优化（uvloop）
uv sync --extra perf
```

`uv sync` 会创建 `.venv` 并安装 `debata` 命令入口；后续用 `uv run` 时无需手动激活虚拟环境。

### 3. 首次启动

推荐直接用 WebUI 配置：

```bash
# 默认监听 0.0.0.0:8000，局域网可访问
uv run python main.py

# 只允许本机访问
uv run python main.py --host 127.0.0.1 --port 8000
```

终端会打印访问地址和 WebUI 令牌；首次打开会自动进入配置向导。令牌也会保存在数据目录的 `webui_token.txt`。

服务器 / SSH 环境可以用 CLI 向导：

```bash
uv run python main.py --no-gui --setup
```

完全无界面运行：

```bash
uv run python main.py --no-gui
```

### 4. 后台运行（Linux）

首次安装并启动 systemd 用户服务（不需要 sudo）：

```bash
bash scripts/install_systemd_user.sh
```

之后可以用短命令管理：

```bash
./debata start      # 启动；首次会自动安装服务
./debata status     # 查看状态
./debata log        # 实时查看日志，Ctrl+C 退出
./debata stop       # 停止
```

没有 systemd 用户会话（容器、WSL、精简系统）时，`start/stop/restart/status/log` 会自动回退到 PID 文件后台模式；开机自启需要容器重启策略或 cron `@reboot`。

自定义监听地址 / headless：

```bash
bash scripts/install_systemd_user.sh --host 127.0.0.1 --port 9000
bash scripts/install_systemd_user.sh --headless
```

完整说明见 [后台运行指南](docs/background_running.md)。

## debata 指令

`debata` 是项目提供的服务管理命令。安装脚本会把它放到 `~/.local/bin/debata`；如果 PATH 中没有，可以在项目目录使用 `./debata`，或用 `uv run debata ...`。

| 命令 | 作用 |
|---|---|
| `debata start` | 启动服务；首次运行会自动安装 systemd 用户服务 |
| `debata stop` | 停止服务 |
| `debata restart` | 重启服务 |
| `debata status` | 查看运行状态 |
| `debata log` | 实时跟随日志（Ctrl+C 退出） |
| `debata enable` | 设置开机自启，并立即启动 |
| `debata disable` | 取消开机自启（当前进程继续运行） |
| `debata install` | 只安装服务，不启动 |
| `debata uninstall` | 卸载服务 |
| `debata help` | 显示帮助 |

常用示例：

```bash
./debata status
./debata log
./debata restart
uv run debata status          # 不想依赖 PATH 时
```

说明：

- 优先使用 systemd 用户服务，不需要 sudo。
- `debata enable` 会尝试开启 linger；若提示失败，执行 `sudo loginctl enable-linger "$USER"`。
- NapCat 需要单独保持运行。
- 修改端口 / headless 请重新运行安装脚本，传入对应参数。

## uv 常用指令

| 命令 | 作用 |
|---|---|
| `uv sync` | 按 `uv.lock` 创建/同步 `.venv` 并安装项目 |
| `uv sync --extra dev` | 安装开发依赖（pytest、ruff 等） |
| `uv sync --extra perf` | 安装 Linux/macOS 性能优化（uvloop） |
| `uv sync --extra dev --extra perf` | 同时安装开发与性能依赖 |
| `uv run python main.py` | 在项目环境中启动 WebUI |
| `uv run python main.py --no-gui --setup` | 运行 CLI 配置向导 |
| `uv run python main.py --napcat` | 只重新配置 NapCat |
| `uv run python main.py --test-adapter` | 测试 NapCat 连接 5 秒后退出 |
| `uv run debata status` | 通过项目入口执行 `debata` 子命令 |
| `uv run pytest tests/ -q --ignore=tests/test_kv_cache_real.py` | 运行测试（需先装 dev extra） |
| `uv run ruff check .` | 运行 lint |
| `uv lock` | 重新解析并更新 `uv.lock` |
| `uv add <package>` | 添加运行依赖并更新 `pyproject.toml` / `uv.lock` |
| `uv run --frozen ...` | 严格使用现有 lockfile，不自动更新依赖 |
| `uv tree` | 查看依赖树 |

常用组合：

```bash
# 安装开发环境
uv sync --extra dev

# 跑全量测试
uv run pytest tests/ -q --ignore=tests/test_kv_cache_real.py

# lint
uv run ruff check .

# 临时指定端口启动
uv run python main.py --host 127.0.0.1 --port 9000
```

## 常用启动参数

```bash
uv run python main.py --port 9000          # 修改 WebUI 端口
uv run python main.py --host 127.0.0.1     # 只监听本机
uv run python main.py --token my-token     # 固定 WebUI 令牌
uv run python main.py --no-auth            # 关闭鉴权，仅限完全可信内网
uv run python main.py --no-gui             # 纯命令行 Runtime
uv run python main.py --no-gui --setup     # CLI 配置向导
uv run python main.py --napcat             # 重新配置 NapCat
uv run python main.py --test-adapter       # 测试 NapCat 连接
uv run python main.py --list-secrets       # 列出密钥 ID，不显示值
```

## 配置与数据目录

Linux 默认数据目录：

```text
~/.local/share/Debata_Agent/instances/default/
├── config.yaml          # 主配置（已被 .gitignore 排除）
├── secrets.enc          # 加密后的 API 密钥
├── logs/debata.log      # 应用日志
├── memory/              # 聊天历史、重要记忆、用量记录
├── vector/              # RAG 向量库
├── workspace/           # AI 可读写工作区
├── providers/presets/   # 用户自定义 provider 预设
└── webui_token.txt      # WebUI 访问令牌
```

- 可用 `--config` 指定其他配置文件。
- `DEBATA_DATA_ROOT` 可以覆盖数据根目录；旧数据会在首次启动时迁移。
- 其他平台的默认路径与迁移规则见 [数据根契约](docs/data_root_contract.md)。
- `config.yaml`、`secrets.enc`、聊天库和 workspace 都不进入 Git，请自行备份。

## 分支与更新

- `main`：默认分支，用于浏览、克隆和发布快照。
- `refactor/main`：应用内更新器跟踪的稳定分支，与 `main` 保持同步。
- 日常开发请使用功能分支并提交 PR；本仓库不再维护 `develop` 分支。

WebUI 的「设置 → 更新」会从 GitHub Releases/tag 读取版本，目标分支为 `refactor/main`。更新只切换源码，不覆盖数据目录；依赖变化或脏工作树会拒绝。发布流程见 [Releases 版本管理](docs/releases.md)。

## 测试与质量

```bash
uv sync --extra dev
uv run pytest tests/ -q --ignore=tests/test_kv_cache_real.py
uv run ruff check .
```

GitHub Actions 在 Ubuntu / Windows × Python 3.11 / 3.12 上运行 pytest 与 ruff，配置见 [.github/workflows/test.yml](.github/workflows/test.yml)。`tests/test_kv_cache_real.py` 需要真实 API/网络，默认不跑。

## 项目结构

```text
DebataBot/
├── adapters/               渠道适配器（NapCat 等）
├── providers/              LLM 协议、预设与能力矩阵
├── agents/                 主聊天 / 主动思考 / 总结 / 人格管理 / 社交
├── mind/                   人格状态、用户画像、关系、线索、待办
├── memory/                 历史、重要记忆、归档、RAG、用量存储
├── tools/                  AI 工具、schema 分级、安全图片工具
├── features/               视觉、TTS、天气、搜索、Embedding
├── core/                   事件总线、消息管道、Runtime、模型视图
├── app_config/             配置、路径、密钥加密
├── webui/                  FastAPI 后端 + 静态前端
├── plugins/                本地模型插件
├── utils/                  CQ 解析、token 估算、图片契约、请求分区
├── personas/               人格目录
├── scripts/                安装、验证、基准与维护脚本
└── tests/                  单元、协议、集成与 WebUI 回归
```

## 文档

- [图文教程](docs/getting_started.md) — 从零开始
- [后台运行指南](docs/background_running.md) — systemd / tmux / screen / nohup
- [功能列表](docs/features.md) — 聊天风格、记忆、人格、WebUI
- [角色编写指南](docs/persona_writing_guide.md) — 怎么写会「活」的人格
- [架构总览](docs/architecture.md) — 模块依赖、模型视图、Token 计量
- [WebUI 说明](docs/webui.md) — 页面结构、接口一览
- [Releases 版本管理](docs/releases.md) — 发布、回退与兼容声明
- [提供商开发](docs/provider_development.md) — 接新的 LLM
- [适配器开发](docs/adapter_development.md) — 接新的聊天平台
- [存储与缓存维护](docs/storage_maintenance.md) — 分页、盘点、安全清理
- [数据根契约](docs/data_root_contract.md) — 数据目录与迁移
- [KV 缓存基准](docs/kv_cache_benchmark.md) — 缓存与估算说明

## 安全与隐私

- `config.yaml`、`secrets.enc`、`data/`、个人人格、日志和 workspace 均被 `.gitignore` 排除。
- WebUI 默认令牌鉴权；`--no-auth` 只适合完全可信的内网。
- 公网访问请使用 Nginx/Caddy 反向代理 + HTTPS，或只监听 `127.0.0.1`。
- 长期服务请使用专用用户，不要用 root。
- 聊天记录、人格和配置属于个人数据；迁移或分享前请备份并脱敏。

## 已知限制

- 这是重构快照，不包含上游完整提交历史；上游稳定版本见 [binglang001/Debata_Agent](https://github.com/binglang001/Debata_Agent)。
- 真实供应商限流、图片计费和线上 KV 命中率仍需在授权后用小流量验证。
- 图片 token 是资源预留，不是供应商精确账单。
- 在线更新不会自动安装依赖或执行数据迁移。

## License 与致谢

本项目源自 [binglang001/Debata_Agent](https://github.com/binglang001/Debata_Agent) 及其贡献者，保留原始版权与协议声明。

[Apache License 2.0](LICENSE) — 可用于商业使用、修改与分发。

特别致谢：[NapCat](https://github.com/NapNeko/NapCatQQ)、[FastAPI](https://fastapi.tiangolo.com/)、[Uvicorn](https://www.uvicorn.org/)、[uv](https://github.com/astral-sh/uv)，以及所有提交 Issue、PR 和参与测试的人。

<div align="center">

**「砚台旁有墨，纸上有空」**

*Ink by the inkstone, space on the paper.*

</div>
