"""C4 图片预算纯函数：reserve、预算视图、wire 指标与请求体护栏。"""

from __future__ import annotations

import copy

import pytest

from utils.image_budget import (
    assert_wire_request_fits,
    build_budget_messages,
    estimate_refs_budget,
    estimate_wire_request_body_bytes,
    group_validated_refs,
    resolve_image_budget_profile,
    wire_image_metrics,
)
from utils.image_contracts import (
    GIF_TRANSFORM_VERSION,
    TRANSFORM_VERSION_V2,
    ImageAssetError,
    ImageBudgetProfile,
    ImageLimits,
    asset_relative_path,
)


def _ref(index: int = 0, *, byte_size: int = 300, message_id: str = "m1") -> dict:
    sha = f"{index + 1:064x}"
    return {
        "sha256": sha,
        "mime": "image/png",
        "width": 32,
        "height": 24,
        "byte_size": byte_size,
        "path": asset_relative_path(TRANSFORM_VERSION_V2, sha, "image/png"),
        "detail": "auto",
        "message_id": message_id,
        "index": index,
        "transform_version": TRANSFORM_VERSION_V2,
    }


def _gif_ref(index: int = 0, *, byte_size: int = 300, message_id: str = "m1") -> dict:
    sha = f"{index + 11:064x}"
    return {
        "sha256": sha,
        "mime": "image/gif",
        "width": 32,
        "height": 24,
        "byte_size": byte_size,
        "path": asset_relative_path(GIF_TRANSFORM_VERSION, sha, "image/gif"),
        "detail": "auto",
        "message_id": message_id,
        "index": index,
        "transform_version": GIF_TRANSFORM_VERSION,
    }


def _source(refs: list[dict], *, text: str = "看这张图") -> dict:
    return {
        "role": "user",
        "content": text,
        "conversation_id": "private:1",
        "metadata": {"messages": [{"message_id": "m1", "inbound_seq": 1}], "image_assets": refs},
    }


def _profile(tokens: int = 256) -> ImageBudgetProfile:
    return ImageBudgetProfile(
        name="reserve_v1",
        per_image_tokens=tokens,
        basis="configured_reserve",
        exact=False,
    )


def test_reserve_profile_counts_each_ref_and_duplicate_occurrence():
    refs = [_ref(0, byte_size=300), _ref(0, byte_size=300), _ref(1, byte_size=600)]
    estimate = estimate_refs_budget(refs, profile=_profile(256))

    assert estimate.image_count == 3
    assert estimate.image_tokens == 768
    assert estimate.exact is False
    assert estimate.basis == "configured_reserve"
    assert estimate.encoded_image_bytes == (
        4 * ((300 + 2) // 3) + 256
    ) * 2 + 4 * ((600 + 2) // 3) + 256


def test_resolve_profile_disabled_has_no_image_tokens():
    assert resolve_image_budget_profile(enabled=False) is None
    assert resolve_image_budget_profile(enabled=True).per_image_tokens == 8192


def test_budget_view_matches_wire_order_without_base64_or_mutation():
    refs = [_ref(0), _ref(1, message_id="m1")]
    source = _source(refs)
    original = copy.deepcopy(source)

    grouped = group_validated_refs([source], enforce_request_limits=False)
    budget = build_budget_messages([source], grouped)

    content = budget[0]["content"]
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1] == {
        "type": "text",
        "text": "[图片所属消息 msg_id=m1 index=0]",
    }
    assert content[2]["type"] == "image_url"
    assert content[2]["image_url"]["url"] == "<managed-image>"
    assert content[4]["image_url"]["detail"] == "auto"
    assert "base64" not in str(budget)
    assert source == original
    assert budget[0]["metadata"] is not source["metadata"]


def test_budget_view_rejects_already_materialized_or_non_user_assets():
    source = _source([_ref(0)])
    source["content"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    with pytest.raises(ImageAssetError, match="实体化"):
        build_budget_messages([source], {0: [_ref(0)]})

    non_user = _source([_ref(0)])
    non_user["role"] = "assistant"
    with pytest.raises(ImageAssetError, match="真实用户消息"):
        build_budget_messages([non_user], {0: [_ref(0)]})


def test_wire_metrics_accepts_complete_gif_data_url():
    refs = [_gif_ref(0)]
    url = "data:image/gif;base64," + ("A" * 100)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "看这个动图"},
            {
                "type": "text",
                "text": "[GIF所属消息 msg_id=m1 index=0]",
            },
            {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
        ],
        "metadata": {"image_assets": refs},
    }

    assert wire_image_metrics([message]) == (1, len(url))
    assert assert_wire_request_fits([message]) == (1, len(url))


def test_wire_metrics_requires_managed_data_url_and_matching_count():
    refs = [_ref(0)]
    url = "data:image/png;base64," + ("A" * 100)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
        ],
        "metadata": {"image_assets": refs},
    }
    assert wire_image_metrics([message]) == (1, len(url))

    with pytest.raises(ImageAssetError) as error:
        wire_image_metrics(
            [{**message, "content": [{"type": "image_url", "image_url": {"url": "https://x/a.png"}}]}]
        )
    assert error.value.code == "image_projection_failed"

    with pytest.raises(ImageAssetError, match="数量"):
        wire_image_metrics([{**message, "content": [{"type": "text", "text": "x"}]}])


def test_assert_wire_request_fits_handles_count_bytes_body_and_no_image_fast_path():
    assert assert_wire_request_fits([{"role": "user", "content": "纯文字"}]) == (0, 0)

    refs = [_ref(0), _ref(1)]
    url = "data:image/png;base64," + ("A" * 100)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
            {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
        ],
        "metadata": {"image_assets": refs},
    }

    with pytest.raises(ImageAssetError) as count_error:
        assert_wire_request_fits(
            [message],
            limits=ImageLimits(max_images_per_request=1),
        )
    assert count_error.value.code == "image_request_count_exceeded"

    with pytest.raises(ImageAssetError) as byte_error:
        assert_wire_request_fits(
            [message],
            limits=ImageLimits(max_encoded_image_bytes=10),
        )
    assert byte_error.value.code == "image_request_bytes_exceeded"

    with pytest.raises(ImageAssetError) as body_error:
        assert_wire_request_fits(
            [message],
            limits=ImageLimits(max_request_body_bytes=1024),
        )
    assert body_error.value.code == "provider_request_body_exceeded"

    with pytest.raises(ImageAssetError) as mismatch_error:
        assert_wire_request_fits(
            [{**message, "metadata": {"image_assets": refs[:1]}}]
        )
    assert mismatch_error.value.code == "image_projection_failed"


def test_body_estimate_redacts_base64_and_includes_tools():
    refs = [_ref(0)]
    url = "data:image/png;base64," + ("A" * 10_000)
    message = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": url}}],
        "metadata": {"image_assets": refs},
    }
    small = estimate_wire_request_body_bytes(
        [message],
        [{"type": "function", "function": {"name": "x"}}],
        encoded_image_bytes=len(url),
    )
    # 估算不含第二份 10KB base64；仍会把实际编码量加一次。
    assert small < 10_000 * 2
    assert small >= len(url)
