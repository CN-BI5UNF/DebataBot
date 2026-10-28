"""记忆总结 Agent —— 历史超长时调用小模型提取重要信息并截断。

V1 行为：
    当 history >= SUMMARIZE_AT（默认 20000）时触发：
    1. 取前 SUMMARIZE_RANGE_END（默认 11000）条 + 现存重要记忆
    2. 让模型选 cut_point（在 [range_start, range_end] 之间的语义完整点）
    3. 提取新增重要信息
    4. 调用方据此调用 history.truncate_head(cut_point) + important.replace_all(new_important)

V2 改进：
    - 总结的提示词模板独立可调
    - 与 LLM 解耦：summarize() 返回 dict，由调用方持久化
    - 错误处理更明确
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app_config.schema import AgentConfig, SummarizeConfig
from providers.base import IProvider, ProviderError, ReasoningConfig
from utils.token_budget import TokenEstimator

from .base import StatusCallback, UsageRecorder

logger = logging.getLogger(__name__)


class SummaryAgent:
    """对话历史总结器。"""

    def __init__(
        self,
        provider: IProvider,
        cfg: AgentConfig,
        summarize_cfg: SummarizeConfig,
        *,
        usage_recorder: UsageRecorder | None = None,
        status_callback: StatusCallback | None = None,
        prompt_budget_tokens: int | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.summarize_cfg = summarize_cfg
        self.usage_recorder = usage_recorder
        self.status_callback = status_callback
        self.prompt_budget_tokens = (
            int(prompt_budget_tokens) if prompt_budget_tokens is not None else None
        )

    async def summarize_rolling(
        self,
        history_slice: list[dict[str, Any]],
        existing_summary_text: str,
        existing_important_text: str,
        *,
        chunk_index: int = 0,
        total_chunks: int = 1,
    ) -> dict[str, Any] | None:
        """把一段旧历史并入全局滚动摘要。

        S01 升级：
        1. 使用 build_safe_summary_prompt 将历史材料严格数据化，tools=None 隔离。
        2. 使用 validate_and_repair_summary_json 校验 10 字段与空/拒绝/无进展，
           对轻微格式瑕疵做有限修复。
        3. 失败仅记录结构化状态码与 hash，杜绝泄露聊天正文。
        4. 返回包含 summary_text, new_important 以及完整 structured 字典。
        """
        if not history_slice:
            return None

        from core.summary_chunker import (
            SummaryValidationError,
            build_safe_summary_prompt,
            validate_and_repair_summary_json,
        )

        prompt = build_safe_summary_prompt(
            history_slice,
            existing_summary_text,
            existing_important_text,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )

        budget = self.prompt_budget_tokens
        if budget is None:
            from providers.model_capabilities import model_capability

            cap = model_capability(str(getattr(self.cfg, "provider", "")), str(self.cfg.model))
            if cap and cap.context_length:
                budget = max(1024, int(cap.context_length) - int(getattr(self.cfg, "max_tokens", 4096) or 4096))
            else:
                budget = 32768

        summary_messages = [
            {
                "role": "system",
                "content": "你是当前角色的记忆管理系统。负责滚动压缩旧对话。",
            },
            {"role": "user", "content": prompt},
        ]
        estimated = TokenEstimator(model=self.cfg.model).estimate_prompt(
            summary_messages,
            None,
        )
        if estimated > budget:
            logger.warning(
                "滚动摘要输入超预算，拒绝调用 provider：estimated=%s budget=%s",
                estimated,
                budget,
            )
            self._emit_status("error", "滚动摘要输入超预算")
            return None

        try:
            self._emit_status("thinking", "滚动摘要生成中")
            result = await self.provider.chat_completion(
                [
                    {
                        "role": "system",
                        "content": "你是当前角色的记忆管理系统。负责滚动压缩旧对话。",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=self.cfg.max_tokens,
                reasoning=self._to_provider_reasoning(),
                stream=True,
                timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
            )
            await self._record_usage(result.usage, operation="rolling_summary")
        except ProviderError as e:
            logger.error("滚动摘要失败（API）: %s", e)
            self._emit_status("error", "滚动摘要失败")
            return None
        except Exception as e:
            logger.exception("滚动摘要异常: %s", e)
            self._emit_status("error", "滚动摘要异常")
            return None

        raw_content = result.content or ""
        try:
            validated = validate_and_repair_summary_json(
                raw_content,
                base_summary_text=existing_summary_text,
            )
            return {
                "summary_text": validated.summary_text,
                "new_important": validated.new_important,
                "structured": validated.as_dict(),
            }
        except SummaryValidationError as err:
            digest = hashlib.sha256(raw_content.encode("utf-8", errors="replace")).hexdigest()[:12]
            logger.warning(
                "滚动摘要结构化校验失败: code=%s message=%s content_len=%d digest=%s",
                err.code,
                err.message,
                len(raw_content),
                digest,
            )
            self._emit_status("error", f"滚动摘要校验失败: {err.code}")
            return None
        except Exception as err:
            logger.error("滚动摘要校验未知异常: %s", err)
            self._emit_status("error", "滚动摘要校验异常")
            return None

    async def summarize(
        self,
        history_slice: list[dict[str, Any]],
        existing_important_text: str,
    ) -> dict[str, Any] | None:
        """返回 {"cut_point": int, "new_important": [{"timestamp", "content"}, ...]} 或 None。"""
        if not history_slice:
            return None

        history_text = "\n".join(
            f"[{m.get('role', '?')}] {m.get('content', '') or ''}"
            for m in history_slice
        )
        range_start = self.summarize_cfg.range_start_messages
        range_end = self.summarize_cfg.range_end_messages

        # 同上：静态规范在前（可缓存），易变数据在后。
        # 注意「前 N 条」这句话含 len(history_slice)，属易变内容，已挪到数据区。
        prompt = (
            "你是当前角色的记忆管理系统。\n\n"
            f"<任务>\n"
            f"1. 从对话历史中提取新增的重要信息（人名、关系、约定、秘密、偏好等），"
            f"不要存日常闲聊。\n"
            f"2. 检查历史中第 {range_start}~{range_end} 条之间，"
            f"找一个语义完整的位置（话说完、话题转换处）作为截断点。\n"
            f"3. 合并现存重要记忆和新提取的记忆，去重，形成新一份重要记忆。\n"
            f"</任务>\n\n"
            f"返回 JSON：\n"
            f"```json\n"
            f'{{"cut_point": <数字>, "new_important": '
            f'[{{"timestamp": "时间", "content": "一句话描述"}}, ...]}}\n'
            f"```\n\n"
            f"注意：cut_point 必须在 {range_start}~{range_end} 之间。\n\n"
            f"以下是该角色的前 {len(history_slice)} 条对话历史。\n\n"
            f"<现存重要记忆>\n{existing_important_text or '（无）'}\n</现存重要记忆>\n\n"
            f"<对话历史>\n{history_text}\n</对话历史>"
        )

        try:
            # 总结一次性吐 8k+ tokens，整体 timeout 比首 token 宽得多
            self._emit_status("thinking", "历史总结生成中")
            result = await self.provider.chat_completion(
                [
                    {
                        "role": "system",
                        "content": "你是当前角色的记忆管理系统。"
                        "从对话历史中提取重要信息并确定截断点。",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=self.cfg.max_tokens,
                reasoning=self._to_provider_reasoning(),
                stream=True,
                timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
            )
            await self._record_usage(result.usage, operation="history_summary")
        except ProviderError as e:
            logger.error(f"记忆总结失败（API）: {e}")
            self._emit_status("error", "历史总结失败")
            return None
        except Exception as e:
            logger.exception(f"记忆总结异常: {e}")
            self._emit_status("error", "历史总结异常")
            return None

        parsed = _parse_json_object(result.content or "")
        if not parsed:
            logger.error(f"总结返回无 JSON：{(result.content or '')[:200]}")
            return None

        cut_point = parsed.get("cut_point")
        if not isinstance(cut_point, int) or cut_point < 0:
            logger.warning(f"总结返回的 cut_point 无效: {cut_point}")
            return None
        # 限制在范围内
        cut_point = max(range_start, min(cut_point, range_end))

        new_important = parsed.get("new_important", [])
        if not isinstance(new_important, list):
            new_important = []

        return {
            "cut_point": cut_point,
            "new_important": new_important,
        }

    def _to_provider_reasoning(self) -> ReasoningConfig | None:
        if self.cfg.reasoning is None:
            return None
        return ReasoningConfig(
            enabled=self.cfg.reasoning.enabled,
            budget=self.cfg.reasoning.budget,
            max_tokens=self.cfg.reasoning.max_tokens,
        )

    async def _record_usage(self, usage, **metadata: Any) -> None:
        if self.usage_recorder is None:
            return
        try:
            await self.usage_recorder(
                usage,
                {
                    "provider": self.provider.name,
                    "model": self.cfg.model,
                    "agent": "历史总结",
                    **metadata,
                },
            )
        except Exception:
            logger.debug("记录总结模型用量失败", exc_info=True)

    def _emit_status(self, state: str, text: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(
                {
                    "state": state,
                    "text": text,
                    "model": self.cfg.model,
                    "agent": "历史总结",
                }
            )
        except Exception:
            logger.debug("更新总结模型状态失败", exc_info=True)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        logger.error(f"总结 JSON 解析失败: {e}, raw={text[start:end][:200]}")
        return None
    return parsed if isinstance(parsed, dict) else None


def _format_history_line(record: dict[str, Any]) -> str:
    role = record.get("role", "?")
    conversation_id = str(record.get("conversation_id") or "").strip()
    scope_hint = f" conversation_id={conversation_id}" if conversation_id else ""
    return f"[{role}{scope_hint}] {record.get('content', '') or ''}"
