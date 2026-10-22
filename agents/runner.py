"""AgentRunner —— 通用的多轮工具循环执行框架。

替代旧版 llm_client.run_agent_loop。差异：
    - 直接面向 IProvider（不再绑死 DeepSeek）
    - 工具执行通过外部注入的 executor，避免循环依赖
    - 显式记录 reasoning 过程，方便 UI 可视化
    - finish_reason 结构化（替代旧版 final 字符串判定）

循环退出条件（保留 V1 语义）：
    1. AI 调用了 no_action → finish_reason='no_action'
    2. 所有非 no_action 工具显式允许成功后结束 → 'finish_after_success'
    3. AI 未调用工具，提示重试后仍不调用 → 'no_tool_after_retry'
    4. 工具循环提醒宽限用完 → 'tool_loop_finalized'，随后无工具收尾
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from app_config.schema import AgentConfig
from app_config.schema import ReasoningConfig as CfgReasoning
from providers.base import (
    CompletionResult,
    IProvider,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnsupportedError,
    ReasoningConfig,
    ToolCall,
    normalize_messages,
    safe_provider_error_text,
)
from utils.image_budget import assert_wire_request_fits
from utils.image_contracts import (
    ImageAssetError,
    ImageLimits,
    declared_image_assets,
)
from utils.model_records import ModelViewPolicy, project_model_record
from utils.token_budget import TokenEstimator
from utils.tool_result_state import no_action_succeeded, tool_result_blocks_completion

from .base import (
    AgentPromptBudgetExceeded,
    AgentRunResult,
    FinishReason,
    RuntimeEventCallback,
    StatusCallback,
    ToolExecutor,
    UsageRecorder,
)
from .context_builder import first_tail_block_index

logger = logging.getLogger(__name__)


# 兼容旧导出；runner 不再根据这个集合提前结束。
DEFAULT_NO_FEEDBACK_TOOLS: set[str] = {
    "save_important_memory",
    "update_important_memory",
    "delete_important_memory",
    "no_action",
    "send_poke",
    "set_msg_emoji_like",
    "set_friend_add_request",
    "set_group_add_request",
    "schedule_wakeup",
}

# 发送类工具需要把结果回填给模型；发送成功不等于本轮结束。
SEND_TOOL_NAMES: set[str] = {
    "send_private_messages",
    "send_group_message",
    "send_voice_message",
}

AUTO_FINISH_TOOL_NAMES: set[str] = {
    *SEND_TOOL_NAMES,
    "commit_send_attempt",
    "save_important_memory",
    "update_important_memory",
    "delete_important_memory",
    "schedule_wakeup",
    "tool_search",
}
"""发送成功后允许直接收尾的工具集合。

