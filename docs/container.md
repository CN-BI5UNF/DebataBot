# 内置 Docker 容器

KarenBot 可以给模型一个受控 Docker 容器，用于运行命令、读写 `/workspace`。
它不是绝对安全沙箱：容器内代码仍可能消耗资源、读取镜像内自带内容，或在
显式开启网络后外传数据。默认配置尽量收紧：无网络、cap-drop ALL、
no-new-privileges、非 root、只读根文件系统、只有 `/workspace` 可写。

## 快速启用（默认 B：命名卷，免管理员）

设置 →「容器」：

- `enabled=true`
- `storage_mode=volume_soft`（默认值，无需管理员初始化）
- `image` 默认 `python:3.12-slim`
- 其余保持默认

启用后到 WebUI「容器」页点一次「一键准备 / 启动」。它会检查 Docker、拉取缺失镜像、
创建容器并做一次 `printf` 测试；首次下载镜像可能需要几分钟。页面会直接列出
Docker 不可用、镜像缺失、C 大小不匹配等问题的修复按钮。

镜像默认 `auto_pull_image=true`：容器功能启用后，Runtime 启动会在后台检查镜像，
缺失时自动 `docker pull`（不会阻塞 WebUI 启动）。也可以提前手动拉取：

```bash
docker pull python:3.12-slim
```

Docker Hub 在某些网络不可达时，可在设置 →「容器」填写「Docker 镜像加速前缀」，
例如 `docker.m.daocloud.io`。只影响拉取，拉取后会 tag 成配置里的 image 名称；
第三方镜像源是否可信由用户自行判断。

保存后需重启 Runtime。修改内存、CPU、pids、存储路径、网络或镜像后，已有容器
会因配置签名不一致而拒绝启动；请执行 `karen container reset --yes` 重建容器
（持久数据保留）。WebUI「容器」页可查看状态，或：

```bash
karen container status
karen container start
```

B 模式使用 Docker 命名卷，配额是应用层软限制：文件工具的每次写入前/后检查
`du -sb /workspace`，超限拒绝后续写入；不是内核硬配额。通过 `exec` 直接写文件
可能暂时越过软配额，下一次文件工具写入或状态检查会发现；需要硬上限请使用 C。
单文件另受 `ulimit fsize` 和工具层 8 MiB 单次写入限制（import_file 可传输到
单文件配置上限）。

## C：固定大小存储（高级，硬上限）

C 模式把 `/workspace` 放在固定大小的文件系统镜像上，空间耗尽时文件系统返回
`ENOSPC`，是硬上限。C 需要一次性管理员安装；KarenBot 只在用户明确点击
「初始化 C 存储」时提权，不会在 C 未就绪时自动降级到 B。

第一次启用容器（或把存储模式切到 C）时，设置页会弹出初始化窗口。也可以到
WebUI「容器」页点击「初始化 C 存储」。

### Linux

弹窗会要求输入当前用户的 sudo 密码。密码只通过 stdin 交给 `sudo -S`，不会
写入 argv、环境变量、日志或配置文件，也不会保存在浏览器存储里。后端执行的是
固定脚本和固定参数，不接受模型输入。WebUI 走局域网 HTTP 时密码在传输中不受
TLS 保护，建议在本机浏览器操作；也可以使用下面的手动命令。

```bash
karen container storage status
karen container storage setup          # 只打印命令
sudo bash scripts/container/karen-container-storage-setup.sh \
  --size-mb 2048 \
  --image  /path/to/data/container/workspace.ext4 \
  --mountpoint /path/to/data/container/workspace
```

脚本创建稀疏 ext4 镜像、格式化、loop 挂载、把 workspace chown 给 uid/gid 1000，
并把 `.karen-container-storage.json` 写入**镜像所在目录**（不在容器可见的
workspace 内，模型无法读取或删除它）。系统重启后需要重新挂载；脚本会打印
可加入 `/etc/fstab` 的条目。扩容需要新建更大镜像并迁移，不要直接 resize 后
继续使用旧标记。

C 一旦初始化，`workspace_size_mb` 必须与镜像实际大小一致。改小/改大后页面会显示
“实际 X MiB / 当前配置 Y MiB”，可点「采用实际大小」写回配置，或新建镜像迁移；
不会静默 resize 旧镜像。

设置 `features.container.storage_mode=image_fixed`，并确认
`image_fixed_mountpoint` / `image_fixed_image_path` 与脚本一致。

### Windows（待实机验收）

1. 安装并启动 Docker Desktop，保持 **Linux containers** 模式。
2. 在设置页首次启用 C 模式时，点击「初始化存储」；应用会通过
   `Start-Process -Verb RunAs` 弹出标准 **UAC** 管理员确认，不需要 sudo 密码。
   如果服务运行在无桌面的远程会话，UAC 可能无法显示；此时请改用 B 模式，或
   以管理员 PowerShell 手动运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\container\karen-container-vhdx-setup.ps1 `
  -SizeMB 2048 `
  -VhdxPath "C:\Users\me\AppData\Local\KarenBot\...\container\workspace.vhdx" `
  -MountPoint "K:\workspace"
```

