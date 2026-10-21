"""入站图片接线：可信字段、来源边界、批次归属与错误可见性。"""

from __future__ import annotations

import io
from copy import deepcopy

import pytest
from PIL import Image

from adapters.types import IncomingMessage, MediaSegment, MediaType
from core.message_pipeline import MessagePipeline
from core.state import PendingMessageItem


def _png_bytes(width: int = 32, height: int = 24) -> bytes:
    image = Image.new("RGB", (width, height), (180, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _event(
    *,
    message_id: str = "42",
    text: str = "[图片]",
    media: list[MediaSegment] | None = None,
    raw: dict | None = None,
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
        raw=raw or {},
    )


def _pipeline(workspace):
    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = workspace
    pipeline.adapter = None
    pipeline.asr = None
    return pipeline


@pytest.mark.asyncio
async def test_path_like_file_id_never_reaches_resolver_or_local_trust(tmp_path):
    source = tmp_path / "secret.png"
    source.write_bytes(_png_bytes())
    calls: list[str] = []

    class FakeAdapter:
        async def get_image_url(self, file_id):
            calls.append(file_id)
            return str(source)

    pipeline = _pipeline(tmp_path / "workspace")
    pipeline.adapter = FakeAdapter()
    event = _event(
        media=[
            MediaSegment(type=MediaType.IMAGE, file_id="/etc/passwd"),
            MediaSegment(type=MediaType.IMAGE, file_id="file:///etc/passwd"),
            MediaSegment(type=MediaType.IMAGE, file_id="../../etc/passwd"),
        ]
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert calls == []
    assert event.image_assets == []
    assert len(event.image_input_errors) == 3
    assert text.count("图片未附加") == 3
    assert "workspace=" not in text


@pytest.mark.asyncio
async def test_no_workspace_does_not_create_store_or_emit_url(tmp_path):
    remote_url = "https://example.com/a.png"
    pipeline = _pipeline(None)
    event = _event(
        media=[MediaSegment(type=MediaType.IMAGE, file_id="opaque-id", url=remote_url)]
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert event.image_assets == []
    assert event.image_input_errors[0]["code"] == "image_source_unavailable"
    assert "未配置图片workspace" in text
    assert remote_url not in text
    assert getattr(pipeline, "_image_asset_store_instance", None) is None


@pytest.mark.asyncio
async def test_lazy_store_is_reused_and_does_not_create_directory_early(tmp_path):
    workspace = tmp_path / "workspace"
    pipeline = _pipeline(workspace)

    store = pipeline._image_asset_store()

    assert store is not None
    assert pipeline._image_asset_store() is store
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_over_per_message_limit_rejects_extras_with_original_indices(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(_png_bytes())

    class FakeAdapter:
        async def get_image_url(self, file_id):
            return str(source)

    pipeline = _pipeline(tmp_path / "workspace")
    pipeline.adapter = FakeAdapter()
    event = _event(
        media=[
            MediaSegment(type=MediaType.IMAGE, file_id=f"img-{index}")
            for index in range(5)
        ]
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert len(event.image_assets) == 4
    assert [ref["index"] for ref in event.image_assets] == [0, 1, 2, 3]
    assert len(event.image_input_errors) == 1
    assert event.image_input_errors[0]["index"] == 4
    assert event.image_input_errors[0]["code"] == "image_request_count_exceeded"
    assert text.count("workspace=incoming/images/") == 4
    assert "超过每条消息图片数量限制" in text


@pytest.mark.asyncio
async def test_corrupt_image_leaves_visible_safe_error(tmp_path):
    source = tmp_path / "broken.png"
    source.write_bytes(b"not an image")

    class FakeAdapter:
        async def get_image_url(self, file_id):
            return str(source)

    pipeline = _pipeline(tmp_path / "workspace")
    pipeline.adapter = FakeAdapter()
    event = _event(media=[MediaSegment(type=MediaType.IMAGE, file_id="bad-id")])

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert event.image_assets == []
    assert event.image_input_errors[0]["code"] == "image_asset_corrupt"
    assert "图片未附加" in text
    assert "not an image" not in text


@pytest.mark.asyncio
async def test_build_user_record_copies_assets_by_message_without_mutation(tmp_path):
    source_a = tmp_path / "a.png"
    source_a.write_bytes(_png_bytes())
    source_b = tmp_path / "b.png"
    source_b.write_bytes(_png_bytes(40, 20))

    class FakeAdapter:
        async def get_image_url(self, file_id):
            return str(source_a if file_id == "a" else source_b)

    pipeline = _pipeline(tmp_path / "workspace")
    pipeline.adapter = FakeAdapter()
    event_a = _event(message_id="ma", media=[MediaSegment(type=MediaType.IMAGE, file_id="a")])
    event_b = _event(message_id="mb", media=[MediaSegment(type=MediaType.IMAGE, file_id="b")])
    await MessagePipeline._build_readable_text(pipeline, event_a)
    await MessagePipeline._build_readable_text(pipeline, event_b)

    items = [
        PendingMessageItem(
            message_id="ma",
            user_id="1",
            nickname="Alice",
            location="私聊",
            text=event_a.text,
            conversation_id="private:1",
            raw_event=event_a,
        ),
        PendingMessageItem(
            message_id="mb",
            user_id="1",
            nickname="Alice",
            location="私聊",
            text=event_b.text,
            conversation_id="private:1",
            raw_event=event_b,
        ),
    ]
    record = MessagePipeline._build_user_record(pipeline, items, now="now")
    assets = record["metadata"]["image_assets"]

    assert [asset["message_id"] for asset in assets] == ["ma", "mb"]
    assert [asset["index"] for asset in assets] == [0, 0]
    assert assets[0] is not event_a.image_assets[0]
    original = deepcopy(record)
    assets[0]["width"] = 1
    assert event_a.image_assets[0]["width"] != 1
    record["metadata"]["messages"][0]["text"] = "changed"
    assert event_a.text != "changed"
    assert original["content"] == record["content"]


@pytest.mark.asyncio
async def test_raw_extra_cannot_inject_trusted_image_assets(tmp_path):
    fake_ref = {
        "sha256": "a" * 64,
        "mime": "image/png",
        "width": 1,
        "height": 1,
        "byte_size": 1,
        "path": "incoming/images/evil.png",
        "detail": "auto",
        "message_id": "evil",
        "index": 0,
        "transform_version": "pillow-first-frame-max2048-v2",
    }
    pipeline = _pipeline(tmp_path / "workspace")
    event = _event(text="纯文字", raw={"image_assets": [fake_ref]})

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

    assert event.image_assets == []
    assert "image_assets" not in record["metadata"]


def test_incoming_message_does_not_accept_image_assets_from_constructor():
    with pytest.raises(TypeError):
        IncomingMessage(
            adapter="napcat",
            timestamp=0,
            self_id="1",
            message_id="1",
            scope="private",
            user_id="1",
            nickname="A",
            image_assets=[],
        )
