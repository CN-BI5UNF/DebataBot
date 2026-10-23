"""WebUI 后台更新检查。

需求是“每隔 9 天检测一次”，但应用不一定连续运行 9 天：
    - 启动后延迟 90 秒做第一次检查（错过上次到期时间时立即补查）；
    - 之后每 30 分钟看一次 update_state.json，只在真正到期时才联网；
    - 手动检查会重置下一次到期时间；
    - 失败后 6 小时重试，成功后再排到 9 天后。

不把网络检查放进请求路径；总览绿标只读缓存状态。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .api.update import service_for_host

logger = logging.getLogger(__name__)

INITIAL_DELAY_SECONDS = 90.0
POLL_SECONDS = 30 * 60.0


async def run_update_watchdog(
    host: Any,
    *,
    initial_delay: float = INITIAL_DELAY_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> None:
    try:
        await asyncio.sleep(max(0.0, float(initial_delay)))
    except asyncio.CancelledError:
        return

    while True:
        try:
            service = service_for_host(host)
            if service is not None:
                result = await service.auto_check_if_due()
                if result.get("error"):
                    logger.warning(
                        "自动检查更新失败：%s",
                        result.get("message") or result.get("error"),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 自动检查失败不能影响 WebUI/Runtime；下次轮询再试。
            logger.warning("自动更新检查异常", exc_info=True)
        try:
            await asyncio.sleep(max(60.0, float(poll_seconds)))
        except asyncio.CancelledError:
            return


__all__ = ["INITIAL_DELAY_SECONDS", "POLL_SECONDS", "run_update_watchdog"]