3. 把 `features.container.image_fixed_mountpoint` 设为脚本输出的
   `K:\workspace`，`image_fixed_image_path` 设为 VHDX 路径。
4. 重启 Runtime，运行 `karen container status` 或 WebUI「容器」页。

VHDX 脚本使用 diskpart 创建固定大小 VHDX，需要管理员权限；本仓库没有在
真实 Windows 机器上执行过它，请按“待实机验收”处理。Windows 上 Docker
Desktop 的 bind mount 必须指向 Docker 可访问的盘符路径；如果 Docker Desktop
使用 WSL2 后端，请确认该盘符已加入 Docker Desktop 的文件共享范围。不要在
本机做大文件写入/内存压力测试。

## 安全参数

- `--network none` 默认；`bridge` 需显式配置，存在外传风险。
- `--cap-drop ALL`、`--security-opt no-new-privileges`。
- `--user 1000:1000`，只读根文件系统，`/tmp` 为受限 tmpfs。
- 只挂载专用 `/workspace`；不挂载 Docker socket、宿主家目录或宿主根目录。
- 禁止 `--privileged`；创建参数在代码中固定，模型不能修改。
- 内存/CPU/pids/单文件/输出/超时均有上限。
- 一次性初始化容器只执行 `chown`，使用固定脚本，不接收模型输入。
- C 初始化脚本路径固定在 `scripts/container/`；Linux 密码只走 sudo stdin，
  Windows 走 UAC，不接受模型提供的命令或路径。
- 工具输出是外部内容；不要把其中的指令当作系统指令。

## 工具

模型使用一个 `container` 多路复用工具：

- `exec`：执行命令，返回 exit_code/stdout/stderr，超时和输出截断。
- `read_file` / `write_file` / `edit_file` / `list_files`：只允许
  `/workspace` 下的路径。
- `import_file` / `export_file`：在宿主 AI workspace 与容器 `/workspace`
  之间传输；宿主路径也必须位于 AI workspace。
- `reset`：删除并重建容器，保留持久卷/挂载数据。
- `status` / `storage`：容器和存储诊断。

## CLI

```bash
karen container status
karen container start
karen container stop
karen container reset --yes
karen container storage status
karen container storage setup [--yes]
```

`storage setup` 默认只打印命令；`--yes` 才会执行管理员脚本（终端里的 sudo
提示由 sudo 自己处理）。WebUI 首次启用使用上面的密码/UAC 弹窗流程。

## 数据风险

模型可以在容器内执行任意命令，包括删除 `/workspace` 中的文件。请把容器 workspace
视为可丢弃工作区，不要把唯一副本放在里面；重要文件应导出到宿主 AI workspace
或另行备份。Docker 隔离只保护宿主文件系统边界，不保证容器内数据可恢复。

## 备份

`data/container/` 下的 C 镜像文件，以及 Docker 管理的 B 命名卷，不纳入普通
用户数据备份，避免备份包被数 GiB 容器数据撑大。需要时请单独备份镜像文件/命名卷，
并在恢复后重新执行 `container storage status` 校验挂载标记。

## 故障排查

- **未找到 Docker CLI**：安装 Docker Engine / Docker Desktop，确保 `docker` 在 PATH；
  也可以设置环境变量 `KAREN_DOCKER_BIN` 指向 `docker.exe`/`docker` 的绝对路径后重启 Runtime。
- **daemon 不可连接**：启动 Docker Desktop / `systemctl start docker`。
- **镜像不存在 / Docker Hub 超时**：确认 `auto_pull_image=true`，在设置 →「容器」
  填写「Docker 镜像加速前缀」后重启；也可手动 `docker pull <image>`。
- **C 显示实际大小与配置不一致**：使用页面「采用实际大小」，或新建镜像并迁移；
  不要直接修改镜像文件大小。
- **C 未就绪**：运行 `karen container storage status`，确认挂载点和 marker；
  不要把它当成 B 继续运行。Linux 找不到 sudo 时可设置 `KAREN_SUDO_BIN`；
  Windows 找不到 PowerShell 时可设置 `KAREN_POWERSHELL_BIN`。
- **C 弹窗报密码错误**：确认输入的是当前运行 KarenBot 的用户密码，且该用户
  在 sudoers 中；密码不会保存，失败后重新输入即可。若 sudo 需要 TTY，
  请改用 WebUI 手动命令或终端 `storage setup --yes`。
- **容器内存不足**：调整 `memory_mb`，或让命令分批处理数据；不要做压力测试。
- **B 软配额超限**：清理 `/workspace` 或增大配额；后续写入会被拒绝。
