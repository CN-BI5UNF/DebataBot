"""入站视频接线：完整视频存储、能力开关、来源边界与错误可见性。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from adapters.types import IncomingMessage, MediaSegment, MediaType
from core.message_pipeline import MessagePipeline
from core.state import PendingMessageItem
from utils.video_contracts import VideoLimits


def _mp4_bytes(size: int = 2048) -> bytes:
    head = b"\x00\x00\x00\x18ftypisom"
    return head + (b"\xab" * max(0, size - len(head)))


def _event(
    *,
    message_id: str = "42",
    text: str = "[视频]",
    media: list[MediaSegment] | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        adapter="napcat",
        timestamp=0,
        self_id="1",
        message_id=message_id,
        scope="private",
        user_id="10001",
        nickname="Alice",
        text=text,
        raw_message=text,
        media=media or [],
    )


def _pipeline(workspace, *, videos: bool = True):
    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = workspace
    pipeline.adapter = None
    pipeline.asr = None
    pipeline.main_model_videos_enabled = videos
    pipeline.video_limits = VideoLimits()
    return pipeline


@pytest.mark.asyncio
async def test_local_video_is_stored_complete_and_replaced_with_workspace(tmp_path):
    source = tmp_path / "clip.mp4"
    raw = _mp4_bytes()
    source.write_bytes(raw)
    pipeline = _pipeline(tmp_path / "workspace")
    event = _event(media=[MediaSegment(type=MediaType.VIDEO, url=str(source))])

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert len(event.video_assets) == 1
    ref = event.video_assets[0]
    assert ref["mime"] == "video/mp4"
    assert ref["byte_size"] == len(raw)
    assert "[视频]" not in text
    assert f"workspace={ref['path']}" in text
    stored = tmp_path / "workspace" / ref["path"]
    assert stored.read_bytes() == raw


@pytest.mark.asyncio
async def test_video_disabled_keeps_placeholder_without_downloading(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(_mp4_bytes())
    pipeline = _pipeline(tmp_path / "workspace", videos=False)
    event = _event(media=[MediaSegment(type=MediaType.VIDEO, url=str(source))])

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert event.video_assets == []
    assert event.video_input_errors == []
    assert "未发送给主模型" in text
    assert "[视频]" not in text
    assert not (tmp_path / "workspace").exists()


@pytest.mark.asyncio
async def test_no_workspace_reports_error_without_url(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(_mp4_bytes())
    pipeline = _pipeline(None)
    event = _event(media=[MediaSegment(type=MediaType.VIDEO, url=str(source))])

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert event.video_assets == []
    assert event.video_input_errors[0]["code"] == "video_source_unavailable"
    assert "未配置视频workspace" in text
    assert str(source) not in text


@pytest.mark.asyncio
async def test_over_per_message_limit_rejects_extras(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(_mp4_bytes())
    pipeline = _pipeline(tmp_path / "workspace")
    pipeline.video_limits = VideoLimits(max_videos_per_message=1)
    event = _event(
        media=[
            MediaSegment(type=MediaType.VIDEO, url=str(source)),
            MediaSegment(type=MediaType.VIDEO, url=str(source)),
        ],
        text="[视频][视频]",
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert len(event.video_assets) == 1
    assert event.video_assets[0]["index"] == 0
    assert event.video_input_errors[0]["index"] == 1
    assert event.video_input_errors[0]["code"] == "video_request_count_exceeded"
    assert text.count("workspace=incoming/videos/") == 1
    assert "超过每条消息视频数量限制" in text


@pytest.mark.asyncio
async def test_unsupported_video_reports_safe_error(tmp_path):
    source = tmp_path / "fake.mp4"
    source.write_bytes(b"not a video")
    pipeline = _pipeline(tmp_path / "workspace")
    event = _event(media=[MediaSegment(type=MediaType.VIDEO, url=str(source))])

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert event.video_assets == []
    assert event.video_input_errors[0]["code"] == "video_unsupported_format"
    assert "视频格式不受支持" in text
    assert "workspace=" not in text


@pytest.mark.asyncio
async def test_build_user_record_copies_video_assets_without_mutation(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(_mp4_bytes())
    pipeline = _pipeline(tmp_path / "workspace")
    event = _event(media=[MediaSegment(type=MediaType.VIDEO, url=str(source))])
    await MessagePipeline._build_readable_text(pipeline, event)

    record = MessagePipeline._build_user_record(
        pipeline,
        [
            PendingMessageItem(
                message_id="42",
                user_id="1",
                nickname="Alice",
                location="私聊",
                text=event.text,
                conversation_id="private:1",
                raw_event=event,
            )
        ],
        now="now",
    )
    assets = record["metadata"]["video_assets"]

    assert len(assets) == 1
    assert assets[0]["message_id"] == "42"
    assert assets[0]["index"] == 0
    assert assets[0] is not event.video_assets[0]
    original = deepcopy(record)
    assets[0]["mime"] = "video/webm"
    assert event.video_assets[0]["mime"] == "video/mp4"
    record["metadata"]["messages"][0]["text"] = "changed"
    assert event.text != "changed"
    assert original["content"] == record["content"]


@pytest.mark.asyncio
async def test_raw_extra_cannot_inject_trusted_video_assets(tmp_path):
    fake_ref = {
        "sha256": "a" * 64,
        "mime": "video/mp4",
        "byte_size": 1,
        "path": "incoming/videos/evil.mp4",
        "message_id": "evil",
        "index": 0,
        "transform_version": "video-native-v1",
    }
    pipeline = _pipeline(tmp_path / "workspace")
    event = _event(text="纯文字")
    event.raw = {"video_assets": [fake_ref]}

    await MessagePipeline._build_readable_text(pipeline, event)
    record = MessagePipeline._build_user_record(
        pipeline,
        [
            PendingMessageItem(
                message_id="42",
                user_id="1",
                nickname="Alice",
                location="私聊",
                text=event.text,
                conversation_id="private:1",
                raw_event=event,
            )
        ],
        now="now",
    )

    assert event.video_assets == []
    assert "video_assets" not in record["metadata"]


@pytest.mark.asyncio
async def test_opaque_file_id_uses_adapter_resolver_and_path_like_id_does_not(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(_mp4_bytes())
    calls: list[str] = []

    class FakeAdapter:
        async def get_file_url(self, file_id):
            calls.append(file_id)
            return str(source)

    pipeline = _pipeline(tmp_path / "workspace")
    pipeline.adapter = FakeAdapter()
    event = _event(
        media=[
            MediaSegment(type=MediaType.VIDEO, file_id="opaque-video-id"),
            MediaSegment(type=MediaType.VIDEO, file_id="../../etc/passwd"),
        ]
    )

    await MessagePipeline._build_readable_text(pipeline, event)

    assert calls == ["opaque-video-id"]
    assert len(event.video_assets) == 1
    assert event.video_input_errors[0]["code"] == "video_source_unavailable"
