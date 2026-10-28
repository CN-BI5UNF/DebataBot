# 后台运行 Karen

目标：关掉 SSH / 终端后，Karen 仍然继续收消息、跑人格后台和 WebUI。
和 TRSS-Yunzai 的“后台运行”思路一样：临时挂机可以用 `tmux` / `screen`，
长期运行推荐交给 `systemd` 管理（自动重启、开机自启、日志集中）。

日常只需要记住这几条：

```bash
karen start     # 启动
karen stop      # 停止
karen log       # 查看日志
karen enable    # 开机自启
karen disable   # 取消开机自启
```

> NapCat 是独立进程，不在本文启动范围内。请先确保 NapCat 已经运行并允许
> Karen 连接；Karen 断线后会自动重连。

### 没有 systemd 用户会话时

容器、WSL、部分精简系统里，`systemctl --user` 会报
`Failed to connect to bus`。此时 `karen` 会自动切换到内置后台模式
（PID 文件 + 独立进程），不需要 `systemd`：

```bash
karen start      # 启动
karen stop       # 停止
karen restart    # 重启
karen status     # 状态
karen log        # 日志
```

- `start/stop/restart/status/log` 都可正常使用。
- `enable` 会启动后台进程，但无法设置开机自启；请使用容器重启策略，
  或把 `karen start` 加入 cron `@reboot` / 登录脚本。
- 运行状态写在项目目录的 `.karen-run/`；应用日志仍在
  `~/.local/share/KarenBot/instances/default/logs/karen.log`。
- 内置模式不会在进程崩溃后自动拉起；需要自动重启请使用 systemd，
  或容器的 `restart` 策略。
- 安装脚本检测不到可用的 systemd 用户 bus 时会跳过 unit 安装，直接启动
  内置后台模式，不再报错退出。

---

## 0. 先确认能前台跑起来

```bash
cd ~/KarenBot
uv sync
uv run python main.py
```

浏览器打开终端打印的地址，完成首次配置后按 `Ctrl+C` 退出。
如果还没有配置，也可以先跑 CLI 向导：

```bash
uv run python main.py --no-gui --setup
```

后台服务建议直接调用项目虚拟环境，不依赖 shell 的 PATH：

```bash
.venv/bin/python main.py
```

---

## 1. 推荐：systemd 用户服务（无需 sudo）

适合个人设备 / 单用户 Armbian、Ubuntu、Debian。服务以当前用户运行，
不会用 root 跑机器人。

### 一次性安装

```bash
cd ~/KarenBot
bash scripts/install_systemd_user.sh
```

安装脚本会写入 systemd unit、安装 `karen` 命令，并启动 WebUI + QQ Runtime。

### 日常命令

```bash
karen start       # 启动（首次会自动安装服务）
karen stop        # 停止
karen log         # 实时查看日志
karen enable      # 开机自启（并立即启动）
karen disable     # 取消开机自启（当前进程继续运行）
karen status      # 运行状态
karen restart     # 重启
```

如果提示 `karen: command not found`，在项目目录用 `./karen start`；
安装脚本会尝试把命令装到 `~/.local/bin/karen`。

### 开启 linger（注销后继续运行）

`karen enable` 会尝试自动开启 linger。如果提示失败，执行：

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger   # 期望 Linger=yes
```

### headless / 自定义端口

先运行安装脚本（unit 已存在时 `karen start` 不会覆盖配置）：

```bash
bash scripts/install_systemd_user.sh --headless
bash scripts/install_systemd_user.sh --host 127.0.0.1 --port 9000
```

### 等价的 systemctl 命令

`karen` 只是把下面这些命令收起来了；排查时可以直接用：

```bash
systemctl --user status karenbot --no-pager
systemctl --user restart karenbot
systemctl --user stop karenbot
systemctl --user enable karenbot
systemctl --user disable karenbot
journalctl --user -u karenbot -n 200 --no-pager
```

### 查看 WebUI 地址和令牌

完整版服务的启动横幅会进入 journal：

```bash
journalctl --user -u karenbot -n 80 --no-pager | grep -A 12 'WebUI 已启动'
```

令牌也会保存在：

```bash
cat ~/.local/share/KarenBot/instances/default/webui_token.txt
```

如果设置了 `KAREN_DATA_ROOT`，路径会相应变化。
想固定令牌，可以编辑 unit 里的 `ExecStart`，追加 `--token 你的令牌`；
注意命令行参数对本机其他用户可见，个人设备上通常可以接受。

### 手动安装（不使用脚本）

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/karenbot.user.service \
   ~/.config/systemd/user/karenbot.service
# 如果项目不在 ~/KarenBot，先编辑 unit 里的 WorkingDirectory/ExecStart
systemctl --user daemon-reload
systemctl --user enable --now karenbot
sudo loginctl enable-linger "$USER"
```

---

## 2. 服务器：systemd 系统级服务

适合多用户服务器，或希望用一个专用账号运行：

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin karen
sudo mkdir -p /opt/KarenBot
sudo chown -R karen:karen /opt/KarenBot

