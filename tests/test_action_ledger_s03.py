"""T18 / S03 / C05 专项测试：Action Ledger 状态机、轮次水位与循环内局部紧缩。"""

import pytest

from core.action_ledger import (
    ActionLedgerManager,
    TurnCheckpointManager,
    compact_turn_loop,
)
from core.d01 import _DDL
from memory.debata_db import DebataDB
from utils.token_budget import TokenEstimator


@pytest.fixture
def ledger_db(tmp_path):
    db_file = tmp_path / "action_ledger_test.db"
    db = DebataDB(db_file)
    db.load()
    with db.connect() as conn:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    return db


def test_action_ledger_lifecycle(ledger_db):
    """测试动作状态机生命周期：prepared -> started -> result_committed。"""
    mgr = ActionLedgerManager(ledger_db)

    # 1. prepare
    mgr.prepare_action(
        "persona_1",
        "act_100",
        turn_id="turn_1",
        tool_name="web_search",
        args_summary='{"q": "Debata"}',
    )
    rec1 = mgr.get_action("persona_1", "act_100")
    assert rec1 is not None
    assert rec1.state == "prepared"
    assert rec1.receipt["tool_name"] == "web_search"

    # 2. start
    mgr.start_action("persona_1", "act_100")
    rec2 = mgr.get_action("persona_1", "act_100")
    assert rec2 is not None
    assert rec2.state == "started"

    # 3. commit
    mgr.commit_action_result(
        "persona_1",
        "act_100",
        receipt={"ok": True, "count": 5},
    )
    rec3 = mgr.get_action("persona_1", "act_100")
    assert rec3 is not None
    assert rec3.state == "result_committed"
    assert rec3.receipt["ok"] is True


def test_action_ledger_unknown_auditing(ledger_db):
    """测试 E33：动作超时或外部异常时标记为 unknown，保留无正文审计回执。"""
    mgr = ActionLedgerManager(ledger_db)
    mgr.mark_action_unknown(
        "persona_1",
        "act_err_1",
        error_detail="Timeout after 30s connecting to external service",
    )
    rec = mgr.get_action("persona_1", "act_err_1")
    assert rec is not None
    assert rec.state == "unknown"
    assert "Timeout after 30s" in rec.receipt["error"]
    assert rec.receipt["status"] == "unknown"


def test_turn_checkpoint_manager(ledger_db):
    """测试轮次检查点与水位记录。"""
    chk_mgr = TurnCheckpointManager(ledger_db)

    chk_mgr.open_turn("persona_1", "turn_xyz", watermark_seq=42)
    cp1 = chk_mgr.get_checkpoint("persona_1", "turn_xyz")
    assert cp1 is not None
    assert cp1.state == "open"
    assert cp1.watermark_seq == 42
    assert cp1.raw_seq == 0

    chk_mgr.record_step("persona_1", "turn_xyz", raw_seq=3)
    cp2 = chk_mgr.get_checkpoint("persona_1", "turn_xyz")
    assert cp2 is not None
    assert cp2.raw_seq == 3

    chk_mgr.close_turn("persona_1", "turn_xyz")
    cp3 = chk_mgr.get_checkpoint("persona_1", "turn_xyz")
    assert cp3 is not None
    assert cp3.state == "closed"


def test_compact_turn_loop_compaction():
    """测试单轮工具循环内局部紧缩：紧缩已闭合早期工具调用，保护最新输入与未决工具。"""
    estimator = TokenEstimator()

    messages = [
        {"role": "user", "content": "帮我查一下近期的两份文件并做对比。"},
        # 早期已闭合工具轮 1
        {
            "role": "assistant",
            "content": "正在搜索第一份文件",
            "tool_calls": [{"id": "call_1", "function": {"name": "search"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "非常长非常详细的第一份文件搜索结果原始内容……" * 20,
        },
        # 早期已闭合工具轮 2
        {
            "role": "assistant",
            "content": "正在搜索第二份文件",
            "tool_calls": [{"id": "call_2", "function": {"name": "search"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": "非常长非常详细的第二份文件搜索结果原始内容……" * 20,
        },
        # 当前最新一轮工具调用（尚未闭合或刚发起）
        {
            "role": "assistant",
            "content": "对比中，发起核对",
            "tool_calls": [{"id": "call_3", "function": {"name": "compare"}}],
        },
        # 最新用户补充输入
        {"role": "user", "content": "补充要求：着重看安全部分。"},
    ]

    total_tokens = estimator.estimate_messages(messages)
    # 限制预算使得原消息超限
    tight_budget = total_tokens // 2

    res = compact_turn_loop(messages, input_budget=tight_budget, estimator=estimator)
    assert res.compacted is True
    assert res.saved_tokens > 0
    assert "call_1" in res.covered_tool_call_ids

    # 验证保护原则：最新一条用户输入绝不被截断
    assert res.remaining_messages[-1]["content"] == "补充要求：着重看安全部分。"
    # 验证最新一次工具调用 call_3 仍被完整保留
    last_assistant = next(
        m
        for m in reversed(res.remaining_messages)
        if m.get("role") == "assistant"
    )
    assert last_assistant.get("tool_calls")[0]["id"] == "call_3"

    # 验证早期工具 call_1 的 verbose 内容已被压缩为结果摘要
    tool_1_msg = next(
        m
        for m in res.remaining_messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_1"
    )
    assert "[工具结果摘要]" in tool_1_msg["content"]
    assert len(tool_1_msg["content"]) < len(messages[2]["content"])
