# 受信 launcher 部署包（未安装）

本目录只准备可审阅的 launcher 代码和 systemd 样例，**没有**修改当前
`debata-agent.service`、没有复制文件到系统目录、没有重启服务。

## 为什么需要独立程序

`core/updater.py` 的应用内兼容门禁会被 `git switch` 一起换回旧代码；
直接执行 `main.py` 也能绕过。受管部署应在 **源码树之外** 放一份只依赖
标准库、root 可写但服务用户不可改的 launcher，在 import/启动 Runtime 前检查：

1. 目标源码的 `compatibility.json`（manifest_format / launcher protocol /
   data_contract_epoch / 每个 plane 的读写范围）；
2. 本地 `config.yaml`、`memory/**`、`vector/**`、`maintenance.sqlite3` 的只读
   元数据；
3. `compatibility_state.json` 的迁移 phase/generation/features；
4. 首次安装（无配置）默认放行向导，但不放行已知数据上的迁移。

失败退出码 78，目标 Runtime 不会启动。它不能拦截同 UID/root 手动绕过，也
不替代数据迁移/备份。

## 审阅与安装（需要用户明确授权）

```bash
# 先只读检查
python3 deploy/trusted_launcher.py \
  --project-root /home/kickpi/Debata_Agent \
  --data-dir /home/kickpi/.local/share/Debata_Agent/instances/default \
  --check-only --json

# 授权后再安装固定副本（示例；不要自动 sudo/覆盖）
sudo install -d -m 0755 -o root -g root /usr/local/lib/debata
sudo install -m 0755 -o root -g root \
  deploy/trusted_launcher.py /usr/local/lib/debata/trusted_launcher.py
```

要求：launcher 文件 root:root、0755，所在目录 root:root 且服务用户无写权限。
`compatibility.json` 和数据目录仍由部署侧管理；launcher 只读。

launcher 还会：

- 校验 target executable 和 `main.py` 都在 `--project-root` 内；
- 校验命令的 `--config` 与 `--config`/`--data-dir` 绑定一致；
- exec 前设置 `DEBATA_DATA_ROOT`，不让旧环境变量改写数据根；
- 检查 `<DATA_DIR>/maintenance/controller_state.json`，维护 phase 拒绝启动；
- `setup_required` 只有显式 `--allow-setup` 才允许进入向导。

## systemd drop-in 样例

样例在 `deploy/systemd/debata-launcher.conf.example`。当前 unit 的
`ExecStart` 类似：

```text
ExecStart="/home/kickpi/Debata_Agent/.venv/bin/python" \
  "/home/kickpi/Debata_Agent/main.py" --host 0.0.0.0 --port 8000
```

受管版本改为先经过固定 launcher：

```ini
[Service]
ExecStart=
ExecStart=/usr/local/lib/debata/trusted_launcher.py \
  --project-root /home/kickpi/Debata_Agent \
  --data-dir /home/kickpi/.local/share/Debata_Agent/instances/default \
  -- /home/kickpi/Debata_Agent/.venv/bin/python \
  /home/kickpi/Debata_Agent/main.py --host 0.0.0.0 --port 8000
```

安装步骤（**本轮不执行**）：

```bash
mkdir -p ~/.config/systemd/user/debata-agent.service.d
cp deploy/systemd/debata-launcher.conf.example \
  ~/.config/systemd/user/debata-agent.service.d/launcher.conf
systemctl --user daemon-reload
systemctl --user restart debata-agent.service
systemctl --user status debata-agent.service
```

## 回滚/卸载

```bash
rm ~/.config/systemd/user/debata-agent.service.d/launcher.conf
systemctl --user daemon-reload
systemctl --user restart debata-agent.service
sudo rm -f /usr/local/lib/debata/trusted_launcher.py
```

回滚前先确认旧代码理解当前数据；源码回滚不等于数据回滚。更新/回退需重新做
兼容门禁与备份演练。

## 边界

- launcher 只是启动保护，不是迁移执行器，不会自动回退数据。
- 没有真实维护控制器/外部 apply 进程，生产迁移仍 blocked。
- 不会创建新 tag、不会上传代码、不会触发远端检查。
- 当前设备的 systemd unit 保持原样；部署必须获得当前明确授权。
