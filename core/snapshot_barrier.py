"""严格迁移快照的写屏障协议。

这不是一个可单独使用的“假屏障”：实现必须真正停止新迁移/GC/删除/配置写入，
结算当前最小 item/事务，并暂停或可靠缓冲入站，然后才返回 ``SnapshotMarker``。
没有可信维护控制器时，调用方不应构造 ``migration_snapshot``；用户导出
（user_export）不要求屏障，也不得冒充迁移安全。

本模块只定义接口与不可变标记，不启动/停止 Runtime，不修改 systemd。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class BarrierError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SnapshotMarker:
    """屏障生效后由维护控制器签发的快照水位。"""

    epoch: int
    generation: int
    high_water: int = 0
    contract_epoch: int = 0
    migration_set_id: str = ""
    features: frozenset[str] = field(default_factory=frozenset)
    migration_safe: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def manifest_fields(self) -> dict[str, Any]:
        return {
            "epoch": int(self.epoch),
            "generation": int(self.generation),
            "high_water": int(self.high_water),
            "contract_epoch": int(self.contract_epoch),
            "migration_set_id": self.migration_set_id,
            "features": sorted(self.features),
            "migration_safe": bool(self.migration_safe),
            # details 只允许水位/计数/路径类别，不能放密钥或正文；这里仍做浅拷贝。
            "details": dict(self.details),
        }


class SnapshotBarrier(ABC):
    """可信维护控制器实现的写屏障。"""

    @abstractmethod
    def freeze(self) -> SnapshotMarker:
        """停止/缓冲所有相关写入并结算后返回水位。"""

    @abstractmethod
    def release(self, marker: SnapshotMarker) -> None:
        """快照发布或失败后释放屏障；必须幂等。"""


__all__ = ["BarrierError", "SnapshotBarrier", "SnapshotMarker"]