模型如果还想看结果/保存记忆/安排 wakeup，需要把这些调用和发送放在同一轮。
"""

LOOP_REMINDER_MESSAGE = (
    "你已连续多轮调用工具。请重新审视任务内容和执行情况，不要在同一个错误上反复无意义尝试；"
    "必要时更换方向、汇报进展或收尾。"
)


class AgentRunner:
    """多轮工具循环执行器。"""

    def __init__(
        self,
        provider: IProvider,
        agent_cfg: AgentConfig,
        *,
        no_feedback_tools: set[str] | None = None,
        runtime_event_callback: RuntimeEventCallback | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = agent_cfg
        self.no_feedback_tools = (
            no_feedback_tools if no_feedback_tools is not None else DEFAULT_NO_FEEDBACK_TOOLS
        )
        self.runtime_event_callback = runtime_event_callback
        self._kv_tools_diag_cache: dict[tuple[str, int], dict[str, Any]] = {}

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tool_executor: ToolExecutor,
        task_contract: str | None = None,
        pending_context_provider: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        max_loops: int | None = None,
        usage_recorder: UsageRecorder | None = None,
        status_callback: StatusCallback | None = None,
        runtime_event_callback: RuntimeEventCallback | None = None,
        status_label: str = "主模型",
        estimated_prompt_tokens: int = 0,
        calib_ratio: float = 1.0,
        kv_diagnostics_mode: str = "summary",
        auto_finish_after_send: bool = False,
        model_view_policy: ModelViewPolicy | None = None,
        image_limits: ImageLimits | None = None,
        provider_limit_bytes: int | None = None,
        prompt_budget_tokens: int | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> AgentRunResult:
        """执行多轮工具循环。

        Args:
            task_contract: 本轮的"任务合约"——一句话描述本轮目标。
                每 refocus_interval 轮重注入一次到 messages 末尾，防止焦点漂移。
            model_view_policy: 新工具结果/pending runtime message 的逐条投影策略。
                None 或 enabled=False 时保持旧流程；执行控制始终使用完整的
                raw result，投影只影响发给模型的副本。
        """
        policy = model_view_policy
        budget_estimator = token_estimator or TokenEstimator(
            model=str(getattr(self.cfg, "model", "") or "")
        )
        msgs = list(messages)
        records: list[dict[str, Any]] = []
        # 本轮稳定边界只在入口从结构证据取一次；后续工具结果/提醒只能 append。
        initial_stable_prefix_end = first_tail_block_index(msgs)

        def project_for_model(
            record: dict[str, Any],
            *,
            tool_name: str | None = None,
        ) -> dict[str, Any]:
            if policy is None or not policy.enabled:
                return record
            return project_model_record(record, policy=policy, tool_name=tool_name)

        def append_runtime_record(
            record: dict[str, Any],
            *,
            persist: bool = True,
        ) -> None:
            """运行态记录：模型看投影副本；需要落盘时 records 保存 raw。"""
            msgs.append(project_for_model(record))
            if persist:
                records.append(record)
        reasoning_logs: list[str] = []
        final_content = ""
        finish_reason: FinishReason = "no_response"
        loop_count = 0
        no_tool_retry_count = 0
        prompt_tokens_total = 0
        # 只记第一次调用的 prompt_tokens：它才是与上下文预检估算口径一致的那个
        # prompt。后续工具循环会把工具结果不断追加进来，累加值远大于预检估算，
        # 拿累加值做校准会把 ratio 推向 >1，反而让估算越来越离谱。
        first_prompt_tokens = 0
        refocus_interval = self.cfg.refocus_interval
        reminder_interval = max(1, int(self.cfg.tool_loop_reminder_interval))
        final_warning_count = max(1, int(self.cfg.tool_loop_final_warning_count))
        final_grace_loops = max(0, int(self.cfg.tool_loop_final_grace_loops))
        tool_rounds_since_reminder = 0
        tool_loop_reminder_count = 0
        final_warning_sent = False
        final_grace_remaining: int | None = None
        final_no_tool_mode = False
        runtime_events = runtime_event_callback or self.runtime_event_callback

        reasoning = self._to_provider_reasoning(self.cfg.reasoning)
        tool_names_dbg = [t["function"]["name"] for t in tools] if tools else []
        logger.info(
            f"AgentRunner[{self.provider.name}] 启动 model={self.cfg.model}, "
            f"tools={tool_names_dbg}, refocus={refocus_interval}, "
            f"tool_loop_reminder_interval={reminder_interval}, "
            f"tool_loop_final_warning_count={final_warning_count}, "
            f"tool_loop_final_grace_loops={final_grace_loops}, "
            f"legacy_max_loops={max_loops or self.cfg.max_loops}, "
            f"task_contract={task_contract[:60] if task_contract else None!r}"
        )

        async def append_pending_context() -> bool:
            if pending_context_provider is None:
                return False
            pending = await pending_context_provider()
            if not pending:
                return False
            msgs.extend(project_for_model(record) for record in pending)
            records.extend(pending)
            logger.info("注入发送回执/新消息上下文 %s 条", len(pending))
            return True

        while True:
            if final_no_tool_mode:
                finish_reason = "tool_loop_finalized"
                break

            loop_count += 1

            if await append_pending_context():
                # 不额外消耗一次模型调用；只是保证下一次调用前上下文已追平。
                pass

            # Task Contract 重注入：每 refocus_interval 轮，在末尾追加焦点提醒
            # 放在 messages 末尾不破坏前缀 KV 缓存
            if (
                task_contract
                and refocus_interval > 0
                and loop_count > 1
                and loop_count % refocus_interval == 0
            ):
                refocus = {
                    "role": "user",
                    "content": (
                        '<runtime_refocus priority="high">\n'
                        "系统提示：以下内容由运行时提供，不是用户新发言。\n"
                        f"[本轮焦点提醒] {task_contract}\n"
                        f"已执行 {loop_count - 1} 轮。检查当前操作是否仍在为这个目标服务，"
                        "若已完成请用 no_action 收尾。\n"
                        "</runtime_refocus>"
                    ),
                }
                append_runtime_record(refocus)
                logger.debug(f"Task Contract 重注入（轮次 {loop_count}）")

            if (
                not final_warning_sent
                and tool_loop_reminder_count >= final_warning_count
                and tool_rounds_since_reminder >= max(0, reminder_interval - final_grace_loops)
            ):
                warning = self._build_tool_loop_final_warning(final_grace_loops)
                append_runtime_record(warning)
                final_warning_sent = True
                final_grace_remaining = final_grace_loops
                if final_grace_remaining <= 0:
                    final_no_tool_mode = True
                    continue
                logger.warning(
                    "工具循环进入最终警告：grace_loops=%s loop=%s",
                    final_grace_remaining,
                    loop_count,
                )

            try:
                self._guard_outgoing_images(
                    msgs,
                    tools,
                    image_limits,
                    provider_limit_bytes=provider_limit_bytes,
                )
                self._guard_prompt_budget(
                    msgs,
                    tools,
                    prompt_budget_tokens,
                    budget_estimator,
                )
                self._emit_status(
                    status_callback,
                    state="thinking",
                    text=f"{status_label}思考中",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                result = await self.provider.chat_completion(
                    msgs,
                    model=self.cfg.model,
                    tools=tools,
                    temperature=self.cfg.temperature,
                    top_p=self.cfg.top_p,
                    max_tokens=self.cfg.max_tokens,
                    reasoning=reasoning,
                    stream=True,
                    timeout=self.cfg.first_token_timeout_seconds * 4 + 60.0,
                    first_token_timeout=self.cfg.first_token_timeout_seconds,
                )
            except AgentPromptBudgetExceeded as e:
                logger.error(
                    "AgentRunner 本地预算阻止发送（轮次 %s）：estimated=%s budget=%s",
                    loop_count,
                    e.estimated,
                    e.budget,
                )
                self._emit_status(
                    status_callback,
                    state="error",
                    text=f"{status_label}输入超出预算",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="budget_blocked",
                    reasoning_logs=reasoning_logs,
                    prompt_tokens=prompt_tokens_total,
                    first_prompt_tokens=first_prompt_tokens,
                )
            except ProviderTimeoutError as e:
                logger.error(
                    "AgentRunner 超时（轮次 %s）：%s",
                    loop_count,
                    safe_provider_error_text(e),
                )
                self._emit_status(
                    status_callback,
                    state="error",
                    text=f"{status_label}超时",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                    prompt_tokens=prompt_tokens_total,
                    first_prompt_tokens=first_prompt_tokens,
                )
            except ProviderError as e:
                logger.error(
                    "AgentRunner API 错误（轮次 %s）：%s",
                    loop_count,
                    safe_provider_error_text(e),
                )
                self._emit_status(
                    status_callback,
                    state="error",
                    text=f"{status_label}调用失败",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                    prompt_tokens=prompt_tokens_total,
                    first_prompt_tokens=first_prompt_tokens,
                )
            except Exception as e:
                logger.error(
                    "AgentRunner 未知错误（轮次 %s，type=%s）：%s",
                    loop_count,
                    type(e).__name__,
                    safe_provider_error_text(e),
                )
                self._emit_status(
                    status_callback,
                    state="error",
                    text=f"{status_label}异常",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                    prompt_tokens=prompt_tokens_total,
                    first_prompt_tokens=first_prompt_tokens,
                )

            if first_prompt_tokens <= 0:
                first_prompt_tokens = int(result.usage.prompt_tokens or 0)
            prompt_tokens_total += result.usage.prompt_tokens
            await self._record_usage(
                usage_recorder,
                result.usage,
                agent=status_label,
                operation="agent_loop",
                loop=loop_count,
                **_kv_prompt_diagnostics(
                    msgs,
                    tools,
                    loop=loop_count,
                    model=self.cfg.model,
                    mode=kv_diagnostics_mode,
                    tools_diag_cache=self._kv_tools_diag_cache,
                    stable_prefix_end=initial_stable_prefix_end,
                ),
                **self._token_estimate_metadata(
                    estimated_prompt_tokens,
                    result.usage.prompt_tokens,
                    calib_ratio,
                    loop=loop_count,
                ),
            )
            if result.reasoning_content:
                reasoning_logs.append(result.reasoning_content)

            content_preview = (result.content or "")[:60]
            logger.info(
                f"轮次 {loop_count}: content={content_preview!r}, "
                f"tool_calls={len(result.tool_calls)}"
            )

            # === 分支 1：无工具调用 → 引导重试或终止 ===
            if not result.tool_calls:
                if await append_pending_context():
                    continue
                if no_tool_retry_count <= 0:
                    # 允许一次纠正重试：丢弃纯文本草稿，只插入系统纠正后继续。
                    # 不能把无效 assistant 文本放回上下文，否则下一轮可能把
                    # 内部分析/RAG 解释原样当作可发送消息。
                    no_tool_retry_count += 1
                    err = {
                        "role": "user",
                        "content": (
                            '<runtime_correction priority="high">\n'
                            "系统提示：以下内容由运行时提供，不是用户新发言。\n"
                            "错误：未调用工具。必须调用 send_* 发消息或 no_action 不操作。"
                            "上一轮纯文本已被系统丢弃，不要复述、转发或引用它。\n"
                            "</runtime_correction>"
                        ),
                    }
                    append_runtime_record(err)
                    continue
                # 重试后仍未调用工具，丢弃文本
                final_content = ""
                finish_reason = "no_tool_after_retry"
                break

            no_tool_retry_count = 0

            assistant_record = self._build_assistant_record(result)
            msgs.append(assistant_record)
            records.append(assistant_record)

            # === 分支 2：执行所有工具调用 ===
            self._emit_status(
                status_callback,
                state="tool",
                text=f"调用工具：{', '.join(tc.name for tc in result.tool_calls)}",
                model=self.cfg.model,
                loop=loop_count,
                tool_names=[tc.name for tc in result.tool_calls],
            )
            tc_results = await self._execute_tools(
                result.tool_calls,
                tool_executor,
                runtime_event_callback=runtime_events,
                loop=loop_count,
            )

            stop_results = [
                r for r in tc_results if r["result"].get("stop_after_tool")
            ]
            no_action_results = [
                r for r in tc_results if r["name"] == "no_action"
            ]
            blocked_by_result = any(
                self._tool_result_blocks_completion(r["result"]) for r in tc_results
            )
            # 只有“整批都是明确成功的 no_action”才允许安静结束；混合批次不能
            # 因为其中一个 no_action 成功就掩盖其它工具失败/待处理。
            only_no_action_success = bool(
                no_action_results
                and len(no_action_results) == len(tc_results)
                and not blocked_by_result
                and all(
                    no_action_succeeded(r["result"]) for r in no_action_results
                )
            )
            finish_after_success = (
                not blocked_by_result
                and self._all_non_no_action_results_allow_completion(tc_results)
            )
            auto_finish_after_send_result = (
                auto_finish_after_send
                and not blocked_by_result
                and self._should_auto_finish_after_send(tc_results)
            )
            normal_finish = bool(
                stop_results
                or only_no_action_success
                or finish_after_success
                or auto_finish_after_send_result
            )

            append_final_warning_after_tool_records = False
            if not normal_finish:
                tool_rounds_since_reminder += 1
                if final_grace_remaining is not None:
                    final_grace_remaining -= 1
                    if final_grace_remaining <= 0:
                        final_no_tool_mode = True
                elif (
                    final_grace_loops <= 0
                    and not final_warning_sent
                    and tool_loop_reminder_count >= final_warning_count
                    and tool_rounds_since_reminder >= reminder_interval
                ):
                    final_warning_sent = True
                    final_grace_remaining = 0
                    final_no_tool_mode = True
                    append_final_warning_after_tool_records = True
                if (
                    not final_warning_sent
                    and tool_rounds_since_reminder >= reminder_interval
                ):
                    tool_loop_reminder_count += 1
                    tool_rounds_since_reminder = 0
                    self._append_loop_reminder(
                        tc_results,
                        reminder_interval=reminder_interval,
                        reminder_count=tool_loop_reminder_count,
                        final_warning_count=final_warning_count,
                    )

            for tcr in tc_results:
                raw_tool_record = {
                    "role": "tool",
                    "tool_call_id": tcr["id"],
                    "content": json.dumps(tcr["result"], ensure_ascii=False),
                }
                # 执行控制/落盘 history 使用完整 raw result；模型只看到投影副本。
                msgs.append(
                    project_for_model(raw_tool_record, tool_name=tcr["name"])
                )
                records.append(raw_tool_record)

            if append_final_warning_after_tool_records:
                warning = self._build_tool_loop_final_warning(final_grace_loops)
                append_runtime_record(warning)
                logger.warning(
                    "工具循环进入最终警告：grace_loops=%s loop=%s",
                    final_grace_remaining,
                    loop_count,
                )

            if stop_results:
                final_content = result.content or ""
                finish_reason = "tool_stop"
                break

            if await append_pending_context():
                continue

            # === 终止条件检查 ===
            if auto_finish_after_send_result:
                final_content = result.content or ""
                finish_reason = "finish_after_send"
                break

            if only_no_action_success:
                final_content = "NO_ACTIONS"
                finish_reason = "no_action"
                break

            if blocked_by_result:
                continue

            if finish_after_success:
                final_content = result.content or ""
                finish_reason = "finish_after_success"
                break

            if final_no_tool_mode:
                finish_reason = "tool_loop_finalized"
                break

        if finish_reason == "tool_loop_finalized":
            logger.warning("工具循环最终宽限已用完，进入无工具收尾")
            stop_record = self._build_tool_loop_stop()
            append_runtime_record(stop_record)
            final_result = await self._finalize_tool_loop(
                msgs,
                usage_recorder=usage_recorder,
                status_callback=status_callback,
                status_label=status_label,
                kv_diagnostics_mode=kv_diagnostics_mode,
                stable_prefix_end=initial_stable_prefix_end,
                image_limits=image_limits,
                provider_limit_bytes=provider_limit_bytes,
                prompt_budget_tokens=prompt_budget_tokens,
                token_estimator=budget_estimator,
            )
            if final_result is not None:
                prompt_tokens_total += final_result.usage.prompt_tokens
                if final_result.reasoning_content:
                    reasoning_logs.append(final_result.reasoning_content)
                final_record = self._build_assistant_record(final_result)
                records.append(final_record)
                final_content = (final_result.content or "").strip()
            if not final_content:
                final_content = self._last_assistant_text(records)

        self._emit_status(
            status_callback,
            state="idle",
            text="空闲",
            model=self.cfg.model,
            loop=loop_count,
            finish_reason=finish_reason,
        )
        return AgentRunResult(
            final_content=final_content,
            records=records,
            loop_count=loop_count,
            finish_reason=finish_reason,
            reasoning_logs=reasoning_logs,
            prompt_tokens=prompt_tokens_total,
            first_prompt_tokens=first_prompt_tokens,
        )

    # ============================================================
    # 内部辅助
    # ============================================================

    @staticmethod
    def _to_provider_reasoning(
        cfg: CfgReasoning | None,
    ) -> ReasoningConfig | None:
        if cfg is None:
            return None
        return ReasoningConfig(
            enabled=cfg.enabled,
            budget=cfg.budget,
            max_tokens=cfg.max_tokens,
        )

    @staticmethod
    def _build_assistant_record(result: CompletionResult) -> dict[str, Any]:
        record: dict[str, Any] = {
            "role": "assistant",
            "content": result.content or "",
        }
        if result.reasoning_content or result.reasoning_blocks:
            record["reasoning_content"] = result.reasoning_content
        if result.reasoning_blocks:
            record["reasoning_blocks"] = result.reasoning_blocks
        if result.tool_calls:
            record["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in result.tool_calls
            ]
        return record

    @staticmethod
    def _last_assistant_text(records: list[dict[str, Any]]) -> str:
        for record in reversed(records):
            if record.get("role") == "assistant":
                content = str(record.get("content") or "").strip()
                if content:
                    return content
        return ""

    @staticmethod
    def _token_estimate_metadata(
        estimated_prompt_tokens: int,
        actual_prompt_tokens: int,
        calib_ratio: float,
        *,
        loop: int,
    ) -> dict[str, Any]:
        """把预检估算与真实用量的对照写进 usage 记录。

        只有第一次调用（loop=1）的估算和实际口径一致；工具循环后续轮次
        的 prompt 已经增长，不能拿来算误差。
        """
        if estimated_prompt_tokens <= 0:
            return {}
        metadata: dict[str, Any] = {
            "estimated_prompt_tokens": int(estimated_prompt_tokens),
            "calib_ratio": round(float(calib_ratio), 4),
        }
        if loop == 1 and actual_prompt_tokens > 0:
            metadata["prompt_estimate_ratio"] = round(
                actual_prompt_tokens / estimated_prompt_tokens,
                4,
            )
            metadata["prompt_estimate_error_tokens"] = int(
                actual_prompt_tokens - estimated_prompt_tokens
            )
        return metadata

    async def _record_usage(
        self,
        recorder: UsageRecorder | None,
        usage,
        **metadata: Any,
    ) -> None:
        if recorder is None:
            return
        try:
            await recorder(
                usage,
                {
                    "provider": self.provider.name,
                    "model": self.cfg.model,
                    **metadata,
                },
            )
        except Exception:
            logger.debug("记录模型用量失败", exc_info=True)

    @staticmethod
    def _append_loop_reminder(
        tc_results: list[dict[str, Any]],
        *,
        reminder_interval: int,
        reminder_count: int,
        final_warning_count: int,
    ) -> None:
        if not tc_results:
            return
        result = dict(tc_results[-1]["result"])
        result["loop_reminder"] = {
            "level": "reminder",
            "message": LOOP_REMINDER_MESSAGE,
            "tool_loop_reminder_interval": reminder_interval,
            "reminder_count": reminder_count,
            "final_warning_count": final_warning_count,
        }
        tc_results[-1]["result"] = result

    @staticmethod
    def _build_tool_loop_final_warning(grace_loops: int) -> dict[str, Any]:
        return {
            "role": "user",
            "content": (
                '<tool_loop_final_warning priority="high">\n'
                "系统提示：工具调用轮数过多，即将结束循环。\n"
                f"你还有 {grace_loops} 轮工具调用机会。\n"
                "请停止反复尝试同一错误，利用剩余机会完成必要操作、汇报当前结果并收尾。\n"
                "</tool_loop_final_warning>"
            ),
        }

    @staticmethod
    def _build_tool_loop_stop() -> dict[str, Any]:
        return {
            "role": "user",
            "content": (
                '<tool_loop_stop priority="high">\n'
                "系统提示：工具调用机会已用完。\n"
                "请不要再调用工具。基于已有结果完成最终汇报、说明未完成原因，"
                "或在无需回复时结束。\n"
                "</tool_loop_stop>"
            ),
        }

    @staticmethod
    def _guard_prompt_budget(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        budget_tokens: int | None,
        estimator: TokenEstimator,
    ) -> None:
        if budget_tokens is None:
            return
        estimated = int(estimator.estimate_prompt(messages, tools))
        if estimated > int(budget_tokens):
            raise AgentPromptBudgetExceeded(estimated, int(budget_tokens))

    @staticmethod
    def _guard_outgoing_images(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        limits: ImageLimits | None,
        *,
        provider_limit_bytes: int | None = None,
    ) -> None:
        """每次 provider 网络调用前的最后多媒体护栏。

        纯文字请求直接放行；含图请求在数量/结构/编码字节/JSON 总量超限时
        抛出 ProviderUnsupportedError，保证不发出超限 payload。
        """
        if limits is None:
            return
        try:
            assert_wire_request_fits(
                messages,
                tools_schema=tools,
                limits=limits,
                provider_limit_bytes=provider_limit_bytes,
            )
        except ImageAssetError as exc:
            logger.error(
                "provider 请求图片护栏拒绝发送 code=%s",
                exc.code,
            )
            raise ProviderUnsupportedError(
                f"请求图片资源超出限制：{exc.safe_message()}"
            ) from None

    async def _finalize_tool_loop(
        self,
        messages: list[dict[str, Any]],
        *,
        usage_recorder: UsageRecorder | None,
        status_callback: StatusCallback | None,
        status_label: str,
        kv_diagnostics_mode: str = "summary",
        stable_prefix_end: int | None = None,
        image_limits: ImageLimits | None = None,
        provider_limit_bytes: int | None = None,
        prompt_budget_tokens: int | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> CompletionResult | None:
        """工具循环最终宽限用尽后做一次无工具收尾。

        这一步不再传 tools，避免模型继续循环；结果只作为记录和上层 fallback 使用。
        """
        try:
            self._guard_outgoing_images(
                messages,
                None,
                image_limits,
                provider_limit_bytes=provider_limit_bytes,
            )
            self._guard_prompt_budget(
                messages,
                None,
                prompt_budget_tokens,
                token_estimator or TokenEstimator(model=str(self.cfg.model or "")),
            )
            self._emit_status(
                status_callback,
                state="thinking",
                text=f"{status_label}整理部分结果",
                model=self.cfg.model,
            )
            result = await self.provider.chat_completion(
                messages,
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=min(
                    int(self.cfg.max_tokens or self.cfg.tool_loop_final_max_tokens),
                    int(self.cfg.tool_loop_final_max_tokens),
                ),
                reasoning=self._to_provider_reasoning(self.cfg.reasoning),
                stream=True,
                timeout=self.cfg.first_token_timeout_seconds * 4 + 60.0,
                first_token_timeout=self.cfg.first_token_timeout_seconds,
            )
            await self._record_usage(
                usage_recorder,
                result.usage,
                agent=status_label,
                operation="agent_loop_tool_loop_final",
                **_kv_prompt_diagnostics(
                    messages,
                    None,
                    loop=0,
                    model=self.cfg.model,
                    mode=kv_diagnostics_mode,
                    tools_diag_cache=self._kv_tools_diag_cache,
                    stable_prefix_end=stable_prefix_end,
                ),
            )
            return result
        except Exception as e:
            logger.warning(
                "AgentRunner 工具循环无工具收尾失败: %s",
                safe_provider_error_text(e),
            )
            self._emit_status(
                status_callback,
                state="error",
                text=f"{status_label}收尾失败",
                model=self.cfg.model,
            )
            return None

    @staticmethod
    def _emit_status(callback: StatusCallback | None, **payload: Any) -> None:
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            logger.debug("更新模型状态失败", exc_info=True)

    async def _execute_tools(
        self,
        tool_calls: list[ToolCall],
        executor: ToolExecutor,
        *,
        runtime_event_callback: RuntimeEventCallback | None = None,
        loop: int | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for step, tc in enumerate(tool_calls, start=1):
            name = tc.name
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
                if not isinstance(args, dict):
                    raise ValueError(f"工具参数必须是对象: {tc.arguments!r}")
            except (json.JSONDecodeError, ValueError) as e:
                result = _build_runner_tool_error_result(
                    name,
                    brief="工具参数解析失败。",
                    error=f"参数解析失败: {e}",
                )
                await self._emit_runtime_event(
                    runtime_event_callback,
                    _build_tool_result_event(
                        tc,
                        args=None,
                        result=result,
                        error_type=type(e).__name__,
                        loop=loop,
                        step=step,
                    ),
                )
                results.append({"id": tc.id, "name": name, "args": {}, "result": result})
                continue

            await self._emit_runtime_event(
                runtime_event_callback,
                _build_tool_started_event(tc, args, loop=loop, step=step),
            )
            try:
                if _executor_accepts_tool_call_id(executor):
                    result = await executor(name, args, tool_call_id=tc.id)
                else:
                    result = await executor(name, args)
                if not isinstance(result, dict):
                    result = {"ok": True, "value": result}
            except Exception as e:
                logger.error(
                    "工具 %s 执行失败（type=%s）：%s",
                    name,
                    type(e).__name__,
                    safe_provider_error_text(e),
                )
                result = _build_runner_tool_error_result(
                    name,
                    brief="工具执行失败。",
                    error=str(e),
                )
                error_type = type(e).__name__
            else:
                error_type = None

            result = self._maybe_mark_turn_completion(name, args, result)
            await self._emit_runtime_event(
                runtime_event_callback,
                _build_tool_result_event(
                    tc,
                    args=args,
                    result=result,
                    error_type=error_type,
                    loop=loop,
                    step=step,
                ),
            )
            results.append({"id": tc.id, "name": name, "args": args, "result": result})
        return results

    @staticmethod
    async def _emit_runtime_event(
        callback: RuntimeEventCallback | None,
        event: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        maybe_awaitable = callback(event)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    @staticmethod
    def _maybe_mark_turn_completion(
        name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "no_action" or args.get("finish_after_success") is not True:
            return result
        if AgentRunner._tool_result_blocks_completion(result):
            return result
        marked = dict(result)
        completion = dict(marked.get("turn_completion") or {})
        completion["allowed"] = True
        completion.setdefault("reason", "finish_after_success")
        marked["turn_completion"] = completion
        return marked

    @staticmethod
    def _result_is_successful_send(result: dict[str, Any]) -> bool:
        if tool_result_blocks_completion(result):
            return False
        if result.get("qq_visible") is True or result.get("accepted") is True:
            return True
        status = str(result.get("status") or "").strip().lower()
        return status in {"sent", "accepted", "done", "ok", "success"}

    @staticmethod
    def _should_auto_finish_after_send(tc_results: list[dict[str, Any]]) -> bool:
        """发送成功且本轮没有其它待执行动作时，直接收尾。

        条件严格限制在发送/提交/记忆类工具；只要模型还调用了查询或人格动作，
        就继续循环，避免吞掉发送后的观察需求。
        """
        if not tc_results:
            return False
        names = {str(item.get("name") or "") for item in tc_results}
        if not names or not names <= AUTO_FINISH_TOOL_NAMES:
            return False
        send_results = [
            item["result"]
            for item in tc_results
            if item.get("name") in SEND_TOOL_NAMES
            or item.get("name") == "commit_send_attempt"
        ]
        if not send_results:
            return False
        return any(
            AgentRunner._result_is_successful_send(result)
            for result in send_results
        )

    @staticmethod
    def _tool_result_blocks_completion(result: dict[str, Any]) -> bool:
        # 与 tools.base 共用同一套谓词，避免一个认为成功、另一个要求重试。
        return tool_result_blocks_completion(result)

    @staticmethod
    def _all_non_no_action_results_allow_completion(
        tc_results: list[dict[str, Any]],
    ) -> bool:
        non_no_action = [r for r in tc_results if r["name"] != "no_action"]
        if not non_no_action:
            return False
        return all(
            r["result"].get("turn_completion", {}).get("allowed") is True
            for r in non_no_action
        )


def _build_runner_tool_error_result(
    tool_name: str,
    *,
    brief: str,
    error: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "tool": tool_name,
        "result_format": "structured_json",
        "brief": brief,
        "error": error,
    }


def _build_tool_started_event(
    tc: ToolCall,
    args: dict[str, Any],
    *,
    loop: int | None,
    step: int,
) -> dict[str, Any]:
    payload = {
        "tool_call_id": tc.id,
        "tool_name": tc.name,
        "args": args,
        "raw_arguments": tc.arguments,
        **_tool_args_summary(args=args, raw_arguments=tc.arguments),
    }
    _add_loop_step(payload, loop=loop, step=step)
    return {
        "event_type": "tool_call_started",
        "source": "agent_runner",
        "tool_call_id": tc.id,
        "payload": payload,
    }


def _build_tool_result_event(
    tc: ToolCall,
    *,
    args: dict[str, Any] | None,
    result: dict[str, Any],
    error_type: str | None,
    loop: int | None,
    step: int,
) -> dict[str, Any]:
    payload = {
        "tool_call_id": tc.id,
        "tool_name": tc.name,
        "raw_arguments": tc.arguments,
        "result": result,
        **_tool_args_summary(args=args, raw_arguments=tc.arguments),
        **_tool_result_summary(result, error_type=error_type),
    }
    if args is not None:
        payload["args"] = args
    _add_loop_step(payload, loop=loop, step=step)
    return {
        "event_type": "tool_result_received",
        "source": "agent_runner",
        "tool_call_id": tc.id,
        "payload": payload,
    }


def _tool_args_summary(
    *,
    args: dict[str, Any] | None,
    raw_arguments: str,
) -> dict[str, Any]:
    if args is None:
        args_length = len(raw_arguments or "")
        args_keys: list[str] = []
        args_key_count = 0
        args_hash = _short_hash(raw_arguments or "")
    else:
        text = _stable_json_text(args)
        args_keys, args_key_count = _limited_keys(args)
        args_length = len(text)
        args_hash = _short_hash(text)
    return {
        "args_hash": args_hash,
        "args_keys": args_keys,
        "args_key_count": args_key_count,
        "args_length": args_length,
        "args_preview": _summary_preview(
            "args",
            keys=args_keys,
            key_count=args_key_count,
            length=args_length,
        ),
    }


def _tool_result_summary(
    result: dict[str, Any],
    *,
    error_type: str | None,
) -> dict[str, Any]:
    text = _stable_json_text(result)
    result_keys, result_key_count = _limited_keys(result)
    payload: dict[str, Any] = {
        "result_hash": _short_hash(text),
        "result_keys": result_keys,
        "result_key_count": result_key_count,
        "result_length": len(text),
        "result_preview": _summary_preview(
            "result",
            keys=result_keys,
            key_count=result_key_count,
            length=len(text),
        ),
    }
    ok = result.get("ok")
    if isinstance(ok, bool):
        payload["ok"] = ok
    status = _light_status(result.get("status"))
    if status is not None:
        payload["status"] = status
    event_error_type = _event_error_type(result, error_type)
    if event_error_type:
        payload["error_type"] = event_error_type
    return payload


def _limited_keys(value: dict[str, Any]) -> tuple[list[str], int]:
    keys = sorted(str(key) for key in value.keys())
    return keys[:20], len(keys)


def _summary_preview(
    kind: str,
    *,
    keys: list[str],
    key_count: int,
    length: int,
) -> str:
    joined = ",".join(keys) if keys else "-"
    suffix = ",..." if key_count > len(keys) else ""
    return f"{kind}: keys={joined}{suffix}; length={length}"[:80]


def _stable_json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _light_status(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:80]
    if isinstance(value, list | tuple | set | frozenset):
        return [str(item)[:40] for item in list(value)[:5]]
    return type(value).__name__


def _event_error_type(result: dict[str, Any], explicit: str | None) -> str | None:
    if explicit:
        return str(explicit)[:80]
    value = result.get("error_type")
    if value:
        return str(value)[:80]
    if result.get("ok") is False and result.get("error"):
        return "tool_error"
    return None


def _add_loop_step(payload: dict[str, Any], *, loop: int | None, step: int) -> None:
    if loop is not None:
        payload["loop"] = loop
    payload["step"] = step


def _kv_prompt_diagnostics(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    loop: int,
    model: str = "",
    mode: str = "full",
    tools_diag_cache: dict[tuple[str, int], dict[str, Any]] | None = None,
    stable_prefix_end: int | None = None,
) -> dict[str, Any]:
    """生成轻量 KV 诊断信息，只记录结构和统计，不记录正文。

    模式：
        off      —— 只返回模式标记，零序列化。
        summary  —— 单遍遍历，不拼整段 JSON；生产默认由配置传入。
        full     —— 旧行为，保留全量哈希；直接调用时的兼容默认值。
    """
    selected = str(mode or "summary").strip().lower()
    if selected == "off":
        return {"kv_diagnostics_mode": "off", "kv_loop": int(loop)}
    if selected == "full":
        diag = _kv_prompt_diagnostics_full(
            messages,
            tools,
            loop=loop,
            model=model,
            stable_prefix_end=stable_prefix_end,
        )
        diag["kv_diagnostics_mode"] = "full"
        return diag
    diag = _kv_prompt_diagnostics_summary(
        messages,
        tools,
        loop=loop,
        tools_diag_cache=tools_diag_cache,
        stable_prefix_end=stable_prefix_end,
    )
    diag["kv_diagnostics_mode"] = "summary"
    return diag


def _diagnostic_text_content(content: Any) -> str:
    """只提取文本块，避免 str(list) 把图片 base64 展开成诊断字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict)
            and str(block.get("type") or "") == "text"
            and block.get("text") is not None
        ]
        return "".join(texts)
    return ""


