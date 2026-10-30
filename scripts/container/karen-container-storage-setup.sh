#!/usr/bin/env bash
# KarenBot C-mode container storage: fixed-size ext4 image + loop mount.
#
# This is a one-time administrator action. It creates a sparse image, formats
# it ext4, mounts it, chowns the workspace to uid/gid 1000, and writes the
# marker checked by ContainerStorage. It does not resize or delete existing
# data. Run with sudo. No large file content is written by this script.
set -euo pipefail

SIZE_MB=2048
IMAGE=""
MOUNTPOINT=""

usage() {
  echo "用法: sudo bash $0 --size-mb 2048 --image /path/workspace.ext4 --mountpoint /path/workspace"
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --size-mb) SIZE_MB="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --mountpoint) MOUNTPOINT="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

[[ -n "$IMAGE" && -n "$MOUNTPOINT" ]] || usage
[[ "$SIZE_MB" =~ ^[0-9]+$ && "$SIZE_MB" -ge 64 ]] || {
  echo "size-mb 必须是 >=64 的整数"; exit 2;
}
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "需要 root：请使用 sudo，或在 WebUI 首次启用时通过弹窗输入密码。"
  exit 1
fi
command -v mkfs.ext4 >/dev/null || { echo "缺少 mkfs.ext4（e2fsprogs）"; exit 1; }
command -v mount >/dev/null || { echo "缺少 mount"; exit 1; }

mkdir -p "$(dirname "$IMAGE")" "$MOUNTPOINT"
MARKER="$(dirname "$IMAGE")/.karen-container-storage.json"

if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
  # marker 故意放在镜像目录（容器不可见），不是挂载点里面。
  if [[ -f "$MARKER" ]]; then
    marker_size="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("size_mb", ""))' "$MARKER" 2>/dev/null || true)"
    if [[ "$marker_size" != "$SIZE_MB" ]]; then
      echo "挂载点已经初始化过，但镜像大小不一致："
      echo "  实际: ${marker_size:-未知} MiB"
      echo "  当前配置: ${SIZE_MB} MiB"
      echo "请把 features.container.workspace_size_mb 改回实际大小，或新建镜像并迁移；不要直接 resize 旧镜像。"
      exit 1
    fi
    echo "挂载点已就绪: $MOUNTPOINT（${marker_size} MiB）"
    exit 0
  fi
  echo "挂载点已被其他文件系统占用，拒绝覆盖: $MOUNTPOINT"
  exit 1
fi

if [[ -e "$IMAGE" ]]; then
  actual_bytes=$(stat -c %s "$IMAGE")
  expected_bytes=$((SIZE_MB * 1024 * 1024))
  if [[ "$actual_bytes" -ne "$expected_bytes" ]]; then
    echo "已存在镜像大小 $actual_bytes 与配置 ${expected_bytes} 不一致；请新建镜像并迁移。"
    exit 1
  fi
else
  truncate -s "${SIZE_MB}M" "$IMAGE"
  mkfs.ext4 -F -m 0 -L karen-container "$IMAGE"
fi

mount -o loop "$IMAGE" "$MOUNTPOINT"
chown -R 1000:1000 "$MOUNTPOINT"
python3 - "$IMAGE" "$SIZE_MB" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

image, size_mb = Path(sys.argv[1]), int(sys.argv[2])
marker = image.parent / ".karen-container-storage.json"
with open(marker, "w", encoding="utf-8") as f:
    json.dump(
        {
            "mode": "image_fixed",
            "size_mb": size_mb,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        f,
        ensure_ascii=False,
        indent=2,
    )
    f.write("\n")
PY

echo "C 存储已就绪: $MOUNTPOINT"
echo "重启后需要重新挂载；可加入 /etc/fstab："
echo "$IMAGE $MOUNTPOINT ext4 loop,defaults,noatime 0 0"
echo "扩容需要新建更大镜像并迁移，不要直接 resize 后继续使用旧标记。"
