"""完整视频契约：格式识别、归属校验、预算视图与 wire 护栏。"""

from __future__ import annotations

import copy

import pytest

from utils.video_budget import (
    assert_wire_video_request_fits,
    estimate_video_refs_budget,
    resolve_video_budget_profile,
    wire_video_metrics,
)
from utils.video_contracts import (
    VIDEO_TRANSFORM_VERSION,
    VideoAssetError,
    VideoLimits,
    build_video_budget_messages,
    detect_video_mime,
    group_validated_video_refs,
    validate_declared_video_assets,
    validate_video_asset_ref,
    video_asset_relative_path,
)


def _mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + b"\x00" * 256


def _ref(
    index: int = 0,
    *,
    byte_size: int = 1024,
    message_id: str = "m1",
    mime: str = "video/mp4",
) -> dict:
    sha = f"{index + 1:064x}"
    return {
        "sha256": sha,
        "mime": mime,
        "byte_size": byte_size,
        "path": video_asset_relative_path(sha, mime),
        "message_id": message_id,
        "index": index,
        "transform_version": VIDEO_TRANSFORM_VERSION,
    }


def _source(refs: list[dict], *, text: str = "看视频") -> dict:
    return {
        "role": "user",
        "content": text,
        "conversation_id": "private:1",
        "metadata": {
            "messages": [{"message_id": "m1", "inbound_seq": 1}],
            "video_assets": refs,
        },
    }


# ============================================================
# 格式识别
# ============================================================


def test_detect_video_mime_by_magic_not_extension():
    mp4 = _mp4_bytes()
    assert detect_video_mime(mp4) == "video/mp4"
    assert detect_video_mime(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 16) == (
        "video/quicktime"
    )
    assert detect_video_mime(b"\x1aE\xdf\xa3" + b"\x00" * 64 + b"webm") == (
        "video/webm"
    )
    assert detect_video_mime(b"\x00\x00\x01\xba" + b"\x00" * 64) == "video/mpeg"
    assert detect_video_mime(b"not a video at all") is None
    # Matroska 与 WebM 共用 EBML；未确认 provider 支持 MKV 前不误判。
    assert detect_video_mime(b"\x1aE\xdf\xa3" + b"\x00" * 64 + b"matroska") is None


# ============================================================
# ref 与归属
# ============================================================


def test_video_ref_validation_accepts_managed_path_and_rejects_tampering():
    ref = _ref()
    assert validate_video_asset_ref(ref) == ref

    with pytest.raises(VideoAssetError):
        validate_video_asset_ref({**ref, "path": "../x.mp4"})
    with pytest.raises(VideoAssetError):
        validate_video_asset_ref({**ref, "mime": "video/x-msvideo"})
    with pytest.raises(VideoAssetError):
        validate_video_asset_ref({**ref, "byte_size": 0})
    with pytest.raises(VideoAssetError):
        validate_video_asset_ref({**ref, "sha256": "not-a-hash"})
    with pytest.raises(VideoAssetError):
        validate_video_asset_ref({**ref, "extra": 1})
    with pytest.raises(VideoAssetError):
        validate_video_asset_ref(
            {**ref, "byte_size": 2048},
            limits=VideoLimits(max_video_bytes=1024),
        )


def test_declared_video_assets_enforces_per_message_and_request_limits():
    with pytest.raises(VideoAssetError) as per_message:
        validate_declared_video_assets(
            [_source([_ref(0), _ref(1)])],
            limits=VideoLimits(max_videos_per_message=1),
        )
    assert per_message.value.code == "video_request_count_exceeded"

    with pytest.raises(VideoAssetError) as per_request:
        validate_declared_video_assets(
            [
                _source([_ref(0)], text="a"),
                {
                    **_source([_ref(0, message_id="m2")], text="b"),
                    "metadata": {
                        "messages": [{"message_id": "m2", "inbound_seq": 2}],
                        "video_assets": [_ref(0, message_id="m2")],
                    },
                },
            ],
            limits=VideoLimits(max_videos_per_request=1),
        )
    assert per_request.value.code == "video_request_count_exceeded"


def test_video_origin_must_match_a_unique_original_message():
    source = _source([_ref(0, message_id="missing")])
    with pytest.raises(VideoAssetError) as error:
        validate_declared_video_assets([source])
    assert error.value.code == "invalid_video_asset"

    duplicate = _source([_ref(0), _ref(0)])
    with pytest.raises(VideoAssetError) as duplicate_error:
        validate_declared_video_assets([duplicate])
    assert duplicate_error.value.code == "invalid_video_asset"


# ============================================================
# 预算视图与 wire
# ============================================================


