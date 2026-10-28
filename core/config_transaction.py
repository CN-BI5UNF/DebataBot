"""C12-R: 配置事务、安全防护与敏感凭证补偿契约。

核心契约（第二阶段接续方案 2.6 第 21 节）：
1. 配置事务与并发写保护（Config Lock & Revision CAS，E39）：
   - 支持 config lock（异步+线程安全）；
   - 携带 expected_revision 进行乐观并发控制，防止旧前端页面覆盖新配置；
   - 唯一临时文件（unique tmp，`config.yaml.tmp.<uuid>`）+ fsync + 原子 replace，杜绝固定 tmp 冲突。
2. 安全合并（Safe Draft Merge）：
   - 1M / null / backup / update / storage 隐藏或未修改字段不被覆盖；
   - 空 key / blank secret 不删已有旧 key（空key不删旧key）。
3. 凭据 Staged 补偿（Staged Secrets Compensation）：
   - 临时暂存新凭据，待配置全流程校验并通过原子落盘后正式提交；
   - 校验或落盘失败时回滚暂存凭据，绝不损坏原有生产凭据。
4. API 安全屏障（CSRF / Origin / Lease，E40）：
   - 双重提交 CSRF Cookie + Header 校验；
   - Origin / Referer 域名来源白名单校验；
   - 备份导出高敏下载租约（Download Lease）：一次性消费或 5 分钟 TTL，强制 no-store 响应头，路径身份防逃逸。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_SECONDS = 300.0  # 5 分钟
_CSRF_SECRET = b"karen-csrf-protection-secret-v2.6"


class ConfigConflictError(RuntimeError):
    """配置并发冲突（版本落后或 revision 失配）。"""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"配置并发冲突: 预期版本 {expected}，实际当前版本 {actual}")
        self.expected = expected
        self.actual = actual


class ConfigSecurityError(RuntimeError):
    """安全校验失败（CSRF/Origin/Lease 异常）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def compute_config_revision(content: str | bytes) -> str:
    """计算配置内容的稳定指纹（Revision）。"""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()[:16]


class ConfigLockManager:
    """异步 + 线程安全的配置修改锁。"""

    def __init__(self) -> None:
        self._async_lock = asyncio.Lock()
        self._thread_lock = threading.Lock()

    async def acquire_async(self) -> None:
        await self._async_lock.acquire()

    def release_async(self) -> None:
        self._async_lock.release()

    def __enter__(self) -> None:
        self._thread_lock.acquire()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._thread_lock.release()


def safe_merge_config_draft(
    base: dict[str, Any],
    patch: dict[str, Any],
    *,
    protected_keys: frozenset[str] = frozenset(
        {"backup", "update", "storage", "1M", "secrets", "internal"}
    ),
) -> dict[str, Any]:
    """安全合并配置草稿。

    规则（C12-R）：
    1. 保留 base 中隐藏或未编辑的字段（特别是 protected_keys）；
    2. 空字符串或 None 的 api_key/key 绝不覆盖已有旧有效 key（空key不删旧key）；
    3. 递归字典深度合并，不整体替换子字典。
    """
    merged = deepcopy(base)

    for key, value in patch.items():
        if value is None:
            # None 不修改原值
            continue

        # 检查是否为空 key
        if (
            isinstance(value, str)
            and ("key" in key.lower() or "token" in key.lower())
            and not value.strip()
        ):
            # 空 key 忽略，保留原 key
            continue

        if key in protected_keys and key not in patch:
            # 保护键直接保留
            continue

        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = safe_merge_config_draft(
                merged[key],
                value,
                protected_keys=protected_keys,
            )
        else:
            merged[key] = deepcopy(value)

    return merged