# 用 karen 用户完成部署：
sudo -u karen -H bash
cd /opt/KarenBot
git clone https://github.com/CN-BI5UNF/KarenBot.git .
uv sync
exit

sudo cp scripts/systemd/karenbot.system.service /etc/systemd/system/karenbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now karenbot
```

按需修改 `/etc/systemd/system/karenbot.service` 中的：

- `User=` / `Group=`
- `WorkingDirectory=`
- `ExecStart=`
- 只跑 headless 时给 `main.py` 加 `--no-gui`

日志：

```bash
sudo journalctl -u karenbot -f
```

如果 NapCat 也是系统服务，在 `[Unit]` 里加：

```ini
After=napcat.service
Wants=napcat.service
```

---

## 3. 临时挂机：tmux

适合调试、短时间离开。SSH 断开后进程仍在。

```bash
cd ~/KarenBot
tmux new -s karen
# 在 tmux 里运行：
uv run python main.py
# 或：.venv/bin/python main.py
```

分离：按 `Ctrl+B`，松开后按 `D`。

```bash
tmux ls                 # 查看会话
tmux attach -t karen   # 重新进入
tmux kill-session -t karen
```

---

## 4. 临时挂机：screen

```bash
cd ~/KarenBot
screen -S karen
uv run python main.py
```

分离：按 `Ctrl+A`，松开后按 `D`。

```bash
screen -ls              # 查看
screen -r karen        # 重新进入
screen -S karen -X quit
```

---

## 5. nohup（最简单，不推荐长期）

```bash
cd ~/KarenBot
mkdir -p logs
nohup .venv/bin/python main.py > logs/karen-console.log 2>&1 &
echo $! > logs/karen.pid
```

停止：

```bash
kill "$(cat logs/karen.pid)"
```

说明：

- `nohup` 不会自动重启，机器重启后也不会自启；
- 控制台输出在 `logs/karen-console.log`；
- 应用自身日志仍在数据目录的 `logs/karen.log`；
- 长期运行请用 systemd。

---

## 6. 更新升级

```bash
cd ~/KarenBot
git pull --ff-only
uv sync
systemctl --user restart karenbot
```

系统级服务把最后一行换成：

```bash
sudo systemctl restart karenbot
```

更新前建议备份数据目录（默认在 `~/.local/share/KarenBot/`）：

```bash
tar -czf karen-data-$(date +%Y%m%d).tar.gz \
  -C ~/.local/share KarenBot
```

---

## 7. 常见问题

### 服务启动失败 / 203/EXEC

```bash
karen status
karen log        # 持续输出，Ctrl+C 退出
```

也可以只看最近 100 行：

```bash
journalctl --user -u karenbot -n 100 --no-pager
```

常见原因：

- `.venv/bin/python` 不存在：回到项目目录执行 `uv sync`；
- unit 里的路径不对：确认 `WorkingDirectory`、`ExecStart`；
- 用了 `uv run` 但 systemd 的 PATH 找不到 uv：改用 `.venv/bin/python`。

### 200/CHDIR

`WorkingDirectory` 不存在或当前用户没有权限。

### `systemctl --user` 报 Failed to connect to bus

先开启 linger 并重新登录：

```bash
sudo loginctl enable-linger "$USER"
```

仍然不行时，改用系统级服务（第 2 节）。

### 端口被占用

```bash
ss -ltnp 'sport = :8000'
```

换端口：

```bash
bash scripts/install_systemd_user.sh --port 9000
```

### 关掉 SSH 后服务停了

先运行 `karen enable`（会尝试开启 linger）。如果仍然停止，执行：

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger   # 期望 Linger=yes
```

### NapCat 没连上

Karen 只负责作为客户端连接 NapCat，不会替你启动 NapCat。
先确认 NapCat 的 WebSocket 地址/端口/token 与配置一致：

```bash
uv run python main.py --napcat          # 只重新配置 NapCat
uv run python main.py --test-adapter    # 连接测试 5 秒后退出
```

### 看不到 WebUI 令牌

```bash
cat ~/.local/share/KarenBot/instances/default/webui_token.txt
```

或查看启动日志：

```bash
journalctl --user -u karenbot -n 120 --no-pager | grep -A 12 'WebUI 已启动'
```

---

## 8. 安全建议

- **不要用 root 跑长期服务**。个人设备用用户服务；服务器用专用账号。
- WebUI 默认监听 `0.0.0.0:8000`。只在本机使用时改成 `--host 127.0.0.1`；
  需要公网访问时放在 Nginx/Caddy 反向代理和 HTTPS 后面。
- `--no-auth` 只适合完全可信的内网。关掉鉴权后，任何能访问端口的人都能看聊天记录、改配置。
- `~/.config/karenbot.env`（如果使用）建议 `chmod 600`。
- 数据目录默认权限已经由应用的 `UMask=0077` 收紧；不要把它放进公开可读的目录。