def test_video_budget_view_is_lightweight_and_preserves_order():
    source = _source([_ref(0)], text="看视频")
    original = copy.deepcopy(source)
    grouped = group_validated_video_refs([source])

    budget = build_video_budget_messages([source], grouped)
    content = budget[0]["content"]
    assert content[0] == {"type": "text", "text": "看视频"}
    assert content[1] == {
        "type": "text",
        "text": "[视频所属消息 msg_id=m1 index=0]",
    }
    assert content[2] == {
        "type": "video_url",
        "video_url": {"url": "<managed-video>"},
    }
    assert "base64" not in str(budget)
    assert source == original


def test_video_budget_view_appends_to_materialized_image_content():
    source = _source([_ref(0)], text="图文")
    source["content"] = [
        {"type": "text", "text": "图文"},
        {"type": "image_url", "image_url": {"url": "<managed-image>"}},
    ]
    grouped = group_validated_video_refs([source])
    content = build_video_budget_messages([source], grouped)[0]["content"]
    assert content[1]["type"] == "image_url"
    assert content[-1]["video_url"]["url"] == "<managed-video>"


def test_video_budget_refuses_double_projection():
    source = _source([_ref(0)])
    source["content"] = [
        {"type": "video_url", "video_url": {"url": "<managed-video>"}}
    ]
    with pytest.raises(VideoAssetError, match="实体化"):
        build_video_budget_messages([source], {0: [_ref(0)]})


def test_video_budget_estimate_and_profile():
    assert resolve_video_budget_profile(enabled=False) is None
    profile = resolve_video_budget_profile(enabled=True, reserve_tokens=1024)
    estimate = estimate_video_refs_budget(
        [_ref(0, byte_size=300), _ref(1, byte_size=600)],
        profile=profile,
    )
    assert estimate.video_count == 2
    assert estimate.video_tokens == 2048
    assert estimate.exact is False
    assert estimate.encoded_video_bytes > 0


def test_wire_video_metrics_requires_managed_data_url_and_matching_count():
    refs = [_ref(0, byte_size=3)]
    url = "data:video/mp4;base64,AAAA"
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "x"},
            {"type": "video_url", "video_url": {"url": url}},
        ],
        "metadata": {"video_assets": refs},
    }
    assert wire_video_metrics([message]) == (1, len(url))

    with pytest.raises(VideoAssetError) as error:
        wire_video_metrics(
            [
                {
                    **message,
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {"url": "https://x/a.mp4"},
                        }
                    ],
                }
            ]
        )
    assert error.value.code == "video_projection_failed"

    with pytest.raises(VideoAssetError) as mismatch:
        wire_video_metrics(
            [
                {
                    **message,
                    "content": [
                        {"type": "video_url", "video_url": {"url": url}},
                        {"type": "video_url", "video_url": {"url": url}},
                    ],
                }
            ]
        )
    assert mismatch.value.code == "video_projection_failed"


def test_assert_wire_video_request_fits_checks_count_bytes_and_body():
    assert assert_wire_video_request_fits([{"role": "user", "content": "纯文字"}]) == (
        0,
        0,
    )

    refs = [_ref(0, byte_size=3)]
    url = "data:video/mp4;base64,AAAA"
    message = {
        "role": "user",
        "content": [{"type": "video_url", "video_url": {"url": url}}],
        "metadata": {"video_assets": refs},
    }

    # VideoLimits 的最小值是 1；用两段视频触发请求级上限。
    with pytest.raises(VideoAssetError) as count_error:
        assert_wire_video_request_fits(
            [
                {
                    **message,
                    "content": [
                        {"type": "video_url", "video_url": {"url": url}},
                        {"type": "video_url", "video_url": {"url": url}},
                    ],
                    "metadata": {
                        "messages": [{"message_id": "m1"}],
                        "video_assets": [_ref(0, byte_size=3), _ref(1, byte_size=3)],
                    },
                }
            ],
            limits=VideoLimits(max_videos_per_request=1),
        )
    assert count_error.value.code == "video_request_count_exceeded"

    with pytest.raises(VideoAssetError) as byte_error:
        assert_wire_video_request_fits(
            [message],
            limits=VideoLimits(max_encoded_video_bytes=5),
        )
    assert byte_error.value.code == "video_request_bytes_exceeded"

    with pytest.raises(VideoAssetError) as body_error:
        assert_wire_video_request_fits(
            [message],
            limits=VideoLimits(max_request_body_bytes=10),
        )
    assert body_error.value.code == "provider_request_body_exceeded"

    with pytest.raises(VideoAssetError) as combined_body_error:
        assert_wire_video_request_fits(
            [message],
            limits=VideoLimits(max_request_body_bytes=1000),
            encoded_image_bytes=1000,
        )
    assert combined_body_error.value.code == "provider_request_body_exceeded"