def atomic_flush_replace_config(
    target_path: Path,
    content: str,
    *,
    expected_revision: str | None = None,
) -> str:
    """唯一的临时文件 + fsync + 原子 replace 落盘。

    防范 E35 / E39：杜绝固定 .yaml.tmp 并发冲突，确保持久化落盘与目录同步。
    返回新的 revision。
    """
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. 乐观并发校验 expected_revision
    if expected_revision is not None and target_path.exists():
        current_text = target_path.read_text(encoding="utf-8")
        actual_rev = compute_config_revision(current_text)
        if actual_rev != expected_revision:
            raise ConfigConflictError(expected=expected_revision, actual=actual_rev)

    # 2. 生成唯一的临时文件名
    unique_suffix = f".tmp.{uuid.uuid4().hex}"
    tmp_path = target_path.parent / f"{target_path.name}{unique_suffix}"

    try:
        # 3. 写入内容并强制磁盘同步 (fsync)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        # 4. 原子替换
        os.replace(tmp_path, target_path)

        # 5. 尝试同步父目录元数据 (POSIX)
        try:
            dir_fd = os.open(str(target_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass

        return compute_config_revision(content)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


@dataclass(slots=True)
class StagedSecretsManager:
    """暂存凭据补偿管理器。

    新凭据先写入 staged 缓冲区，全量配置应用成功后正式 commit；
    若失败则自动 rollback，绝不损坏旧凭据。
    """

    active_secrets: dict[str, str] = field(default_factory=dict)
    staged_secrets: dict[str, str] = field(default_factory=dict)

    def stage(self, key_id: str, secret_value: str) -> None:
        """暂存凭据。"""
        if secret_value and secret_value.strip():
            self.staged_secrets[key_id] = secret_value.strip()

    def commit(self) -> None:
        """正式提交暂存凭据至活跃凭据表。"""
        self.active_secrets.update(self.staged_secrets)
        self.staged_secrets.clear()

    def rollback(self) -> None:
        """放弃并清空所有暂存凭据。"""
        self.staged_secrets.clear()

    def get_effective(self, key_id: str) -> str | None:
        """优先从暂存中读取，次之从活跃中读取。"""
        return self.staged_secrets.get(key_id, self.active_secrets.get(key_id))


# ============================================================
# API 安全防护（CSRF, Origin, Download Lease）
# ============================================================


def generate_csrf_token(session_id: str) -> str:
    """生成带 HMAC 签名的 CSRF Token。"""
    timestamp = str(int(time.time()))
    payload = f"{session_id}:{timestamp}".encode()
    sig = hmac.new(_CSRF_SECRET, payload, hashlib.sha256).hexdigest()[:16]
    return f"{timestamp}:{sig}"


def validate_csrf_token(
    session_id: str,
    token: str | None,
    *,
    max_age_seconds: float = 3600.0,
) -> bool:
    """双重提交 Cookie/Header CSRF 校验。"""
    if not token or ":" not in token:
        return False
    try:
        ts_str, sig = token.split(":", 1)
        created_at = float(ts_str)
        if time.time() - created_at > max_age_seconds:
            return False  # 过期
        payload = f"{session_id}:{ts_str}".encode()
        expected_sig = hmac.new(_CSRF_SECRET, payload, hashlib.sha256).hexdigest()[:16]
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
        return False


def validate_origin_header(
    origin: str | None,
    referer: str | None,
    allowed_hosts: list[str],
) -> bool:
    """Origin 与 Referer 来源白名单校验。"""
    target = origin or referer
    if not target:
        # 允许无 Origin 的纯本地客户端 (CLI/Curl/本地测试)
        return True

    from urllib.parse import urlparse

    parsed = urlparse(target)
    hostname = (parsed.hostname or "").lower()
    port = parsed.port

    for allowed in allowed_hosts:
        allowed = allowed.lower()
        if allowed == "*":
            return True
        if ":" in allowed:
            a_host, a_port = allowed.split(":", 1)
            if hostname == a_host and (port is None or str(port) == a_port):
                return True
        elif hostname == allowed:
            return True

    return False


@dataclass(slots=True)
class DownloadLease:
    lease_token: str
    resource_id: str
    target_path: Path
    expires_at: float
    consumed: bool = False


class BackupLeaseManager:
    """高敏备份下载租约管理（防范 E40）。

    契约：
    - 一次性消费或 5 分钟 TTL；
    - 严格路径身份（path identity）验证，禁止路径穿越；
    - 响应头强制 Cache-Control: no-store。
    """

    def __init__(self, allowed_dir: Path) -> None:
        self.allowed_dir = Path(allowed_dir).resolve()
        self._leases: dict[str, DownloadLease] = {}
        self._lock = threading.Lock()

    def create_lease(
        self,
        resource_id: str,
        filename: str,
        *,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> str:
        """为指定的备份文件创建受控下载租约。"""
        # 严格路径检查，防范目录穿越
        safe_path = (self.allowed_dir / filename).resolve()
        try:
            safe_path.relative_to(self.allowed_dir)
        except ValueError as err:
            raise ConfigSecurityError(
                "path_traversal_blocked", "检测到非法路径穿越尝试"
            ) from err

        if not safe_path.is_file():
            raise ConfigSecurityError(
                "file_not_found", f"下载目标文件不存在: {filename}"
            )

        token = f"lease_{secrets.token_urlsafe(24)}"
        lease = DownloadLease(
            lease_token=token,
            resource_id=resource_id,
            target_path=safe_path,
            expires_at=time.time() + ttl_seconds,
        )

        with self._lock:
            # 清理过期租约
            now = time.time()
            self._leases = {k: v for k, v in self._leases.items() if v.expires_at > now and not v.consumed}
            self._leases[token] = lease

        return token

    def verify_and_consume_lease(self, token: str) -> tuple[Path, dict[str, str]]:
        """核验并一次性消费租约。返回物理路径与强制安全头。"""
        with self._lock:
            lease = self._leases.get(token)
            if not lease:
                raise ConfigSecurityError("invalid_lease", "下载租约无效或已被消费")

            if time.time() > lease.expires_at:
                del self._leases[token]
                raise ConfigSecurityError("lease_expired", "下载租约已超时过期")

            if lease.consumed:
                del self._leases[token]
                raise ConfigSecurityError("lease_already_consumed", "租约已被单次使用")

            lease.consumed = True
            target_path = lease.target_path
            del self._leases[token]

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
        return target_path, headers