def _chunked_sha256(value: str) -> str:
    hasher = hashlib.sha256()
    for offset in range(0, len(value), 8192):
        hasher.update(value[offset : offset + 8192].encode("utf-8", "replace"))
    return hasher.hexdigest()


def _diagnostic_asset_identity(asset: Any) -> str:
    if not isinstance(asset, dict):
        return ""
    digest = str(asset.get("sha256") or "")
    if len(digest) != 64:
        return ""
    version = str(asset.get("transform_version") or "")
    detail = str(asset.get("detail") or "auto")
    return f"asset:{digest}:{version}:{detail}"


def _diagnostic_image_parts(block: dict[str, Any]) -> tuple[str, str, str]:
    """返回 (kind, mime, payload_or_url)，不展开正文。"""
    block_type = str(block.get("type") or "")
    if block_type == "image_url":
        value = block.get("image_url")
        url = value.get("url") if isinstance(value, dict) else None
        if isinstance(url, str):
            if url.lower().startswith("data:"):
                header, separator, payload = url.partition(",")
                mime = header[5:].split(";", 1)[0].strip().lower() if separator else ""
                return "data", mime or "image/unknown", payload
            return "url", "", url
        return "invalid", "", ""
    if block_type == "image":
        source = block.get("source")
        if isinstance(source, dict):
            media_type = str(source.get("media_type") or "").lower()
            data = source.get("data")
            if str(source.get("type") or "") == "base64" and isinstance(data, str):
                return "data", media_type or "image/unknown", data
        return "invalid", "", ""
    return "invalid", "", ""


