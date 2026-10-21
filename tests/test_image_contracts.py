"""图片纯契约、限额与预算 profile 测试（不读文件/不联网）。"""

from __future__ import annotations

import copy

import pytest

from utils.image_budget import (
    estimate_refs_budget,
    estimate_request_body_bytes,
    resolve_image_budget_profile,
)
from utils.image_contracts import (
    DEFAULT_IMAGE_TOKEN_RESERVE,
    TRANSFORM_VERSION_V1,
    TRANSFORM_VERSION_V2,
    ImageAssetError,
    ImageLimits,
    asset_relative_path,
    build_image_attribution_text,
    declared_image_assets,
    encode_ref_bytes_estimate,
    has_declared_image_assets,
    transform_version_for_limits,
    validate_declared_assets,
    validate_image_asset_ref,
)


def _ref(**overrides):
    version = overrides.pop("transform_version", TRANSFORM_VERSION_V2)
    sha = overrides.pop("sha256", "a" * 64)
    mime = overrides.pop("mime", "image/png")
    ref = {
        "sha256": sha,
        "mime": mime,
        "width": 640,
        "height": 480,
        "byte_size": 12345,
        "path": asset_relative_path(version, sha, mime),
        "detail": "auto",
        "message_id": "m1",
        "index": 0,
        "transform_version": version,
    }
    ref.update(overrides)
    return ref


def test_image_limits_defaults_and_downward_only_validation():
    limits = ImageLimits()
    assert limits.max_images_per_message == 4
    assert limits.max_images_per_request == 20
    assert ImageLimits(max_image_side=1024).max_image_side == 1024
    with pytest.raises(ValueError):
        ImageLimits(max_image_bytes=8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        ImageLimits(max_images_per_message=True)
    with pytest.raises(ValueError):
        ImageLimits(cache_max_bytes=-1)
    assert ImageLimits(cache_max_bytes=0).cache_max_bytes == 0


def test_transform_version_includes_configured_side():
    assert (
        transform_version_for_limits(ImageLimits())
        == TRANSFORM_VERSION_V2
    )
    assert (
        transform_version_for_limits(ImageLimits(max_image_side=1024))
        == "pillow-first-frame-max1024-v2"
    )


def test_validate_valid_ref_returns_detached_copy():
    ref = _ref()
    original = copy.deepcopy(ref)
    validated = validate_image_asset_ref(ref)
    assert validated == ref
    validated["width"] = 1
    assert ref == original


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"mime": "image/webp"},
        {"transform_version": "unknown-v1"},
        {"path": "incoming/images/evil.png"},
        {"width": True},
        {"width": 0},
        {"height": 4096},
        {"height": 3000},
        {"byte_size": True},
        {"byte_size": 0},
        {"byte_size": 8 * 1024 * 1024 + 1},
        {"index": True},
        {"index": -1},
        {"detail": "ultra"},
        {"message_id": "bad\nid"},
        {"message_id": "x" * 300},
    ],
)
def test_validate_rejects_invalid_ref_fields(overrides):
    with pytest.raises(ImageAssetError):
        validate_image_asset_ref(_ref(**overrides))


def test_validate_rejects_unknown_ref_fields():
    with pytest.raises(ImageAssetError):
        validate_image_asset_ref(_ref(unexpected="value"))


def test_v1_assets_still_allow_larger_side_for_compatibility():
    ref = _ref(
        transform_version=TRANSFORM_VERSION_V1,
        width=3000,
        height=2000,
        path=asset_relative_path(TRANSFORM_VERSION_V1, "a" * 64, "image/png"),
    )
    validated = validate_image_asset_ref(ref)
    assert validated["width"] == 3000


def test_declared_image_assets_distinguishes_missing_empty_and_invalid():
    assert declared_image_assets({"role": "user", "content": "hi"}) is None
    assert declared_image_assets(
        {"role": "user", "content": "hi", "metadata": {"image_assets": []}}
    ) == []

    for value in ("bad", {"x": 1}, None):
        message = {"role": "user", "content": "hi", "metadata": {"image_assets": value}}
        with pytest.raises(ImageAssetError):
            declared_image_assets(message)

    assert not has_declared_image_assets([{"role": "user", "content": "hi"}])
    assert has_declared_image_assets(
        [{"role": "user", "content": "hi", "metadata": {"image_assets": [_ref()]}}]
    )


