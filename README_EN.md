<div align="center">

# DebataBot

**A chatbot framework that makes virtual characters feel alive**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![WebUI](https://img.shields.io/badge/UI-WebUI%20%28FastAPI%29-blueviolet.svg)](#)
[![CI](https://github.com/CN-BI5UNF/DebataBot/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/CN-BI5UNF/DebataBot/actions/workflows/test.yml)

[Quick start](#quick-start) · [debata commands](#debata-commands) · [Common uv commands](#common-uv-commands) · [Documentation](#documentation)

</div>

---

> "What if the thing behind `/v1/chat/completions` were a person?"

DebataBot does not come with a fixed personality. You give it one, and it
becomes that person. The framework is named after Debata, the first character
built with it.

- Repository: <https://github.com/CN-BI5UNF/DebataBot>
- Default branch: `main`
- In-app update branch: `refactor/main` (kept in sync with `main`, used by the updater for Releases/tags)
- Current state: `0.9.0-alpha` refactor snapshot; interfaces may still change

> This repository is a refactor snapshot of
> [binglang001/Debata_Agent](https://github.com/binglang001/Debata_Agent).
> Original copyright and Apache-2.0 notices are preserved.

## Features

- **Persona system**: persona prompts, dynamic state, relationships, and memory.
- **QQ / NapCat**: WebSocket client/server modes, private and group chats, send queue.
- **Long-term memory**: file mode or RAG mode; archives never rewrite raw history.
- **Tool loop**: send, recall, memory, search, weather, scheduled wake-ups, sub-agents.
- **WebUI**: FastAPI backend + static frontend with setup wizard, settings, chats, memory, logs, and updates.
- **Multiple providers**: OpenAI, Anthropic, DeepSeek, GLM, Qwen, Gemini, OpenRouter presets, plus custom compatible endpoints.
- **Native / standalone vision**: images can go to the main model or an optional separate vision provider.
- **Online updates**: choose a GitHub Release/tag in Settings → Update; only source code changes, data is preserved.

See [Features](docs/features.md) and [Architecture](docs/architecture.md) for details.

## Requirements

- **Python 3.10+** (CI covers 3.11 and 3.12)
- **uv** for dependencies and virtual environments
- **NapCat** (optional, required for QQ): [install guide](https://napneko.github.io/guide/start-install)
- **LLM API key** (DeepSeek is a good default for Chinese and cost)
- Linux / Windows / macOS; servers can run headless

## Quick start

### 1. Install uv

Linux / macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

See the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for other options.

### 2. Clone and install

```bash
git clone https://github.com/CN-BI5UNF/DebataBot.git
cd DebataBot

# Runtime dependencies; creates .venv and the `debata` entry point
uv sync

# Optional: uvloop performance extra on Linux/macOS
uv sync --extra perf
```

### 3. First run

The WebUI is the recommended setup path:

```bash
# Default: 0.0.0.0:8000, reachable on the LAN
uv run python main.py

# Local-only
uv run python main.py --host 127.0.0.1 --port 8000
```

The terminal prints the access URLs and a WebUI token. The first visit opens
the setup wizard. The token is also stored in `webui_token.txt` under the data
directory.

CLI wizard for servers / SSH:

```bash
uv run python main.py --no-gui --setup
```

Fully headless:

```bash
uv run python main.py --no-gui
```

### 4. Run in the background (Linux)

Install and start the systemd user service once (no sudo):

```bash
bash scripts/install_systemd_user.sh
```

Then use the short commands:

```bash
./debata start      # start; installs the service on first run
./debata status     # show status
./debata log        # follow logs (Ctrl+C to exit)
./debata stop       # stop
```

Without a systemd user session (containers, WSL, minimal systems), the CLI
falls back to PID-file background mode for `start/stop/restart/status/log`.
Boot autostart then needs a container restart policy or cron `@reboot`.

Custom host/port or headless mode:

```bash
bash scripts/install_systemd_user.sh --host 127.0.0.1 --port 9000
bash scripts/install_systemd_user.sh --headless
```

Full guide: [Background Running](docs/background_running.md).

## debata commands

`debata` is the service-management command. The installer places it in
`~/.local/bin/debata`; if that directory is not on `PATH`, use `./debata` from
the project directory or `uv run debata ...`.

| Command | Purpose |
|---|---|
| `debata start` | Start; installs the systemd user service on first run |
| `debata stop` | Stop the service |
| `debata restart` | Restart the service |
| `debata status` | Show status |
| `debata log` | Follow logs (Ctrl+C to exit) |
| `debata enable` | Enable boot autostart and start now |
| `debata disable` | Disable boot autostart (current process keeps running) |
| `debata install` | Install the service without starting |
| `debata uninstall` | Uninstall the service |
| `debata help` | Show help |

Examples:

```bash
./debata status
./debata log
./debata restart
uv run debata status          # without relying on PATH
```

Notes:

- systemd user services are preferred and do not require sudo.
- `debata enable` tries to enable linger; if it fails, run
  `sudo loginctl enable-linger "$USER"`.
- NapCat must be kept running separately.
- Re-run the installer with options to change host/port or headless mode.

## Common uv commands

| Command | Purpose |
|---|---|
| `uv sync` | Create/sync `.venv` from `uv.lock` and install the project |
| `uv sync --extra dev` | Install development tools (pytest, ruff) |
| `uv sync --extra perf` | Install uvloop on Linux/macOS |
| `uv sync --extra dev --extra perf` | Install both extras |
| `uv run python main.py` | Start the WebUI in the project environment |
| `uv run python main.py --no-gui --setup` | Run the CLI setup wizard |
| `uv run python main.py --napcat` | Reconfigure NapCat only |
| `uv run python main.py --test-adapter` | Test the NapCat connection for 5 seconds |
| `uv run debata status` | Run the `debata` entry point |
| `uv run pytest tests/ -q --ignore=tests/test_kv_cache_real.py` | Run tests (dev extra required) |
| `uv run ruff check .` | Run lint |
| `uv lock` | Re-resolve and update `uv.lock` |
| `uv add <package>` | Add a runtime dependency |
| `uv run --frozen ...` | Use the current lockfile without updating |
| `uv tree` | Show the dependency tree |

Common combinations:

```bash
uv sync --extra dev
uv run pytest tests/ -q --ignore=tests/test_kv_cache_real.py
uv run ruff check .
uv run python main.py --host 127.0.0.1 --port 9000
```

## Common launch flags

```bash
uv run python main.py --port 9000          # change WebUI port
uv run python main.py --host 127.0.0.1     # local-only
uv run python main.py --token my-token     # fixed WebUI token
uv run python main.py --no-auth            # disable auth (trusted LAN only)
uv run python main.py --no-gui             # headless runtime
uv run python main.py --no-gui --setup     # CLI setup wizard
uv run python main.py --napcat             # reconfigure NapCat only
uv run python main.py --test-adapter       # test NapCat connection
uv run python main.py --list-secrets       # list secret IDs, not values
```

## Configuration and data directory

Default Linux data directory:

```text
~/.local/share/Debata_Agent/instances/default/
├── config.yaml          # main config (gitignored)
├── secrets.enc          # encrypted API keys
├── logs/debata.log      # application log
├── memory/              # chat history, important memory, usage records
├── vector/              # RAG vector store
├── workspace/           # AI read/write sandbox
├── providers/presets/   # user-defined provider presets
└── webui_token.txt      # WebUI access token
```

- Use `--config` to point at another config file.
- `DEBATA_DATA_ROOT` overrides the data root; old data is migrated on first start.
- See [Data Root Contract](docs/data_root_contract.md) for other platforms.
- `config.yaml`, `secrets.enc`, chat databases, and workspace are gitignored. Back them up yourself.

## Branches and updates

- `main` — default branch for browsing, cloning, and snapshot releases.
- `refactor/main` — stable branch tracked by the in-app updater; kept in sync with `main`.
- Development happens on feature branches and pull requests. There is no `develop` branch.

The WebUI updater reads GitHub Releases/tags and targets `refactor/main`. It
changes source code only; data is preserved. Dependency changes or a dirty
worktree will block the update. See [Releases](docs/releases.md).

## Testing and quality

```bash
uv sync --extra dev
uv run pytest tests/ -q --ignore=tests/test_kv_cache_real.py
uv run ruff check .
```

GitHub Actions runs pytest and ruff on Ubuntu / Windows × Python 3.11 / 3.12;
see [.github/workflows/test.yml](.github/workflows/test.yml).
`tests/test_kv_cache_real.py` requires live API/network access and is skipped by default.

## Project layout

```text
DebataBot/
├── adapters/               Channel adapters (NapCat)
├── providers/              LLM providers, presets, capability matrix
├── agents/                 Chat / proactive / summary / persona / social agents
├── mind/                   Persona state, profiles, relationships, clues, todos
├── memory/                 History, important memory, archive, RAG, usage stores
├── tools/                  AI tools, schema profiles, result shrinking
├── features/               Vision / TTS / weather / search / embedding
├── core/                   Event bus, pipelines, runtime, model view
├── app_config/             Config, paths, secret encryption
├── webui/                  FastAPI backend + static frontend
├── plugins/                Local model plugins
├── utils/                  CQ parsing, token estimation, image contracts
├── personas/               Persona files
├── scripts/                Installer, benchmarks, maintenance scripts
└── tests/                  Unit, protocol, integration, and WebUI tests
```

## Documentation

- [Getting Started](docs/getting_started.md) — step-by-step with screenshots
- [Background Running](docs/background_running.md) — systemd / tmux / screen / nohup
- [Features](docs/features.md) — chat style, memory, persona, WebUI
- [Persona Writing Guide](docs/persona_writing_guide.md) — writing a persona that feels alive
- [Architecture](docs/architecture.md) — model view, token metering, tool profiles
- [WebUI Guide](docs/webui.md) — pages and API surface
- [Releases](docs/releases.md) — releases, rollback, compatibility
- [Provider Development](docs/provider_development.md) — adding LLM backends
- [Adapter Development](docs/adapter_development.md) — adding chat platforms
- [Storage Maintenance](docs/storage_maintenance.md) — paging, inventory, safe cleanup
- [Data Root Contract](docs/data_root_contract.md) — data directory and migration
- [KV Cache Benchmark](docs/kv_cache_benchmark.md) — caching and estimates

## Security and privacy

- `config.yaml`, `secrets.enc`, `data/`, personal personas, logs, and the workspace are gitignored.
- The WebUI uses token auth by default. `--no-auth` is only for a fully trusted LAN.
- For public access, use Nginx/Caddy with HTTPS, or bind to `127.0.0.1`.
- Run services as a dedicated user, never as root.
- Chat history, personas, and configuration are personal data; back up and sanitize before sharing.

## Known limitations

- This is a refactor snapshot and does not include the upstream full commit history. See [binglang001/Debata_Agent](https://github.com/binglang001/Debata_Agent) for the upstream stable line.
- Real provider rate limits, image billing, and online KV hit rates still need authorized small-traffic validation.
- Image tokens are a resource reservation, not an exact provider bill.
- Online updates do not install dependencies or run data migrations.

## License and acknowledgements

This project originates from
[binglang001/Debata_Agent](https://github.com/binglang001/Debata_Agent) and its
contributors. Original copyright and license notices are preserved.

[Apache License 2.0](LICENSE) — free for commercial use, modification, and distribution.

Acknowledgements: [NapCat](https://github.com/NapNeko/NapCatQQ),
[FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/),
[uv](https://github.com/astral-sh/uv), and everyone who filed issues, opened
pull requests, or helped with testing.

<div align="center">

**「砚台旁有墨，纸上有空」**

*Ink by the inkstone, space on the paper.*

</div>
