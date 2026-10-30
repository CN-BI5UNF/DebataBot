"""C4 主请求图片预算、三个成功出口、摘要保护与 provider 最后护栏。"""

from __future__ import annotations

import asyncio
import copy
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from adapters.types import IncomingMessage, MediaSegment, MediaType
from agents.runner import AgentRunner
from app_config.schema import AgentConfig
from core.message_pipeline import MessagePipeline
from core.model_view import build_model_view
from core.pipeline_summary import SummaryAttemptResult
from core.state import PendingMessageItem, ProtectedInput
from features.vision.image_assets import ImageAssetStore
from features.vision.video_assets import VideoAssetStore
from memory import ArchiveStore, HistoryManager, RollingSummaryStore
from utils.image_budget import assert_wire_request_fits
from utils.image_contracts import ImageBudgetProfile, ImageLimits
from utils.model_records import ModelViewPolicy
from utils.video_contracts import VideoBudgetProfile, VideoLimits


def _png_bytes(width: int = 32, height: int = 24) -> bytes:
    image = Image.new("RGB", (width, height), (30, 90, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _profile(tokens: int = 256) -> ImageBudgetProfile:
    return ImageBudgetProfile(
        name="reserve_v1",
        per_image_tokens=tokens,
        basis="configured_reserve",
        exact=False,
    )


def _make_pipeline(
    tmp_path,
    *,
    limits: ImageLimits | None = None,
    max_context_tokens: int = 100_000,
    reserve_output_tokens: int = 0,
    videos: bool = False,
    video_limits: VideoLimits | None = None,
) -> MessagePipeline:
    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = tmp_path / "workspace"
    pipeline.adapter = None
    pipeline.asr = None
    pipeline.main_model_images_enabled = True
    pipeline.image_limits = limits or ImageLimits()
    pipeline._image_budget_profile = _profile()
    pipeline.main_model_videos_enabled = videos
    pipeline.video_limits = video_limits or VideoLimits()
    pipeline.video_token_reserve = 256
    pipeline._video_budget_profile = (
        VideoBudgetProfile(
            name="reserve_v1",
            per_video_tokens=256,
            basis="configured_reserve",
            exact=False,
        )
        if videos
        else None
    )
    pipeline._video_asset_store_instance = None
    pipeline._active_input_guards = []
    pipeline._token_calib_ratio = 1.0
    pipeline._token_calib_provider = "fake"
    pipeline._token_calib_model = "fake-1"
    pipeline.model_context_length = max_context_tokens
    pipeline.chat_agent = SimpleNamespace(
        cfg=SimpleNamespace(model="fake-1", max_tokens=1024),
        provider=None,
    )
    pipeline.behavior_cfg = SimpleNamespace(
        context=SimpleNamespace(
            max_context_tokens=max_context_tokens,
            reserve_output_tokens=reserve_output_tokens,
            memory_token_budget=100,
            summary_token_budget=100,
        )
    )
    pipeline._image_asset_store_instance = None

    async def no_summary(**kwargs):
        return SummaryAttemptResult(status="not_needed", reason="test")

    async def no_record(**kwargs):
        return None

    pipeline._maybe_summarize = no_summary
    pipeline._record_main_prompt_budget_failure = no_record
    return pipeline


async def _ref(tmp_path, pipeline, *, index: int = 0, message_id: str = "m1") -> dict:
    store = ImageAssetStore(
        pipeline.workspace_dir,
        limits=pipeline.image_limits,
    )
    return await store.ingest(
        _png_bytes(),
        message_id=message_id,
        index=index,
    )


def _messages(refs: list[dict], *, text: str = "看看图片") -> list[dict]:
    return [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": text,
            "conversation_id": "private:1",
            "metadata": {
                "messages": [
                    {
                        "message_id": "m1",
                        "inbound_seq": 1,
                    }
                ],
                "image_assets": refs,
            },
        },
    ]


def _wire_has_image(messages: list[dict]) -> bool:
    return any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(block, dict) and block.get("type") == "image_url"
            for block in message["content"]
        )
        for message in messages
    )


def _mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + (b"\xab" * 2048)


async def _video_ref(tmp_path, pipeline, *, index: int = 0) -> dict:
    store = VideoAssetStore(
        pipeline.workspace_dir,
        limits=pipeline.video_limits,
    )
    return await store.ingest(
        _mp4_bytes(),
        message_id="m1",
        index=index,
    )


