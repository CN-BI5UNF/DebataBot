"""群聊黑话存储、学习与 prompt 注入。"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.slang import SlangService, SlangStore
from webui.api import slang as slang_api


def _config(**overrides):
    base = dict(
        enabled=True,
        auto_learn=False,
        extract_min_messages=3,
        extract_cooldown_seconds=30,
        inject_max=8,
        max_prompt_chars=1200,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_only_confirmed_with_meaning_is_injected_and_scoped_per_group(tmp_path):
    store = SlangStore(tmp_path / "slang.json")
    store.add_manual(
        persona_id="p1",
        conversation_id="group:1",
        content="难绷",
        meaning="很难绷得住，表示好笑或无语",
        usage="看到离谱内容时",
        example="这图难绷",
    )
    store.add_manual(
        persona_id="p1",
        conversation_id="group:2",
        content="yyds",
        meaning="永远的神",
    )
    candidate = store.upsert_candidates(
        persona_id="p1",
        conversation_id="group:1",
        candidates=[{"content": "草", "meaning": "笑死"}],
    )[0]
    assert candidate["status"] == "candidate"

    context = store.build_context(
        persona_id="p1",
        conversation_id="group:1",
        max_entries=8,
        max_chars=1200,
    )
    assert "【群聊黑话表】" in context
    assert "难绷" in context
    assert "yyds" not in context, "不同群不能串词"
    assert "草" not in context, "候选词不能注入"
    assert "evidence" not in context and "sources" not in context


def test_upsert_increments_and_rejected_is_not_reopened(tmp_path):
    store = SlangStore(tmp_path / "slang.json")
    first = store.upsert_candidates(
        persona_id="p",
        conversation_id="group:1",
        candidates=[{"content": "难绷"}],
    )[0]
    second = store.upsert_candidates(
        persona_id="p",
        conversation_id="group:1",
        candidates=[{"content": "难绷", "meaning": "无语"}],
    )[0]
    assert first["id"] == second["id"]
    assert second["count"] == 2
    assert second["meaning"] == "无语"
    store.update_entry(first["id"], {"status": "rejected"})
    again = store.upsert_candidates(
        persona_id="p",
        conversation_id="group:1",
        candidates=[{"content": "难绷"}],
    )
    assert again == []


def test_manual_entry_requires_meaning_and_group_scope(tmp_path):
    store = SlangStore(tmp_path / "slang.json")
    with pytest.raises(ValueError, match="释义"):
        store.add_manual(
            persona_id="p",
            conversation_id="group:1",
            content="草",
            meaning="",
        )
    with pytest.raises(ValueError, match="group:"):
        store.add_manual(
            persona_id="p",
            conversation_id="private:1",
            content="草",
            meaning="笑",
        )


def test_corrupt_store_is_quarantined_and_read_only(tmp_path):
    path = tmp_path / "slang.json"
    path.write_text("{not json", encoding="utf-8")
    store = SlangStore(path)
    assert store.writable is False
    assert store.list_entries() == []
    assert list(tmp_path.glob("slang.json.corrupt-*"))
    with pytest.raises(RuntimeError, match="不可写"):
        store.add_manual(
            persona_id="p",
            conversation_id="group:1",
            content="草",
            meaning="笑",
        )


def test_prompt_escapes_untrusted_text_and_caps_length(tmp_path):
    store = SlangStore(tmp_path / "slang.json")
    store.add_manual(
        persona_id="p",
        conversation_id="group:1",
        content="<tag>",
        meaning="笑 [CQ:at,qq=1] & 无语",
        usage="a" * 300,
    )
    context = store.build_context(
        persona_id="p",
        conversation_id="group:1",
        max_entries=8,
        max_chars=500,
    )
    assert "&lt;tag&gt;" in context
    assert "[CQ：" in context
    assert len(context) <= 560


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False), usage=None)


@pytest.mark.asyncio
async def test_learner_extracts_candidates_with_evidence(tmp_path):
    provider = FakeProvider(
        [
            {
                "content": "难绷",
                "meaning": "很难绷得住",
                "usage": "看到离谱内容",
                "source_id": "2",
                "confidence": 0.8,
            }
        ]
    )
    service = SlangService(
        tmp_path,
        persona_id="p",
        config=_config(),
        provider_resolver=lambda: (provider, SimpleNamespace(model="m", max_tokens=1024)),
    )
    service.feed("group:1", sender="群友A", text="这波真的 yyds", timestamp=100)
    service.feed("group:1", sender="群友B", text="难绷，这也行", timestamp=101)
    service.feed("group:1", sender="群友C", text="笑死", timestamp=102)
    result = await service.learn_now("group:1", force=True)
    assert result["ok"] is True
    assert result["candidates"] == 1
    entries = service.list_entries()
    assert entries[0]["content"] == "难绷"
    assert entries[0]["status"] == "candidate"
    assert entries[0]["evidence"][0]["sender"] == "群友B"
    assert service.build_context("group:1") == ""
    service.store.update_entry(entries[0]["id"], {"status": "confirmed", "meaning": "无语、绷不住"})
    assert "难绷" in service.build_context("group:1")


@pytest.mark.asyncio
async def test_learner_rejects_unparseable_output(tmp_path):
    class BadProvider:
        async def chat_completion(self, messages, **kwargs):
            return SimpleNamespace(content="不是 JSON", usage=None)

    service = SlangService(
        tmp_path,
        persona_id="p",
        config=_config(),
        provider_resolver=lambda: (BadProvider(), SimpleNamespace(model="m", max_tokens=1024)),
    )
    for index in range(3):
        service.feed("group:1", sender="a", text=f"消息{index}")
    result = await service.learn_now("group:1", force=True)
    assert result["ok"] is False


def _request(service):
    rt = SimpleNamespace(slang_service=service)
    host = SimpleNamespace(rt=rt, state="running", last_error=None)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host=host)))


@pytest.mark.asyncio
async def test_slang_api_manual_crud_and_confirmation(tmp_path):
    service = SlangService(
        tmp_path,
        persona_id="p",
        config=_config(),
        provider_resolver=lambda: (FakeProvider([]), SimpleNamespace(model="m")),
    )
    request = _request(service)
    created = await slang_api.create_slang(
        request,
        {
            "conversation_id": "group:1",
            "content": "难绷",
            "meaning": "无语",
        },
    )
    entry_id = created["entry"]["id"]
    listed = await slang_api.list_slang(request, conversation_id="group:1")
    assert listed["entries"][0]["content"] == "难绷"
    updated = await slang_api.update_slang(
        request, entry_id, {"status": "rejected"}
    )
    assert updated["entry"]["status"] == "rejected"
    with pytest.raises(HTTPException):
        await slang_api.create_slang(
            request,
            {"conversation_id": "private:1", "content": "x", "meaning": "y"},
        )
    deleted = await slang_api.delete_slang(request, entry_id)
    assert deleted["ok"] is True


@pytest.mark.asyncio
async def test_slang_api_learn_endpoint(tmp_path):
    provider = FakeProvider([{"content": "草", "meaning": "笑", "source_id": "1"}])
    service = SlangService(
        tmp_path,
        persona_id="p",
        config=_config(),
        provider_resolver=lambda: (provider, SimpleNamespace(model="m", max_tokens=1024)),
    )
    service.feed("group:1", sender="a", text="草")
    request = _request(service)
    result = await slang_api.learn_now(request, {"conversation_id": "group:1"})
    assert result["ok"] is True
    assert result["candidates"] == 1


def test_task_context_does_not_inject_slang():
    from core.pipeline_task_context import PipelineTaskContextMixin

    class ExplodingSlang:
        def build_context(self, conversation_id):
            raise AssertionError("黑话不应自动注入 task_context")

    class FakePipeline(PipelineTaskContextMixin):
        emoji_dir = None
        pending_requests = SimpleNamespace(to_prompt_text=lambda: "")
        persona_agent = None
        slang_service = ExplodingSlang()

        def _context_lookup_hint(self, conversation_id):
            return ""

    context = FakePipeline()._build_task_context("2026-10-26 12:00", "group:1")
    assert "【群聊黑话表】" not in context
    assert "难绷" not in context


def test_slang_service_lookup_filters_group_and_status(tmp_path):
    store = SlangStore(tmp_path / "slang.json")
    store.add_manual(
        persona_id="p",
        conversation_id="group:1",
        content="难绷",
        meaning="无语、绷不住",
        usage="看到离谱内容时",
        example="这图难绷",
    )
    store.add_manual(
        persona_id="p",
        conversation_id="group:2",
        content="yyds",
        meaning="永远的神",
    )
    store.upsert_candidates(
        persona_id="p",
        conversation_id="group:1",
        candidates=[{"content": "草", "meaning": "笑死"}],
    )
    service = SlangService(
        tmp_path,
        persona_id="p",
        config=_config(),
        provider_resolver=lambda: (None, None),
    )
    result = service.lookup("group:1", query="难", limit=8)
    assert result["ok"] is True
    assert [entry["content"] for entry in result["entries"]] == ["难绷"]
    assert result["entries"][0]["meaning"] == "无语、绷不住"
    assert result["entries"][0]["usage"] == "看到离谱内容时"
    assert "yyds" not in str(result["entries"])
    assert "草" not in str(result["entries"])
    assert "evidence" not in str(result["entries"])
    assert "sources" not in str(result["entries"])
    assert result["notice"]

    assert service.lookup("private:1")["ok"] is False
    assert service.lookup("group:1", query="不存在的词")["returned"] == 0


def test_task_context_adds_time_sense_for_old_messages():
    from core.pipeline_task_context import PipelineTaskContextMixin

    class FakePipeline(PipelineTaskContextMixin):
        emoji_dir = None
        pending_requests = SimpleNamespace(to_prompt_text=lambda: "")
        persona_agent = None
        slang_service = None
        chat_timeline = SimpleNamespace(
            recent=lambda conversation_id, limit: [
                SimpleNamespace(timestamp=time.time() - 1800)
            ]
        )

        def _context_lookup_hint(self, conversation_id):
            return ""

    context = FakePipeline()._build_task_context("2026-10-26 12:00", "group:1")
    assert "分钟前" in context
    assert "不要用“刚刚”" in context


def test_store_keeps_in_memory_index_bounded(tmp_path):
    store = SlangStore(tmp_path / "slang.json", max_entries=2)
    for index in range(4):
        store.add_manual(
            persona_id="p",
            conversation_id="group:1",
            content=f"词{index}",
            meaning=f"释义{index}",
        )
    assert store.stats()["total"] == 2
    assert len(store._entries) == 2
    assert len(json.loads((tmp_path / "slang.json").read_text(encoding="utf-8"))) == 2


@pytest.mark.asyncio
async def test_learner_preserves_messages_arriving_during_model_call(tmp_path):
    """A full window plus new traffic must not drop the new messages."""
    from core.slang import MAX_WINDOW_MESSAGES

    class FeedDuringCallProvider:
        def __init__(self):
            self.service = None

        async def chat_completion(self, messages, **kwargs):
            # Simulate chat traffic arriving while the extractor is thinking.
            # The full deque evicts the oldest sent messages here.
            for index in range(5):
                self.service.feed(
                    "group:1", sender="新群友", text=f"新消息{index}", timestamp=200 + index
                )
            return SimpleNamespace(
                content='[{"content":"草","meaning":"笑","source_id":"1"}]',
                usage=None,
            )

    provider = FeedDuringCallProvider()
    service = SlangService(
        tmp_path,
        persona_id="p",
        config=_config(),
        provider_resolver=lambda: (provider, SimpleNamespace(model="m", max_tokens=1024)),
    )
    provider.service = service
    for index in range(MAX_WINDOW_MESSAGES):
        service.feed("group:1", sender="a", text=f"旧消息{index}", timestamp=100 + index)
    result = await service.learn_now("group:1", force=True)
    assert result["ok"] is True
    # Only the five messages that arrived during the call should remain.
    assert service.buffer_size("group:1") == 5
    window = list(service.learner._windows["group:1"])
    assert [item["text"] for item in window] == [f"新消息{index}" for index in range(5)]
