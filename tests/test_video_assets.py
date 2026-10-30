"""完整视频资产存储：原始字节、权限、校验与投影。"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from features.vision.video_assets import VideoAssetStore
from utils.video_budget import assert_wire_video_request_fits
from utils.video_contracts import (
    VIDEO_TRANSFORM_VERSION,
    VideoAssetError,
    VideoLimits,
)


def _mp4_bytes(size: int = 2048) -> bytes:
    head = b"\x00\x00\x00\x18ftypisom"
    return head + (b"\xab" * max(0, size - len(head)))


def _store(tmp_path, **limits) -> VideoAssetStore:
    return VideoAssetStore(tmp_path / "workspace", limits=VideoLimits(**limits))


@pytest.mark.asyncio
async def test_ingest_preserves_complete_bytes_and_returns_valid_ref(tmp_path):
    store = _store(tmp_path)
    source = _mp4_bytes()
    ref = await store.ingest(source, message_id="m1", index=0)

    assert ref["mime"] == "video/mp4"
    assert ref["byte_size"] == len(source)
    assert ref["path"] == (
        f"incoming/videos/{VIDEO_TRANSFORM_VERSION}/"
        f"{hashlib.sha256(source).hexdigest()}.mp4"
    )
    path = tmp_path / "workspace" / ref["path"]
    assert path.read_bytes() == source
    if os.name != "nt":
        # Windows reports synthesized mode bits and does not support POSIX 0600.
        assert os.stat(path).st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_project_record_sends_full_video_data_url(tmp_path):
    store = _store(tmp_path)
    source = _mp4_bytes()
    ref = await store.ingest(source, message_id="m1", index=0)
    record = {
        "role": "user",
        "content": "看视频",
        "metadata": {
            "messages": [{"message_id": "m1"}],
            "video_assets": [ref],
        },
    }

    projected = await store.project_record(record)
    content = projected["content"]
    assert content[0] == {"type": "text", "text": "看视频"}
    assert content[1]["text"] == "[视频所属消息 msg_id=m1 index=0]"
    assert content[2]["type"] == "video_url"
    url = content[2]["video_url"]["url"]
    assert url.startswith("data:video/mp4;base64,")
    assert assert_wire_video_request_fits([projected])[0] == 1
    assert len(url) > len(source)
    assert record["content"] == "看视频"


@pytest.mark.asyncio
async def test_local_video_requires_trusted_local(tmp_path):
    store = _store(tmp_path)
    source = tmp_path / "clip.mp4"
    source.write_bytes(_mp4_bytes())

    with pytest.raises(VideoAssetError) as error:
        await store.ingest(str(source), message_id="m1", index=0)
    assert error.value.code in {"video_source_unavailable", "video_unsafe_source"}

    ref = await store.ingest(
        source, message_id="m1", index=0, trusted_local=True
    )
    assert ref["byte_size"] == source.stat().st_size


@pytest.mark.asyncio
async def test_ingest_rejects_unsupported_container_and_oversize(tmp_path):
    store = _store(tmp_path, max_video_bytes=1024)
    with pytest.raises(VideoAssetError) as format_error:
        await store.ingest(b"not a video", message_id="m1", index=0)
    assert format_error.value.code == "video_unsupported_format"

    with pytest.raises(VideoAssetError) as size_error:
        await store.ingest(_mp4_bytes(2048), message_id="m1", index=0)
    assert size_error.value.code == "video_too_large"


@pytest.mark.asyncio
async def test_tampered_asset_is_rejected_on_projection(tmp_path):
    store = _store(tmp_path)
    ref = await store.ingest(_mp4_bytes(), message_id="m1", index=0)
    path = tmp_path / "workspace" / ref["path"]
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\xcc" * 512)
    record = {
        "role": "user",
        "content": "看视频",
        "metadata": {
            "messages": [{"message_id": "m1"}],
            "video_assets": [ref],
        },
    }

    with pytest.raises(VideoAssetError) as error:
        await store.project_record(record)
    assert error.value.code == "video_asset_corrupt"


@pytest.mark.asyncio
async def test_existing_content_addressed_asset_is_not_overwritten(tmp_path):
    store = _store(tmp_path)
    source = _mp4_bytes()
    first = await store.ingest(source, message_id="m1", index=0)
    second = await store.ingest(source, message_id="m2", index=1)
    assert first["path"] == second["path"]
    path = tmp_path / "workspace" / first["path"]
    assert path.read_bytes() == source

    # 同一 sha 路径被篡改后，不允许用新内容静默覆盖。
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\xdd" * 512)
    with pytest.raises(VideoAssetError) as error:
        await store.ingest(source, message_id="m3", index=0)
    assert error.value.code == "video_asset_corrupt"


@pytest.mark.asyncio
async def test_projected_video_round_trips_without_metadata_mutation(tmp_path):
    store = _store(tmp_path)
    ref = await store.ingest(_mp4_bytes(), message_id="m1", index=0)
    metadata = {
        "messages": [{"message_id": "m1"}],
        "video_assets": [ref],
    }
    snapshot = json.loads(json.dumps(metadata))
    record = {"role": "user", "content": "x", "metadata": metadata}

    await store.project_record(record)
    assert metadata == snapshot