def _video_messages(refs: list[dict]) -> list[dict]:
    return [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": "看看视频",
            "conversation_id": "private:1",
            "metadata": {
                "messages": [{"message_id": "m1", "inbound_seq": 1}],
                "video_assets": refs,
            },
        },
    ]


@pytest.mark.asyncio
async def test_direct_success_materializes_and_no_image_skips_store(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    ref = await _ref(tmp_path, pipeline)
    result = await MessagePipeline._prepare_main_prompt_for_model(
        pipeline,
        conversation_id="private:1",
        phase="test",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(0, result=_messages([ref])),
    )

    assert result.ok is True
    assert result.contains_images is True
    assert result.image_count == 1
    assert result.image_bytes > 0
    assert result.budget_basis == "configured_reserve"
    user = result.messages[-1]
    assert isinstance(user["content"], list)
    assert user["content"][1]["text"].startswith("[图片所属消息")
    assert user["content"][2]["image_url"]["url"].startswith("data:image/png;base64,")

    plain = _make_pipeline(tmp_path / "plain")
    text_result = await MessagePipeline._prepare_main_prompt_for_model(
        plain,
        conversation_id="private:1",
        phase="test_text",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(
            0, result=[{"role": "user", "content": "纯文字"}]
        ),
    )
    assert text_result.ok is True
    assert text_result.contains_images is False
    assert plain._image_asset_store_instance is None


@pytest.mark.asyncio
async def test_direct_video_success_materializes_complete_video(tmp_path):
    pipeline = _make_pipeline(tmp_path, videos=True)
    ref = await _video_ref(tmp_path, pipeline)
    result = await MessagePipeline._prepare_main_prompt_for_model(
        pipeline,
        conversation_id="private:1",
        phase="test_video",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(0, result=_video_messages([ref])),
    )

    assert result.ok is True
    assert result.contains_videos is True
    assert result.video_count == 1
    assert result.video_bytes > 0
    assert result.video_tokens == 256
    user = result.messages[-1]
    assert isinstance(user["content"], list)
    assert user["content"][1]["text"].startswith("[视频所属消息")
    assert user["content"][2]["type"] == "video_url"
    assert user["content"][2]["video_url"]["url"].startswith(
        "data:video/mp4;base64,"
    )
    assert result.estimated_tokens >= result.video_tokens


@pytest.mark.asyncio
async def test_image_and_video_are_both_projected_in_order(tmp_path):
    pipeline = _make_pipeline(tmp_path, videos=True)
    image_ref = await _ref(tmp_path, pipeline)
    video_ref = await _video_ref(tmp_path, pipeline)
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": "图片和视频",
            "conversation_id": "private:1",
            "metadata": {
                "messages": [{"message_id": "m1", "inbound_seq": 1}],
                "image_assets": [image_ref],
                "video_assets": [video_ref],
            },
        },
    ]
    result = await MessagePipeline._prepare_main_prompt_for_model(
        pipeline,
        conversation_id="private:1",
        phase="test_mixed_media",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(0, result=messages),
    )

    assert result.ok is True
    assert result.contains_images is True
    assert result.contains_videos is True
    user_content = result.messages[-1]["content"]
    image_pos = next(
        index
        for index, block in enumerate(user_content)
        if block.get("type") == "image_url"
    )
    video_pos = next(
        index
        for index, block in enumerate(user_content)
        if block.get("type") == "video_url"
    )
    assert image_pos < video_pos
    assert user_content[image_pos]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert user_content[video_pos]["video_url"]["url"].startswith(
        "data:video/mp4;base64,"
    )