def _diagnostic_image_identity(
    block: dict[str, Any],
    asset: Any = None,
) -> str:
    asset_identity = _diagnostic_asset_identity(asset)
    detail = str(block.get("detail") or "auto")
    if isinstance(block.get("image_url"), dict):
        detail = str(block["image_url"].get("detail") or detail)
    if asset_identity:
        return f"{asset_identity}:detail={detail}"
    kind, mime, payload = _diagnostic_image_parts(block)
    if kind == "data":
        digest = _chunked_sha256(payload)
        return (
            f"data:{mime}:sha256={digest[:16]}:chars={len(payload)}:"
            f"detail={detail}"
        )
    if kind == "url":
        try:
            host = urlsplit(payload).hostname or "remote"
        except ValueError:
            host = "remote"
        digest = _chunked_sha256(payload)
        return f"url:{host}:sha256={digest[:16]}:detail={detail}"
    return f"invalid:detail={detail}"


def _diagnostic_content_identity(
    content: Any,
    assets: list[Any] | None = None,
) -> str:
    """为诊断 hash 生成内容身份；图片只保留 ref/哈希/尺寸。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        image_index = 0
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = str(block.get("type") or "")
            if block_type in {"image_url", "image"}:
                asset = (
                    assets[image_index]
                    if assets is not None and image_index < len(assets)
                    else None
                )
                image_index += 1
                parts.append(
                    f"<image:{_diagnostic_image_identity(block, asset)}>"
                )
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            parts.append(
                json.dumps(
                    block,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
        return "\n".join(parts)
    return json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _diagnostic_assets(message: dict[str, Any]) -> list[Any] | None:
    try:
        return declared_image_assets(message)
    except Exception:  # noqa: BLE001
        return None


def _kv_prompt_diagnostics_summary(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    loop: int,
    tools_diag_cache: dict[tuple[str, int], dict[str, Any]] | None,
    stable_prefix_end: int | None = None,
) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    role_chars: dict[str, int] = {}
    roles: list[str] = []
    content_char_count = 0
    block_counts = {"task_context": 0, "send_receipt": 0, "rag": 0}
    block_chars = {"task_context": 0, "send_receipt": 0, "rag": 0}
    recent_group_lines = 0
    has_recent_group = False

    for message in messages:
        role = str(message.get("role") or "")
        if not role:
            continue
        roles.append(role)
        role_counts[role] = role_counts.get(role, 0) + 1
        content = message.get("content")
        text = _diagnostic_text_content(content)
        role_chars[role] = role_chars.get(role, 0) + len(text)
        content_char_count += len(text)
        for marker, key in (
            ("<task_context", "task_context"),
            ("<send_receipt", "send_receipt"),
            ("<retrieved_conversation_context", "rag"),
        ):
            count = text.count(marker)
            if count:
                block_counts[key] += count
                # 这些块整条消息就是块本身；按消息长度计，避免正则扫描整段上下文。
                block_chars[key] += len(text)
        if "<recent_group_messages" in text:
            has_recent_group = True
        if " msg_id=" in text:
            recent_group_lines += text.count(" msg_id=")

    tail_start = first_tail_block_index(
        messages,
        stable_prefix_end=stable_prefix_end,
    )
    stable_hasher = hashlib.sha256()
    stable_char_count = 0
    for message in messages[:tail_start]:
        role = str(message.get("role") or "")
        content = message.get("content")
        text = _diagnostic_text_content(content)
        stable_char_count += len(text)
        stable_hasher.update(role.encode("utf-8", "replace"))
        stable_hasher.update(b"\0")
        # 内容身份而非长度：同长度正文改写也必须改变稳定区 hash。
        stable_hasher.update(
            _diagnostic_content_identity(
                content,
                _diagnostic_assets(message),
            ).encode("utf-8", "replace")
        )
        stable_hasher.update(b"\0")
        stable_hasher.update(
            json.dumps(
                message.get("tool_calls") or [],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8", "replace")
        )
        stable_hasher.update(b"\0")

    tail_char_count = sum(
        len(_diagnostic_text_content(message.get("content")))
        for message in messages[tail_start:]
    )
    tools_diag = _tools_diagnostics(tools, tools_diag_cache)
    return {
        "kv_loop": int(loop),
        "kv_message_count": len(roles),
        "kv_roles_hash": _short_hash("|".join(roles)),
        "kv_system_count": role_counts.get("system", 0),
        "kv_assistant_count": role_counts.get("assistant", 0),
        "kv_tool_count": role_counts.get("tool", 0),
        "kv_user_count": role_counts.get("user", 0),
        "kv_content_char_count": content_char_count,
        # 近似值：不拼整段 JSON，只按内容 + 每消息固定包装开销估算。
        "kv_serialized_char_count": content_char_count + 64 * len(roles),
        "kv_system_char_count": role_chars.get("system", 0),
        "kv_user_char_count": role_chars.get("user", 0),
        "kv_assistant_char_count": role_chars.get("assistant", 0),
        "kv_tool_char_count": role_chars.get("tool", 0),
        "kv_tools_count": len(tools or []),
        "kv_tools_hash": tools_diag["hash"],
        "kv_tools_char_count": tools_diag["chars"],
        "kv_tools_full_count": tools_diag["full"],
        "kv_tools_stub_count": tools_diag["stub"],
        "kv_prefix_stable_hash": stable_hasher.hexdigest()[:12],
        "kv_prefix_stable_char_count": stable_char_count,
        "kv_tail_block_count": len(messages) - tail_start,
        "kv_tail_char_count": tail_char_count,
        "kv_has_send_receipt": block_counts["send_receipt"] > 0,
        "kv_send_receipt_block_count": block_counts["send_receipt"],
        "kv_send_receipt_char_count": block_chars["send_receipt"],
        "kv_task_context_block_count": block_counts["task_context"],
        "kv_task_context_char_count": block_chars["task_context"],
        "kv_has_recent_group_messages": has_recent_group,
        "kv_recent_group_message_line_count": recent_group_lines,
        "kv_has_rag": block_counts["rag"] > 0,
        "kv_rag_block_count": block_counts["rag"],
        "kv_rag_char_count": block_chars["rag"],
    }


def _tools_diagnostics(
    tools: list[dict[str, Any]] | None,
    cache: dict[tuple[str, int], dict[str, Any]] | None,
) -> dict[str, Any]:
    if not tools:
        return {"hash": "", "chars": 0, "full": 0, "stub": 0}
    # 内容身份：dict 键排序，但 tools 列表顺序保持不变。
    tools_text = json.dumps(
        tools,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(tools_text.encode("utf-8", "replace")).hexdigest()
    key = (digest, len(tools))
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    full = 0
    stub = 0
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        parameters = function.get("parameters") if isinstance(function, dict) else None
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if isinstance(properties, dict) and "_tool_search_required" in properties:
            stub += 1
        else:
            full += 1
    result = {
        "hash": digest[:12],
        "chars": len(tools_text),
        "full": full,
        "stub": stub,
    }
    if cache is not None:
        cache[key] = result
        if len(cache) > 8:
            for stale_key in list(cache)[:-8]:
                cache.pop(stale_key, None)
    return result


_DIAGNOSTIC_DATA_IMAGE_RE = re.compile(
    r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)


def _diagnostic_string_redaction(value: str) -> str:
    lowered = value.lower()
    if lowered.startswith("data:image/"):
        return _diagnostic_data_url_marker(value)
    if lowered.startswith(("http://", "https://")):
        try:
            host = urlsplit(value).hostname or "remote"
        except ValueError:
            host = "remote"
        return f"<url-host:{host}>"
    if "data:image/" in lowered:
        return _DIAGNOSTIC_DATA_IMAGE_RE.sub(
            lambda match: _diagnostic_data_url_marker(match.group(0)),
            value,
        )
    return value


def _diagnostic_data_url_marker(value: str) -> str:
    header, separator, payload = value.partition(",")
    mime = header[5:].split(";", 1)[0].strip().lower() if separator else "image/unknown"
    digest = _chunked_sha256(payload)
    return f"<image-data:{mime}:sha256={digest[:16]}:chars={len(payload)}>"


def _diagnostic_redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "<max-depth>"
    if isinstance(value, str):
        return _diagnostic_string_redaction(value)
    if isinstance(value, dict):
        return {
            str(key): _diagnostic_redact_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_diagnostic_redact_value(item, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


def _kv_prompt_diagnostics_full(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    loop: int,
    model: str = "",
    stable_prefix_end: int | None = None,
) -> dict[str, Any]:
    """全量诊断键名兼容；内部只序列化脱敏副本，不复制整段图片 base64。"""
    normalized = normalize_messages(messages)
    redacted = _diagnostic_redact_value(normalized)
    serialized = json.dumps(
        redacted,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    roles = [str(m.get("role") or "") for m in redacted]
    joined_content = "\n".join(
        _diagnostic_text_content(m.get("content")) for m in redacted
    )
    role_char_counts: dict[str, int] = {}
    for message in redacted:
        role = str(message.get("role") or "")
        role_char_counts[role] = role_char_counts.get(role, 0) + len(
            _diagnostic_text_content(message.get("content"))
        )
    tools_text = json.dumps(
        tools or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    tool_schema_modes = _tool_schema_mode_counts(tools or [])
    tail_start = first_tail_block_index(
        redacted,
        stable_prefix_end=stable_prefix_end,
    )
    stable_text = json.dumps(
        redacted[:tail_start],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return {
        "kv_loop": int(loop),
        "kv_message_count": len(redacted),
        "kv_roles_hash": _short_hash("|".join(roles)),
        "kv_system_count": sum(1 for role in roles if role == "system"),
        "kv_assistant_count": sum(1 for role in roles if role == "assistant"),
        "kv_tool_count": sum(1 for role in roles if role == "tool"),
        "kv_user_count": sum(1 for role in roles if role == "user"),
        "kv_content_char_count": len(joined_content),
        "kv_serialized_char_count": len(serialized),
        "kv_system_char_count": role_char_counts.get("system", 0),
        "kv_user_char_count": role_char_counts.get("user", 0),
        "kv_assistant_char_count": role_char_counts.get("assistant", 0),
        "kv_tool_char_count": role_char_counts.get("tool", 0),
        "kv_tools_count": len(tools or []),
        "kv_tools_hash": _short_hash(tools_text),
        "kv_tools_char_count": len(tools_text),
        "kv_tools_full_count": tool_schema_modes["full"],
        "kv_tools_stub_count": tool_schema_modes["stub"],
        "kv_prefix_8k_hash": _short_hash(serialized[:8192]),
        "kv_prefix_16k_hash": _short_hash(serialized[:16384]),
        "kv_prefix_24k_hash": _short_hash(serialized[:24576]),
        "kv_prefix_32k_hash": _short_hash(serialized[:32768]),
        "kv_prefix_48k_hash": _short_hash(serialized[:49152]),
        "kv_prefix_stable_hash": _short_hash(stable_text),
        "kv_prefix_stable_char_count": len(stable_text),
        "kv_tail_block_count": len(redacted) - tail_start,
        "kv_tail_char_count": len(serialized) - len(stable_text),
        "kv_has_send_receipt": "<send_receipt" in joined_content,
        "kv_send_receipt_block_count": joined_content.count("<send_receipt"),
        "kv_send_receipt_char_count": _tagged_block_char_count(
            joined_content,
            "send_receipt",
        ),
        "kv_task_context_block_count": joined_content.count("<task_context"),
        "kv_task_context_char_count": _tagged_block_char_count(
            joined_content,
            "task_context",
        ),
        "kv_has_recent_group_messages": "<recent_group_messages" in joined_content,
        "kv_recent_group_message_line_count": joined_content.count(" msg_id="),
        "kv_has_rag": "<retrieved_conversation_context" in joined_content,
        "kv_rag_block_count": joined_content.count("<retrieved_conversation_context"),
        "kv_rag_char_count": _tagged_block_char_count(
            joined_content,
            "retrieved_conversation_context",
        ),
    }


def _short_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _tool_schema_mode_counts(tools: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"full": 0, "stub": 0}
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        parameters = function.get("parameters") if isinstance(function, dict) else None
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if isinstance(properties, dict) and "_tool_search_required" in properties:
            counts["stub"] += 1
        else:
            counts["full"] += 1
    return counts


def _tagged_block_char_count(text: str, tag_name: str) -> int:
    pattern = re.compile(
        rf"<{re.escape(tag_name)}(?:\s[^>]*)?>.*?</{re.escape(tag_name)}>",
        re.DOTALL,
    )
    return sum(len(match.group(0)) for match in pattern.finditer(text))


def _executor_accepts_tool_call_id(executor: ToolExecutor) -> bool:
    try:
        signature = inspect.signature(executor)
    except (TypeError, ValueError):
        return False
    positional_count = 0
    for param in signature.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "tool_call_id":
            return True
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_count += 1
    return positional_count >= 3
