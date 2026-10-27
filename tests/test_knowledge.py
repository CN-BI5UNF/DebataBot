"""Knowledge notebook: storage, web distillation and prompt injection."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.knowledge import KnowledgeService, KnowledgeStore


def _config(**overrides):
    base = dict(
        enabled=True,
        inject_max=8,
        max_prompt_chars=1600,
        auto_research_enabled=True,
        research_interval_hours=24,
        research_queries=["今天 网络热梗", "本周 B站 热梗"],
        research_max_notes=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False),
            usage=None,
        )


class FakeSearch:
    def __init__(self, text="搜索结果：city不city 表示洋气/时髦。"):
        self.text = text
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return self.text


def _service(tmp_path, payload, *, search=None, **config_overrides):
    provider = FakeProvider(payload)
    service = KnowledgeService(
        tmp_path,
        persona_id="p",
        config=_config(**config_overrides),
        web_search=search or FakeSearch(),
        provider_resolver=lambda: (provider, SimpleNamespace(model="m", max_tokens=1024)),
    )
    return service, provider


def test_store_only_injects_active_notes_and_escapes(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json")
    store.add_manual(
        persona_id="p",
        kind="meme",
        title="<离谱>",
        content="含义 & [CQ:at,qq=1]",
        status="active",
    )
    store.add_manual(
        persona_id="p",
        kind="fact",
        title="归档",
        content="不应出现",
        status="archived",
    )
    context = store.build_context(persona_id="p", max_entries=8, max_chars=1200)
    assert "<network_knowledge>" in context
    assert "&lt;离谱&gt;" in context
    assert "&amp;" in context
    assert "[CQ：" in context
    assert "归档" not in context
    assert store.stats(persona_id="p")["total"] == 2


def test_cross_kind_near_duplicate_is_not_written(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json")
    store.add_manual(
        persona_id="p",
        kind="fact",
        title="Phigros基本介绍",
        content="Phigros 是一款音乐节奏游戏，曲目很多。",
    )
    result = store.upsert_note(
        persona_id="p",
        kind="note",
        title="Phigros基本资料",
        content="Phigros 是音乐节奏游戏，有很多曲目。",
        source="ai",
    )
    assert result["duplicate"] is True
    assert result["kind"] == "fact"
    assert store.stats(persona_id="p")["total"] == 1


def test_identical_note_is_not_rewritten(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json")
    first = store.upsert_note(
        persona_id="p",
        kind="fact",
        title="版本",
        content="第一版",
    )
    second = store.upsert_note(
        persona_id="p",
        kind="fact",
        title="版本",
        content="第一版",
    )
    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    assert store.stats(persona_id="p")["total"] == 1


def test_generic_title_with_different_content_is_not_duplicate(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json")
    store.add_manual(
        persona_id="p",
        kind="fact",
        title="基本介绍",
        content="苹果是一种水果。",
    )
    result = store.upsert_note(
        persona_id="p",
        kind="note",
        title="基本介绍",
        content="橘子是一种柑橘类水果。",
    )
    assert not result.get("duplicate")
    assert store.stats(persona_id="p")["total"] == 2


def test_shared_notes_are_visible_to_other_personas(tmp_path):
    service, _provider = _service(
        tmp_path,
        [{"kind": "fact", "title": "梗", "content": "含义"}],
    )
    shared_entry = service.add_manual(
        kind="fact",
        title="共享资料",
        content="所有角色都能查到的资料。",
        shared=True,
    )
    private_entry = service.add_manual(
        kind="note",
        title="私有资料",
        content="只有本角色能查到。",
        shared=False,
    )
    other = KnowledgeService(
        tmp_path,
        persona_id="other",
        config=service.config,
        web_search=None,
        provider_resolver=lambda: (None, None),
    )
    result = other.lookup(query="资料")
    titles = {entry["title"] for entry in result["entries"]}
    assert "共享资料" in titles
    assert "私有资料" not in titles
    assert other.stats()["shared"] == 1
    with pytest.raises(ValueError, match="所属人格"):
        other.update_entry(shared_entry["id"], {"content": "x"})
    with pytest.raises(ValueError, match="所属人格"):
        other.delete_entry(shared_entry["id"])
    # 写入时也会把其他角色的共享条目当作已有内容，避免重复。
    duplicate = other.add_note(
        kind="fact",
        title="共享资料",
        content="所有角色都能查到的资料。",
        source="ai",
    )
    assert duplicate["duplicate"] is True
    assert service.stats()["total"] == 2
    assert private_entry["shared"] is False


def test_store_dedupe_merges_only_conservative_duplicates(tmp_path):
    path = tmp_path / "knowledge.json"
    entries = [
        {
            "id": "a", "persona_id": "p", "kind": "fact",
            "title": "Phigros基本介绍", "content": "短介绍",
            "usage": "介绍游戏时",
            "source": "ai", "confidence": 0.5, "status": "active",
            "tags": ["游戏"], "sources": ["https://a.example"],
        },
        {
            "id": "b", "persona_id": "p", "kind": "note",
            "title": "Phigros基本资料", "content": "更完整的 Phigros 资料介绍",
            "source": "manual", "confidence": 1.0, "status": "active",
            "tags": ["音乐游戏"], "sources": ["https://b.example"],
        },
        {
            "id": "c", "persona_id": "p", "kind": "fact",
            "title": "Phigros 难度等级体系", "content": "EZ/HD/IN/AT 等完整等级体系。",
            "status": "active",
        },
        {
            "id": "d", "persona_id": "p", "kind": "note",
            "title": "Phigros 难度等级缩写", "content": "缩写含义与读法。",
            "status": "active",
        },
        {
            "id": "e", "persona_id": "other", "kind": "fact",
            "title": "Phigros基本介绍", "content": "另一个角色的资料。",
            "status": "active",
        },
    ]
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    store = KnowledgeStore(path)

    preview = store.dedupe(persona_id="p", dry_run=True)
    assert preview["dry_run"] is True
    assert preview["before"] == 4
    assert preview["after"] == 3
    assert preview["removed_count"] == 1
    assert preview["removed"][0]["title"] == "Phigros基本介绍"
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 5

    result = store.dedupe(persona_id="p", dry_run=False)
    assert result["removed_count"] == 1
    scoped = store.list_entries(persona_id="p", limit=20)
    assert len(scoped) == 3
    assert "Phigros基本介绍" not in {entry["title"] for entry in scoped}
    kept = next(entry for entry in scoped if entry["title"] == "Phigros基本资料")
    assert kept["source"] == "manual"
    assert "游戏" in kept["tags"] and "音乐游戏" in kept["tags"]
    assert "https://a.example" in kept["sources"]
    assert kept["usage"] == "介绍游戏时"
    # 主题相关但不同的两条笔记必须保留。
    assert {"Phigros 难度等级体系", "Phigros 难度等级缩写"} <= {
        entry["title"] for entry in scoped
    }
    # 其他 persona 不受影响。
    assert len(store.list_entries(persona_id="other", limit=20)) == 1
    assert list(tmp_path.glob("knowledge.json.dedupe-*"))


def test_manual_add_rejects_near_duplicate(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json")
    store.add_manual(
        persona_id="p",
        kind="fact",
        title="Phigros基本介绍",
        content="Phigros 是一款音乐节奏游戏。",
    )
    with pytest.raises(ValueError, match="相似"):
        store.add_manual(
            persona_id="p",
            kind="note",
            title="Phigros基本资料",
            content="Phigros 是音乐节奏游戏。",
        )
    assert store.stats(persona_id="p")["total"] == 1


def test_store_caps_memory_and_file(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json", max_entries=2)
    for index in range(4):
        store.add_manual(
            persona_id="p",
            kind="note",
            title=f"标题{index}",
            content=f"内容{index}",
        )
    assert store.stats(persona_id="p")["total"] == 2
    assert len(store._entries) == 2
    assert len(json.loads((tmp_path / "knowledge.json").read_text(encoding="utf-8"))) == 2


def test_corrupt_store_is_quarantined(tmp_path):
    path = tmp_path / "knowledge.json"
    path.write_text("{not json", encoding="utf-8")
    store = KnowledgeStore(path)
    assert store.writable is False
    assert list(tmp_path.glob("knowledge.json.corrupt-*"))
    with pytest.raises(RuntimeError, match="不可写"):
        store.add_manual(
            persona_id="p",
            kind="note",
            title="x",
            content="y",
        )


@pytest.mark.asyncio
async def test_research_distills_and_saves_notes(tmp_path):
    service, provider = _service(
        tmp_path,
        [
            {
                "kind": "meme",
                "title": "city不city",
                "content": "表示洋气、时髦，常带调侃。",
                "usage": "看到打扮很潮时",
                "tags": ["热梗"],
                "sources": ["https://example.com/a"],
                "confidence": 0.8,
            }
        ],
    )
    result = await service.research("city不city 是什么梗", kind="meme")
    assert result["ok"] is True
    assert result["saved"] == 1
    assert len(provider.calls) == 1
    assert "不可信" in provider.calls[0][0]["content"]
    context = service.build_context()
    assert "city不city" in context
    assert "https://example.com/a" not in context, "来源不进入模型侧 prompt"
    assert service.stats()["kinds"]["meme"] == 1


@pytest.mark.asyncio
async def test_research_accepts_object_wrapper_and_chinese_aliases(tmp_path):
    service, _provider = _service(
        tmp_path,
        {
            "notes": [
                {
                    "类型": "热梗",
                    "标题": "city不city",
                    "内容": "表示洋气、时髦",
                    "用法": "看到打扮很潮时",
                    "标签": ["热梗"],
                    "来源": ["https://example.com/a"],
                    "置信度": 0.7,
                }
            ]
        },
    )
    result = await service.research("city不city", kind="note")
    assert result["ok"] is True
    assert result["saved"] == 1
    entry = service.list_entries()[0]
    assert entry["kind"] == "meme"
    assert entry["title"] == "city不city"
    assert entry["usage"] == "看到打扮很潮时"


@pytest.mark.asyncio
async def test_research_updates_duplicate_title_instead_of_duplicating(tmp_path):
    payload = [
        {
            "kind": "fact",
            "title": "版本",
            "content": "第一版",
            "sources": [],
            "confidence": 0.5,
        }
    ]
    service, _provider = _service(tmp_path, payload)
    await service.research("版本信息", kind="fact")
    service.provider_resolver = lambda: (
        FakeProvider(
            [
                {
                    "kind": "fact",
                    "title": "版本",
                    "content": "第二版",
                    "sources": ["https://example.com/b"],
                    "confidence": 0.9,
                }
            ]
        ),
        SimpleNamespace(model="m", max_tokens=1024),
    )
    await service.research("版本信息 2", kind="fact")
    entries = service.list_entries()
    assert len(entries) == 1
    assert entries[0]["content"] == "第二版"
    assert entries[0]["sources"] == ["https://example.com/b"]


@pytest.mark.asyncio
async def test_research_checks_existing_notes_and_skips_duplicates(tmp_path):
    service, provider = _service(
        tmp_path,
        [
            {
                "kind": "note",
                "title": "Phigros基本资料",
                "content": "Phigros 是音乐节奏游戏，有很多曲目。",
                "sources": [],
                "confidence": 0.5,
            }
        ],
    )
    service.add_manual(
        kind="fact",
        title="Phigros基本介绍",
        content="Phigros 是一款音乐节奏游戏，曲目很多。",
    )
    result = await service.research("Phigros 是什么", kind="fact")
    assert result["ok"] is True
    assert result["saved"] == 0
    assert result["duplicates"] == 1
    assert result["duplicate_titles"] == ["Phigros基本资料"]
    assert result["duplicate_kept_titles"] == ["Phigros基本介绍"]
    assert len(service.list_entries()) == 1
    prompt = provider.calls[0][1]["content"]
    assert "知识库已有条目" in prompt
    assert "Phigros基本介绍" in prompt


@pytest.mark.asyncio
async def test_research_retries_with_permissive_prompt_when_first_is_empty(tmp_path):
    class SequenceProvider:
        def __init__(self):
            self.outputs = [
                "[]",
                json.dumps(
                    [
                        {
                            "kind": "meme",
                            "title": "电子布洛芬",
                            "content": "指刷短视频缓解情绪。",
                            "confidence": 0.5,
                        }
                    ],
                    ensure_ascii=False,
                ),
            ]
            self.calls = 0

        async def chat_completion(self, messages, **kwargs):
            index = min(self.calls, len(self.outputs) - 1)
            self.calls += 1
            return SimpleNamespace(content=self.outputs[index], usage=None)

    provider = SequenceProvider()
    service = KnowledgeService(
        tmp_path,
        persona_id="p",
        config=_config(),
        web_search=FakeSearch("搜索结果不完整"),
        provider_resolver=lambda: (provider, SimpleNamespace(model="m", max_tokens=1024)),
    )
    result = await service.research("本周热梗", kind="meme")
    assert result["ok"] is True
    assert result["saved"] == 1
    assert provider.calls == 2
    assert service.list_entries()[0]["title"] == "电子布洛芬"


@pytest.mark.asyncio
async def test_research_rejects_search_error_string(tmp_path):
    service, provider = _service(
        tmp_path,
        [{"kind": "note", "title": "x", "content": "y"}],
        search=FakeSearch("（免费搜索失败：timeout）"),
    )
    result = await service.research("x")
    assert result["ok"] is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_trend_research_runs_once_then_respects_interval(tmp_path):
    service, provider = _service(
        tmp_path,
        [{"kind": "meme", "title": "梗", "content": "含义", "confidence": 0.6}],
    )
    first = await service.run_trend_research()
    assert first["ok"] is True
    assert first["saved"] == 1
    assert first["duplicates"] == 1
    assert len(provider.calls) == 2

    second = await service.run_trend_research()
    assert second["skipped"] is True
    assert second["reason"] == "not_due"


def test_task_context_does_not_inject_knowledge():
    from core.pipeline_task_context import PipelineTaskContextMixin

    class ExplodingKnowledge:
        def build_context(self):
            raise AssertionError("知识库不应自动注入 task_context")

    class FakePipeline(PipelineTaskContextMixin):
        emoji_dir = None
        pending_requests = SimpleNamespace(to_prompt_text=lambda: "")
        persona_agent = None
        slang_service = None
        knowledge_service = ExplodingKnowledge()

        def _context_lookup_hint(self, conversation_id):
            return ""

    private = FakePipeline()._build_task_context("2026-10-26 12:00", "private:1")
    group = FakePipeline()._build_task_context("2026-10-26 12:00", "group:2")
    assert "<network_knowledge>" not in private
    assert "<network_knowledge>" not in group


def test_knowledge_service_lookup_returns_active_notes_only(tmp_path):
    service, _provider = _service(
        tmp_path,
        [{"kind": "meme", "title": "梗", "content": "含义"}],
    )
    service.add_manual(
        kind="meme",
        title="city不city",
        content="洋气、时髦",
        usage="看到打扮很潮时",
        sources=["https://example.com/a"],
        status="active",
    )
    service.add_manual(
        kind="fact",
        title="归档条目",
        content="不应返回",
        status="archived",
    )
    result = service.lookup(query="city")
    assert result["ok"] is True
    assert result["returned"] == 1
    entry = result["entries"][0]
    assert entry["title"] == "city不city"
    assert entry["usage"] == "看到打扮很潮时"
    assert entry["sources"] == ["https://example.com/a"]
    assert "归档条目" not in str(result["entries"])
    assert result["notice"]
    assert result["truncated"] is False


def test_knowledge_lookup_respects_config_limits(tmp_path):
    service, _provider = _service(
        tmp_path,
        [{"kind": "meme", "title": "梗", "content": "含义"}],
        inject_max=1,
        max_prompt_chars=2000,
    )
    service.add_manual(kind="meme", title="梗一", content="含义一")
    service.add_manual(kind="meme", title="梗二", content="含义二")
    result = service.lookup()
    assert result["returned"] == 1
    assert result["total"] == 2
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_disabled_knowledge_is_not_injected_or_queryable(tmp_path):
    service, _provider = _service(
        tmp_path,
        [{"kind": "meme", "title": "梗", "content": "含义"}],
        enabled=False,
    )
    service.add_manual(
        kind="meme",
        title="梗",
        content="含义",
    )
    assert service.build_context() == ""
    assert service.lookup()["ok"] is False
    result = await service.research("x")
    assert result["ok"] is False