@pytest.mark.asyncio
async def test_pending_media_record_projects_model_copy_but_keeps_history_raw(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    ref = await _ref(tmp_path, pipeline)
    raw = {
        "role": "user",
        "content": "刚来的图片",
        "conversation_id": "private:1",
        "metadata": {
            "messages": [{"message_id": "m1", "inbound_seq": 1}],
            "image_assets": [ref],
        },
    }

    projected = await MessagePipeline._project_pending_media_record(
        pipeline, raw
    )

    assert isinstance(projected["content"], list)
    assert projected["content"][1]["text"].startswith("[图片所属消息")
    assert projected["content"][2]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert assert_wire_request_fits([projected])[0] == 1
    # 历史仍保存原始 refs/文本，不能持久化模型副本里的 data URL。
    assert raw["content"] == "刚来的图片"
    assert raw["metadata"]["image_assets"] == [ref]


@pytest.mark.asyncio
async def test_pending_media_record_strips_disabled_image_refs(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    pipeline.main_model_images_enabled = False
    raw = {
        "role": "user",
        "content": "刚来的图片",
        "conversation_id": "private:1",
        "metadata": {
            "messages": [{"message_id": "m1", "inbound_seq": 1}],
            "image_assets": [
                {
                    "sha256": "a" * 64,
                    "mime": "image/png",
                    "width": 1,
                    "height": 1,
                    "byte_size": 1,
                    "path": "incoming/images/old.png",
                    "detail": "auto",
                    "message_id": "m1",
                    "index": 0,
                    "transform_version": "pillow-first-frame-max2048-v2",
                }
            ],
        },
    }

    projected = await MessagePipeline._project_pending_media_record(
        pipeline, raw
    )

    assert projected["content"] == "刚来的图片"
    assert "image_assets" not in projected["metadata"]
    assert raw["metadata"]["image_assets"]


@pytest.mark.asyncio
async def test_pending_media_record_projects_video_for_model_only(tmp_path):
    pipeline = _make_pipeline(tmp_path, videos=True)
    video_ref = await _video_ref(tmp_path, pipeline)
    raw = {
        "role": "user",
        "content": "刚来的视频",
        "conversation_id": "private:1",
        "metadata": {
            "messages": [{"message_id": "m1", "inbound_seq": 1}],
            "video_assets": [video_ref],
        },
    }

    projected = await MessagePipeline._project_pending_media_record(
        pipeline, raw
    )

    assert isinstance(projected["content"], list)
    assert projected["content"][1]["text"].startswith("[视频所属消息")
    assert projected["content"][2]["type"] == "video_url"
    assert raw["content"] == "刚来的视频"
    assert raw["metadata"]["video_assets"] == [video_ref]


@pytest.mark.asyncio
async def test_video_request_over_count_limit_is_rejected_before_provider(tmp_path):
    pipeline = _make_pipeline(
        tmp_path,
        videos=True,
        video_limits=VideoLimits(max_videos_per_request=1),
    )
    refs = [
        await _video_ref(tmp_path, pipeline, index=0),
        await _video_ref(tmp_path, pipeline, index=1),
    ]
    usage = pipeline._main_prompt_budget_usage(
        _video_messages(refs),
        None,
        100_000,
    )

    code, message = pipeline._main_prompt_budget_failure(usage)
    assert code == "video_request_count_exceeded"
    assert message == "请求视频数量超过限制"
    assert usage.video_count == 2


@pytest.mark.asyncio
async def test_first_and_retry_success_both_go_through_finalize(tmp_path):
    async def run_branch(tmp_path, mode: str):
        pipeline = _make_pipeline(
            tmp_path,
            max_context_tokens=2_000,
            reserve_output_tokens=100,
        )
        pipeline.chat_agent.cfg.max_tokens = 256
        ref = await _ref(tmp_path, pipeline)
        compacted = {"value": False}
        calls: list[str] = []

        async def first_target(*args, **kwargs):
            return 10

        async def retry_target(*args, **kwargs):
            return 5

        async def compact(*, target_after_tokens, reason, **kwargs):
            calls.append(reason)
            if (mode == "first" and "budget_first" in reason) or (
                mode == "retry" and "retry" in reason
            ):
                compacted["value"] = True
            return SimpleNamespace(
                status="success",
                reason="",
                archived_count=1,
                success=True,
            )

        def rebuild():
            text = "短" if compacted["value"] else "长" * 6000
            return asyncio.sleep(0, result=_messages([ref], text=text))

        pipeline._first_budget_retry_target = first_target
        pipeline._retry_budget_target = retry_target
        pipeline._compact_active_history = compact
        result = await MessagePipeline._prepare_main_prompt_for_model(
            pipeline,
            conversation_id="private:1",
            phase="test",
            tools_schema=None,
            rebuild_messages=rebuild,
        )
        assert result.ok is True, result.failure_code
        assert _wire_has_image(result.messages)
        assert result.contains_images is True
        return calls

    first_calls = await run_branch(tmp_path / "first", "first")
    assert any("budget_first" in reason for reason in first_calls)
    assert not any("retry" in reason for reason in first_calls)

    retry_calls = await run_branch(tmp_path / "retry", "retry")
    assert any("budget_first" in reason for reason in retry_calls)
    assert any("retry" in reason for reason in retry_calls)


@pytest.mark.asyncio
async def test_invalid_refs_and_multidimensional_excess_fail_before_store(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    ref = await _ref(tmp_path, pipeline)
    bad_ref = dict(ref)
    bad_ref["sha256"] = "not-a-sha"

    async def no_rebuild():
        return _messages([bad_ref])

    result = await MessagePipeline._prepare_main_prompt_for_model(
        pipeline,
        conversation_id="private:1",
        phase="test",
        tools_schema=None,
        rebuild_messages=no_rebuild,
    )
    assert result.ok is False
    assert result.failure_code == "invalid_asset_reference"
    assert pipeline._image_asset_store_instance is None
    assert all("base64" not in str(message.get("content")) for message in result.messages)

    count_pipeline = _make_pipeline(
        tmp_path / "count",
        limits=ImageLimits(max_images_per_request=1),
    )
    ref_a = await _ref(tmp_path / "count", count_pipeline, index=0)
    ref_b = await _ref(tmp_path / "count", count_pipeline, index=1)
    compact_calls = {"value": 0}

    async def unchanged_compact(**kwargs):
        compact_calls["value"] += 1
        return SimpleNamespace(
            status="success",
            reason="",
            archived_count=1,
            success=True,
        )

    count_pipeline._compact_active_history = unchanged_compact
    count_pipeline._first_budget_retry_target = lambda *args, **kwargs: asyncio.sleep(
        0, result=10
    )
    count_pipeline._retry_budget_target = lambda *args, **kwargs: asyncio.sleep(
        0, result=5
    )
    count_result = await MessagePipeline._prepare_main_prompt_for_model(
        count_pipeline,
        conversation_id="private:1",
        phase="test",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(0, result=_messages([ref_a, ref_b])),
    )
    assert count_result.ok is False
    assert count_result.failure_code == "image_request_count_exceeded"
    assert count_pipeline._image_asset_store_instance is None
    assert compact_calls["value"] >= 1

    byte_pipeline = _make_pipeline(
        tmp_path / "bytes",
        limits=ImageLimits(max_encoded_image_bytes=10),
    )
    byte_ref = await _ref(tmp_path / "bytes", byte_pipeline)
    byte_pipeline._compact_active_history = unchanged_compact
    byte_pipeline._first_budget_retry_target = lambda *args, **kwargs: asyncio.sleep(
        0, result=10
    )
    byte_pipeline._retry_budget_target = lambda *args, **kwargs: asyncio.sleep(
        0, result=5
    )
    byte_result = await MessagePipeline._prepare_main_prompt_for_model(
        byte_pipeline,
        conversation_id="private:1",
        phase="test",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(0, result=_messages([byte_ref])),
    )
    assert byte_result.ok is False
    assert byte_result.failure_code == "image_request_bytes_exceeded"
    assert byte_pipeline._image_asset_store_instance is None


def test_protected_input_source_check_and_cut_limit():
    guard = ProtectedInput(
        conversation_id="private:1",
        message_ids=frozenset({"m1"}),
        inbound_seqs=frozenset({1}),
    )
    pipeline_obj = MessagePipeline.__new__(MessagePipeline)
    pipeline_obj._active_input_guards = [guard]
    source = _messages([], text="x")
    assert MessagePipeline._missing_protected_input(
        pipeline_obj, source, "private:1"
    ) is False
    assert MessagePipeline._missing_protected_input(
        pipeline_obj, [{"role": "user", "content": "x"}], "private:1"
    ) is True

    records = [
        {
            "role": "user",
            "content": f"r{index}",
            "conversation_id": "private:1",
            "metadata": {"messages": [{"message_id": f"m{index}", "inbound_seq": index}]},
        }
        for index in range(3)
    ]
    pipeline_obj._model_view_policy = lambda: ModelViewPolicy()
    view = build_model_view(records, policy=ModelViewPolicy())
    pipeline_obj._active_input_guards = [
        ProtectedInput("private:1", frozenset({"m2"}), frozenset({2}))
    ]
    max_cut, reason = MessagePipeline._protected_max_view_cut(
        pipeline_obj, view, records
    )
    assert reason == ""
    assert max_cut == 2
    sliced = MessagePipeline._select_compaction_slice(
        view.records,
        active_tokens=100,
        target_after_tokens=1,
        estimator=SimpleNamespace(estimate_messages=lambda msgs: 100),
        max_view_cut=max_cut,
    )
    assert len(sliced) <= 2


@pytest.mark.asyncio
async def test_summary_triggered_by_image_count_and_bytes(tmp_path):
    refs = []
    for index in range(3):
        holder = _make_pipeline(tmp_path / f"ref{index}")
        refs.append(
            await _ref(
                tmp_path / f"ref{index}",
                holder,
                index=0,
                message_id=f"m{index}",
            )
        )

    async def make_summary_pipeline(limits: ImageLimits):
        pipeline = _make_pipeline(tmp_path / "summary", limits=limits)
        records = [_messages([ref], text=f"消息{index}")[1] for index, ref in enumerate(refs)]
        for index, record in enumerate(records):
            record["metadata"]["messages"][0]["message_id"] = f"m{index}"
            record["metadata"]["messages"][0]["inbound_seq"] = index

        async def records_fn():
            return copy.deepcopy(records)

        pipeline.history = SimpleNamespace(records=records_fn)
        pipeline.summary_agent = SimpleNamespace()
        pipeline.archive = SimpleNamespace()
        pipeline.rolling_summary = SimpleNamespace()
        pipeline.important = SimpleNamespace()
        pipeline._active_start_index_for_records = lambda raw: 0
        pipeline._fixed_prompt_overhead_tokens = lambda: 0
        pipeline._model_view_policy = lambda: ModelViewPolicy()
        pipeline.behavior_cfg.summarize = SimpleNamespace(
            trigger_at_tokens=10**9,
            trigger_at_context_percent=50,
            target_after_tokens=None,
            target_after_context_percent=30,
        )
        calls = {"count": 0}

        async def compact(**kwargs):
            calls["count"] += 1
            return SummaryAttemptResult(status="success", reason=kwargs["reason"])

        pipeline._compact_active_history_unlocked = compact
        return pipeline, calls

    count_pipeline, count_calls = await make_summary_pipeline(
        ImageLimits(max_images_per_request=2)
    )
    count_result = await MessagePipeline._maybe_summarize_unlocked(
        count_pipeline,
        force=False,
        target_after_tokens=None,
        reason="image_count",
    )
    assert count_result.status == "success"
    assert count_calls["count"] == 1

    byte_pipeline, byte_calls = await make_summary_pipeline(
        ImageLimits(max_encoded_image_bytes=10)
    )
    byte_result = await MessagePipeline._maybe_summarize_unlocked(
        byte_pipeline,
        force=False,
        target_after_tokens=None,
        reason="image_bytes",
    )
    assert byte_result.status == "success"
    assert byte_calls["count"] == 1


@pytest.mark.asyncio
async def test_summary_respects_missing_protected_input(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    record = _messages([], text="x")[1]
    record["metadata"]["messages"][0]["message_id"] = "other"
    record["metadata"]["messages"][0]["inbound_seq"] = 99

    async def records_fn():
        return [record]

    pipeline.history = SimpleNamespace(records=records_fn)
    pipeline.summary_agent = SimpleNamespace()
    pipeline.archive = SimpleNamespace()
    pipeline.rolling_summary = SimpleNamespace()
    pipeline.important = SimpleNamespace()
    pipeline._active_start_index_for_records = lambda raw: 0
    pipeline._fixed_prompt_overhead_tokens = lambda: 0
    pipeline._model_view_policy = lambda: ModelViewPolicy()
    pipeline.behavior_cfg.summarize = SimpleNamespace(
        trigger_at_tokens=1,
        trigger_at_context_percent=50,
        target_after_tokens=None,
        target_after_context_percent=30,
    )
    pipeline._active_input_guards = [
        ProtectedInput("private:1", frozenset({"m1"}), frozenset({1}))
    ]

    result = await MessagePipeline._maybe_summarize_unlocked(
        pipeline,
        force=True,
        target_after_tokens=None,
        reason="protected",
    )
    assert result.status == "failed"
    assert result.reason == "protected_input_missing"


@pytest.mark.asyncio
async def test_calibration_is_skipped_only_for_multimodal_requests(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    pipeline._calibrate_tokens(1000, 1200, contains_images=True)
    assert pipeline._token_calib_ratio == 1.0
    assert pipeline._last_calibration_skipped_reason == "multimodal_prompt"

    pipeline._calibrate_tokens(1000, 1200, contains_images=False)
    assert pipeline._token_calib_ratio != 1.0


@pytest.mark.asyncio
async def test_runner_last_guard_blocks_over_count_without_network(tmp_path):
    pipeline = _make_pipeline(
        tmp_path,
        limits=ImageLimits(max_images_per_request=1),
    )
    ref_a = await _ref(tmp_path, pipeline, index=0)
    ref_b = await _ref(tmp_path, pipeline, index=1)
    store = ImageAssetStore(
        pipeline.workspace_dir,
        limits=pipeline.image_limits,
    )
    source = _messages([ref_a, ref_b])
    projected = await store.project_records(source)
    calls = {"count": 0}

    class CountingProvider:
        name = "fake"

        async def chat_completion(self, *args, **kwargs):
            calls["count"] += 1
            raise AssertionError("超限 payload 不应触达 provider")

    runner = AgentRunner(
        CountingProvider(),
        AgentConfig(
            provider="fake",
            model="fake-1",
            temperature=0.6,
            max_tokens=64,
            max_loops=1,
            refocus_interval=0,
            first_token_timeout_seconds=1.0,
        ),
    )

    async def executor(tool_name, args, *, tool_call_id=None):
        return {"ok": True}

    result = await runner.run(
        projected,
        tools=None,
        tool_executor=executor,
        image_limits=pipeline.image_limits,
    )
    assert calls["count"] == 0
    assert result.finish_reason == "api_error"


def _history_image_record(index: int, ref: dict) -> dict:
    return {
        "role": "user",
        "content": f"第{index}张图",
        "conversation_id": "private:1",
        "metadata": {
            "messages": [{"message_id": f"m{index}", "inbound_seq": index}],
            "image_assets": [ref],
        },
    }


@pytest.mark.asyncio
async def test_real_compaction_exits_whole_records_and_respects_protected_input(tmp_path):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[dict]] = []

        async def summarize_rolling(self, history_slice, existing_summary, important):
            self.calls.append(list(history_slice))
            return {"summary_text": "图片历史摘要", "new_important": []}

    class FakeImportant:
        def text(self) -> str:
            return ""

        async def save(self, *args, **kwargs):
            return {"saved": False}

    async def build_ready_pipeline(root, *, image_limit: int):
        pipeline = _make_pipeline(
            root,
            limits=ImageLimits(max_images_per_request=image_limit),
        )
        history = HistoryManager(root / "history.jsonl")
        archive = ArchiveStore(root / "archive.sqlite3")
        rolling = RollingSummaryStore(root / "rolling.json")
        await history.load()
        await archive.load()
        await rolling.load()
        refs = []
        for index in range(3):
            holder = _make_pipeline(root / f"holder{index}")
            refs.append(
                await _ref(
                    root / f"holder{index}",
                    holder,
                    index=0,
                    message_id=f"m{index}",
                )
            )
        records = [
            _history_image_record(index, ref)
            for index, ref in enumerate(refs)
        ]
        await history.add_records(records, conversation_id="private:1")
        pipeline.history = history
        pipeline.archive = archive
        pipeline.rolling_summary = rolling
        pipeline.summary_agent = FakeSummaryAgent()
        pipeline.important = FakeImportant()
        # _make_pipeline 为其它用例注入了 no-op；这里恢复真实的摘要方法。
        pipeline.__dict__.pop("_maybe_summarize", None)
        pipeline._fixed_prompt_overhead_tokens = lambda: 0
        pipeline.behavior_cfg.summarize = SimpleNamespace(
            trigger_at_tokens=10**9,
            trigger_at_context_percent=50,
            target_after_tokens=None,
            target_after_context_percent=30,
        )
        return pipeline, history, archive, rolling, records

    pipeline, history, archive, rolling, records = await build_ready_pipeline(
        tmp_path / "ok",
        image_limit=2,
    )
    result = await pipeline._maybe_summarize()

    assert result.status == "success", (result.status, result.reason)
    assert rolling.active_start_index() == 1
    archived = await archive.records()
    assert len(archived) == 1
    assert "[图片所属消息" not in str(archived)
    assert "base64" not in str(archived)
    active = (await history.records())[rolling.active_start_index() :]
    active_refs = [
        ref
        for record in active
        for ref in record.get("metadata", {}).get("image_assets", [])
    ]
    assert len(active_refs) == 2

    protected_pipeline, _, protected_archive, protected_rolling, _ = (
        await build_ready_pipeline(tmp_path / "protected", image_limit=1)
    )
    protected_pipeline._active_input_guards = [
        ProtectedInput("private:1", frozenset({"m0"}), frozenset({0}))
    ]
    protected_result = await protected_pipeline._maybe_summarize()

    assert protected_result.status == "failed"
    assert protected_result.reason == "protected_input_budget"
    assert protected_rolling.active_start_index() == 0
    assert await protected_archive.records() == []



@pytest.mark.asyncio
async def test_native_off_keeps_text_and_never_loads_images(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    pipeline.main_model_images_enabled = False
    pipeline._image_budget_profile = None
    ref = await _ref(tmp_path, pipeline)

    def forbidden_store():
        raise AssertionError("native 关闭时不得加载图片 Store")

    pipeline._image_asset_store = forbidden_store
    result = await MessagePipeline._prepare_main_prompt_for_model(
        pipeline,
        conversation_id="private:1",
        phase="native_off",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(0, result=_messages([ref])),
    )

    assert result.ok is True
    assert result.contains_images is False
    assert result.image_count == 0
    assert isinstance(result.messages[-1]["content"], str)
    assert "base64" not in json.dumps(result.messages, ensure_ascii=False)


@pytest.mark.asyncio
async def test_both_switches_on_still_use_native_without_auto_vision_call(tmp_path):
    class FakeVision:
        def __init__(self) -> None:
            self.calls = 0

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.calls += 1
            return "不应自动调用"

    pipeline = _make_pipeline(tmp_path)
    vision = FakeVision()
    pipeline.vision = vision
    ref = await _ref(tmp_path, pipeline)
    result = await MessagePipeline._prepare_main_prompt_for_model(
        pipeline,
        conversation_id="private:1",
        phase="both_on",
        tools_schema=None,
        rebuild_messages=lambda: asyncio.sleep(0, result=_messages([ref])),
    )

    assert result.ok is True
    assert result.contains_images is True
    assert _wire_has_image(result.messages)
    assert vision.calls == 0


@pytest.mark.asyncio
async def test_twelve_round_stable_prefix_with_images(tmp_path):
    from agents import Persona
    from agents.context_builder import build_messages, first_tail_block_index
    from utils.request_history import partition_request_history

    pipeline = _make_pipeline(tmp_path)
    store = ImageAssetStore(
        pipeline.workspace_dir,
        limits=pipeline.image_limits,
    )
    refs = []
    for index in range(3):
        refs.append(
            await store.ingest(
                _png_bytes(20 + index, 20 + index),
                message_id=f"m{index}",
                index=0,
            )
        )
    policy = ModelViewPolicy()
    persona = Persona(
        name="vision_stable",
        prompt="<identity>稳定前缀测试</identity>",
        vars={"name": "测试", "admins": []},
    )
    records: list[dict] = []
    previous_stable: list[dict] = []
    stable_lengths: list[int] = []
    for round_index in range(12):
        records.append(
            {
                "role": "user",
                "content": f"第{round_index}轮图片消息",
                "conversation_id": "private:1",
                "metadata": {
                    "messages": [
                        {
                            "message_id": f"m{round_index}",
                            "inbound_seq": round_index,
                        }
                    ],
                    "image_assets": [refs[round_index % len(refs)]],
                },
            }
        )
        records.append(
            {
                "role": "assistant",
                "content": f"第{round_index}轮回复",
            }
        )
        view = build_model_view(records, policy=policy)
        parts = partition_request_history(
            view.records,
            safe_no_action_groups=view.safe_no_action_groups,
            policy=policy,
        )
        messages = build_messages(
            persona=persona,
            history=view.records,
            current_context=f"<task_context>第{round_index}轮</task_context>",
            cache_friendly_history=True,
            model_view_policy=policy,
            request_history=parts,
        )
        stable_end = first_tail_block_index(messages)
        stable = messages[:stable_end]
        if previous_stable:
            assert stable[: len(previous_stable)] == previous_stable
        previous_stable = stable
        stable_lengths.append(len(stable))
        dumped = json.dumps(messages, ensure_ascii=False)
        assert "base64" not in dumped
        user_messages = [
            message for message in messages if message.get("role") == "user"
        ]
        assert any(
            message.get("metadata", {}).get("image_assets")
            for message in user_messages
        )

    assert stable_lengths == sorted(stable_lengths)
    assert stable_lengths[-1] > stable_lengths[0]


@pytest.mark.asyncio
async def test_final_tool_loop_guard_blocks_images_without_network(tmp_path):
    pipeline = _make_pipeline(
        tmp_path,
        limits=ImageLimits(max_images_per_request=1),
    )
    ref_a = await _ref(tmp_path, pipeline, index=0)
    ref_b = await _ref(tmp_path, pipeline, index=1)
    store = ImageAssetStore(
        pipeline.workspace_dir,
        limits=pipeline.image_limits,
    )
    projected = await store.project_records(_messages([ref_a, ref_b]))
    calls = {"count": 0}

    class CountingProvider:
        name = "fake"

        async def chat_completion(self, *args, **kwargs):
            calls["count"] += 1
            raise AssertionError("超限收尾不应触达 provider")

    runner = AgentRunner(
        CountingProvider(),
        AgentConfig(
            provider="fake",
            model="fake-1",
            temperature=0.6,
            max_tokens=64,
            max_loops=1,
            refocus_interval=0,
            first_token_timeout_seconds=1.0,
        ),
    )
    result = await runner._finalize_tool_loop(
        projected,
        usage_recorder=None,
        status_callback=None,
        status_label="test",
        image_limits=pipeline.image_limits,
    )

    assert result is None
    assert calls["count"] == 0


# ============================================================
# C1：真实合并批次的原始消息归属
# ============================================================


class _ResolverAdapter:
    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    async def get_image_url(self, file_id: str) -> str:
        return self.paths[file_id]


def _incoming_image_event(message_id: str, file_id: str) -> IncomingMessage:
    return IncomingMessage(
        adapter="fake",
        timestamp=1.0,
        self_id="999",
        message_id=message_id,
        scope="private",
        user_id="123",
        nickname="用户",
        text="[图片]",
        raw_message="[图片]",
        media=[MediaSegment(type=MediaType.IMAGE, file_id=file_id)],
    )


async def _prepare_merged_batch(
    build_pipeline,
    tmp_path,
    counts: list[int],
):
    paths = {}
    for origin, count in enumerate(counts):
        for index in range(count):
            file_id = f"img-{origin}-{index}"
            path = tmp_path / f"{file_id}.png"
            path.write_bytes(_png_bytes(20 + origin, 20 + index))
            paths[file_id] = str(path)

    workspace = tmp_path / "workspace"
    pipeline, provider, _, history, _ = await build_pipeline(
        [],
        workspace_dir=workspace,
    )
    pipeline.adapter = _ResolverAdapter(paths)
    pipeline.main_model_images_enabled = True
    pipeline.image_limits = ImageLimits()
    pipeline._image_budget_profile = _profile()
    pipeline._image_asset_store_instance = None

    items = []
    for origin, count in enumerate(counts):
        message_id = f"m{origin}"
        event = _incoming_image_event(
            message_id,
            f"img-{origin}-0",
        )
        event.media = [
            MediaSegment(type=MediaType.IMAGE, file_id=f"img-{origin}-{index}")
            for index in range(count)
        ]
        await pipeline._build_readable_text(event)
        items.append(
            PendingMessageItem(
                message_id=message_id,
                user_id="123",
                nickname="用户",
                location="私聊",
                text=event.text,
                conversation_id="private:123",
                inbound_seq=origin,
                received_at=1.0,
                raw_event=event,
            )
        )
    await pipeline._process_batch(items)
    return pipeline, provider, history, items


def _provider_image_blocks(provider) -> list[dict]:
    blocks = []
    for call in provider.calls:
        for message in call["messages"]:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            blocks.extend(
                block
                for block in content
                if isinstance(block, dict) and block.get("type") == "image_url"
            )
    return blocks


@pytest.mark.asyncio
async def test_merged_batch_two_origins_same_index_reaches_provider(
    build_pipeline,
    tmp_path,
):
    _, provider, history, items = await _prepare_merged_batch(
        build_pipeline,
        tmp_path,
        [1, 1],
    )
    assert len(provider.calls) == 1
    assert len(_provider_image_blocks(provider)) == 2
    assert [asset["message_id"] for item in items for asset in item.raw_event.image_assets] == [
        "m0",
        "m1",
    ]
    assert [asset["index"] for item in items for asset in item.raw_event.image_assets] == [0, 0]
    dumped = json.dumps(await history.records(), ensure_ascii=False)
    assert "base64" not in dumped


@pytest.mark.asyncio
async def test_merged_batch_three_plus_two_passes_per_origin_limit(
    build_pipeline,
    tmp_path,
):
    _, provider, _, _ = await _prepare_merged_batch(
        build_pipeline,
        tmp_path,
        [3, 2],
    )
    assert len(provider.calls) == 1
    blocks = _provider_image_blocks(provider)
    assert len(blocks) == 5
    assert all(
        block["image_url"]["url"].startswith("data:image/png;base64,")
        for block in blocks
    )


@pytest.mark.asyncio
async def test_merged_batch_24_images_rejected_before_provider(
    build_pipeline,
    tmp_path,
):
    _, provider, _, _ = await _prepare_merged_batch(
        build_pipeline,
        tmp_path,
        [4, 4, 4, 4, 4, 4],
    )
    assert provider.calls == []