def test_validate_declared_assets_catches_bad_item_and_duplicate_index():
    good = {"role": "user", "content": "hi", "metadata": {"image_assets": [_ref()]}}
    assert validate_declared_assets([good])[0][1]["message_id"] == "m1"

    bad_ref = _ref()
    bad_ref["sha256"] = "bad"
    bad_item = {
        "role": "user",
        "content": "hi",
        "metadata": {"image_assets": [bad_ref]},
    }
    with pytest.raises(ImageAssetError):
        validate_declared_assets([bad_item])

    dup = {
        "role": "user",
        "content": "hi",
        "metadata": {"image_assets": [_ref(), _ref()]},
    }
    with pytest.raises(ImageAssetError):
        validate_declared_assets([dup])


def test_validate_declared_assets_enforces_per_message_and_request_counts():
    too_many_in_message = {
        "role": "user",
        "content": "hi",
        "metadata": {
            "image_assets": [
                _ref(index=index)
                for index in range(5)
            ]
        },
    }
    with pytest.raises(ImageAssetError):
        validate_declared_assets([too_many_in_message])

    messages = [
        {
            "role": "user",
            "content": f"m{message_index}",
            "metadata": {
                "image_assets": [
                    _ref(index=index, message_id=f"m{message_index}-{index}")
                    for index in range(4)
                ]
            },
        }
        for message_index in range(6)
    ]
    with pytest.raises(ImageAssetError):
        validate_declared_assets(
            messages,
            limits=ImageLimits(max_images_per_request=20),
        )


def test_attribution_text_and_encoded_byte_estimate():
    assert build_image_attribution_text(
        {"message_id": "m1", "index": 2}
    ) == "[图片所属消息 msg_id=m1 index=2]"
    assert encode_ref_bytes_estimate(3) == 4 + 256
    assert encode_ref_bytes_estimate(300) > 300


def test_reserve_profile_is_explicit_and_not_exact():
    disabled = resolve_image_budget_profile(enabled=False)
    assert disabled is None

    profile = resolve_image_budget_profile(enabled=True)
    assert profile is not None
    assert profile.name == "reserve_v1"
    assert profile.per_image_tokens == DEFAULT_IMAGE_TOKEN_RESERVE
    assert profile.exact is False
    assert profile.basis == "configured_reserve"

    custom = resolve_image_budget_profile(
        enabled=True,
        profile="reserve_v1",
        reserve_tokens=2048,
    )
    assert custom is not None and custom.per_image_tokens == 2048

    with pytest.raises(ValueError):
        resolve_image_budget_profile(enabled=True, profile="unknown")
    with pytest.raises(ValueError):
        resolve_image_budget_profile(enabled=True, reserve_tokens=True)


def test_estimate_refs_budget_counts_each_occurrence():
    profile = resolve_image_budget_profile(enabled=True, reserve_tokens=1024)
    refs = [_ref(index=0), _ref(index=1, sha256="b" * 64, path=asset_relative_path(
        TRANSFORM_VERSION_V2, "b" * 64, "image/png"
    ))]
    estimate = estimate_refs_budget(refs, profile=profile)
    assert estimate.image_count == 2
    assert estimate.image_tokens == 2048
    assert estimate.encoded_image_bytes > 0
    assert estimate.exact is False

    text_only = estimate_refs_budget(refs, profile=None)
    assert text_only.image_tokens == 0
    assert text_only.basis == "text_only"


def test_request_body_estimate_is_additive_not_clamped():
    assert estimate_request_body_bytes(
        text_serialized_bytes=100,
        encoded_image_bytes=200,
    ) == 300
    with pytest.raises(ValueError):
        estimate_request_body_bytes(
            text_serialized_bytes=-1,
            encoded_image_bytes=0,
        )
