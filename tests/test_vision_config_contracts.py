"""视觉配置限额/profile 与两个开关独立性测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app_config.schema import (
    ImageLimitsConfig,
    VideoLimitsConfig,
    VisionFeatureConfig,
)
from utils.image_contracts import (
    DEFAULT_IMAGE_TOKEN_RESERVE,
    MAX_IMAGE_BYTES,
    ImageLimits,
)
from utils.video_contracts import (
    DEFAULT_VIDEO_TOKEN_RESERVE,
    MAX_VIDEO_BYTES,
    VideoLimits,
)


def test_vision_config_defaults_are_safe_and_independent():
    cfg = VisionFeatureConfig()
    assert cfg.main_model_images == "auto"
    assert cfg.main_model_videos == "auto"
    assert cfg.gif_mode == "native"
    assert cfg.enabled is False
    assert cfg.image_budget_profile == "auto"
    assert cfg.image_token_reserve == DEFAULT_IMAGE_TOKEN_RESERVE
    assert cfg.image_limits.max_image_bytes == MAX_IMAGE_BYTES
    assert cfg.video_token_reserve == DEFAULT_VIDEO_TOKEN_RESERVE
    assert cfg.video_limits.max_video_bytes == MAX_VIDEO_BYTES


def test_image_limits_can_only_tighten_and_reject_bool():
    lowered = ImageLimitsConfig(max_image_side=1024, cache_max_bytes=0)
    assert lowered.max_image_side == 1024
    assert lowered.to_image_limits() == ImageLimits(
        max_image_side=1024,
        cache_max_bytes=0,
    )

    with pytest.raises(ValidationError):
        ImageLimitsConfig(max_image_bytes=True)
    with pytest.raises(ValidationError):
        ImageLimitsConfig(max_image_bytes=MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValidationError):
        ImageLimitsConfig(max_images_per_request=21)
    with pytest.raises(ValidationError):
        ImageLimitsConfig(cache_max_bytes=-1)


def test_video_limits_can_only_tighten_and_reject_bool():
    lowered = VideoLimitsConfig(
        max_video_bytes=1024,
        max_videos_per_request=1,
    )
    assert lowered.to_video_limits() == VideoLimits(
        max_video_bytes=1024,
        max_videos_per_request=1,
    )

    with pytest.raises(ValidationError):
        VideoLimitsConfig(max_video_bytes=True)
    with pytest.raises(ValidationError):
        VideoLimitsConfig(max_video_bytes=MAX_VIDEO_BYTES + 1)
    with pytest.raises(ValidationError):
        VideoLimitsConfig(max_videos_per_request=3)
    with pytest.raises(ValidationError):
        VideoLimitsConfig(max_request_body_bytes=0)


def test_video_token_reserve_bounds():
    with pytest.raises(ValidationError):
        VisionFeatureConfig(video_token_reserve=100)
    with pytest.raises(ValidationError):
        VisionFeatureConfig(video_token_reserve=600000)
    with pytest.raises(ValidationError):
        VisionFeatureConfig(video_token_reserve=True)
    assert (
        VisionFeatureConfig(video_token_reserve=2048).video_token_reserve == 2048
    )


def test_image_token_reserve_bounds():
    with pytest.raises(ValidationError):
        VisionFeatureConfig(image_token_reserve=100)
    with pytest.raises(ValidationError):
        VisionFeatureConfig(image_token_reserve=70000)
    with pytest.raises(ValidationError):
        VisionFeatureConfig(image_token_reserve=True)
    assert VisionFeatureConfig(image_token_reserve=2048).image_token_reserve == 2048


def test_native_and_independent_switches_do_not_imply_each_other():
    native_off_independent_on = VisionFeatureConfig(
        main_model_images="off",
        enabled=True,
        provider="vision-provider",
        model="vision-model",
    )
    assert native_off_independent_on.main_model_images == "off"
    assert native_off_independent_on.enabled is True

    native_on_independent_off = VisionFeatureConfig(
        main_model_images="on",
        enabled=False,
    )
    assert native_on_independent_off.main_model_images == "on"
    assert native_on_independent_off.enabled is False
