"""测试 token 预算估算口径。"""

from __future__ import annotations

import copy
import threading
import time

from utils import token_budget as token_budget_module
from utils.token_budget import (
    PromptTokenParts,
    TokenEstimator,
    clear_raw_text_cache,
    has_image_inputs,
    raw_text_cache_info,
    raw_text_tokens,
)


class _CountingEncoder:
    """可观测 encode 调用的假 encoder；不访问网络。"""

    def __init__(
        self,
        name: str = "counting",
        *,
        divisor: int = 1,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self.divisor = max(1, divisor)
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def encode(self, text: str) -> list[int]:
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return [0] * max(1, len(text) // self.divisor)


def _patch_encoder(monkeypatch, encoder):
    monkeypatch.setattr(token_budget_module, "_encoding_for_model", lambda model="": encoder)


def _large_text(marker: str = "cache") -> str:
    return (f"{marker} 大文本内容 " * 2000).strip()


def _image_asset(**overrides):
    asset = {
        "sha256": "a" * 64,
        "mime": "image/jpeg",
        "width": 640,
        "height": 480,
        "byte_size": 12345,
        "path": "incoming/images/pillow-first-frame-v1/" + ("a" * 64) + ".jpg",
        "detail": "auto",
    }
    asset.update(overrides)
    return asset


def _history_image_message(**asset_overrides):
    return {
        "role": "user",
        "content": "用户发来一张照片",
        "metadata": {"image_assets": [_image_asset(**asset_overrides)]},
    }


def _provider_image_message(data_url: str, *, metadata: bool = True, **asset_overrides):
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "用户发来一张照片"},
            {
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "auto"},
            },
        ],
    }
    if metadata:
        message["metadata"] = {"image_assets": [_image_asset(**asset_overrides)]}
    return message


def test_estimate_messages_ignores_provider_stripped_metadata():
    estimator = TokenEstimator()
    base = [{"role": "user", "content": "hello"}]
    noisy = [
        {
            "role": "user",
            "content": "hello",
            "metadata": {"raw": "x" * 20000},
        }
    ]

    assert estimator.estimate_messages(noisy) == estimator.estimate_messages(base)


def test_estimate_messages_ignores_conversation_id():
    estimator = TokenEstimator()
    base = [{"role": "user", "content": "hello"}]
    noisy = [
        {
            "role": "user",
            "content": "hello",
            "conversation_id": "conv-" + ("x" * 20000),
        }
    ]

    assert estimator.estimate_messages(noisy) == estimator.estimate_messages(base)


def test_estimate_messages_does_not_count_reasoning_content_by_default():
    estimator = TokenEstimator()
    base = [{"role": "assistant", "content": "done"}]
    noisy = [
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "hidden " * 4000,
        }
    ]

    assert estimator.estimate_messages(noisy) == estimator.estimate_messages(base)


def test_estimate_messages_still_counts_tool_calls():
    estimator = TokenEstimator()
    without_tool = [{"role": "assistant", "content": ""}]
    with_tool = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"query":"' + ("alpha " * 400) + '"}',
                    },
                }
            ],
        }
    ]

    assert estimator.estimate_messages(with_tool) > estimator.estimate_messages(without_tool) + 100


def test_update_calibration_converges_downward_on_consistent_overestimate():
    """tiktoken 对中文高估：持续反馈应让系数收敛到 1 以下，而不是被推高。"""
    estimator = TokenEstimator()
    assert estimator.calib_ratio == 1.0
    for _ in range(20):
        estimator.update_calibration(estimated=10000, actual_prompt_tokens=6000)
    assert estimator.calib_ratio < 0.7
    # 估算结果随之下降
    assert estimator.estimate_text("中文内容" * 100) < TokenEstimator().estimate_text("中文内容" * 100)


def test_update_calibration_clamps_wild_ratio():
    """单次异常样本（例如误传了工具循环累加值）不能把系数带飞。"""
    estimator = TokenEstimator()
    estimator.update_calibration(estimated=10000, actual_prompt_tokens=1_000_000)
    assert estimator.calib_ratio <= 2.0

    estimator = TokenEstimator()
    for _ in range(50):
        estimator.update_calibration(estimated=10000, actual_prompt_tokens=1)
    assert estimator.calib_ratio >= 0.35


def test_update_calibration_ignores_invalid_samples():
    estimator = TokenEstimator()
    estimator.update_calibration(estimated=0, actual_prompt_tokens=100)
    estimator.update_calibration(estimated=100, actual_prompt_tokens=0)
    assert estimator.calib_ratio == 1.0


def test_agent_run_result_defaults_first_prompt_tokens_to_zero():
    from agents.base import AgentRunResult

    result = AgentRunResult()
    assert result.first_prompt_tokens == 0
    assert result.prompt_tokens == 0


def _tools_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "demo",
                "description": "demo tool",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            },
        }
    ]


def test_estimate_prompt_uses_canonical_tools_json():
    estimator = TokenEstimator(model="deepseek-chat")
    messages = [{"role": "user", "content": "hello"}]
    without_tools = estimator.estimate_prompt(messages, None)
    with_tools = estimator.estimate_prompt(messages, _tools_schema())
    assert with_tools > without_tools
    assert estimator.estimate_prompt(messages, _tools_schema()) == with_tools
    assert estimator.estimate_tools_schema(_tools_schema()) > 0


def test_raw_text_cache_is_shared_across_estimators():
    from utils.token_budget import raw_text_tokens

    text = "中文 token 估算 " * 50
    first = raw_text_tokens(text, "deepseek-chat")
    second = raw_text_tokens(text, "deepseek-chat")
    assert first == second
    assert TokenEstimator(model="deepseek-chat").estimate_text(text) >= 1


def test_token_calibration_persists_and_clamps(tmp_path):
    from core.token_calibration import TokenCalibrationStore

    path = tmp_path / "token_calibration.json"
    store = TokenCalibrationStore(path)
    state = store.update_sample(
        "si",
        "deepseek-flash",
        estimated_prompt_tokens=10_000,
        actual_prompt_tokens=8_000,
    )
    assert abs(state.ratio - 0.8) < 1e-9
    assert state.samples == 1

    reloaded = TokenCalibrationStore(path)
    restored = reloaded.get("si", "deepseek-flash")
    assert abs(restored.ratio - 0.8) < 1e-9
    assert restored.samples == 1
    assert restored.mape > 0

    for _ in range(20):
        state = reloaded.update_sample(
            "si",
            "deepseek-flash",
            estimated_prompt_tokens=10_000,
            actual_prompt_tokens=1_000_000,
        )
    assert state.ratio <= 2.0
    assert state.samples == 21


# ============================================================
# 阶段 2：共享 raw-token 缓存
# ============================================================


def test_raw_text_cache_is_shared_across_estimators_without_reencode(monkeypatch):
    encoder = _CountingEncoder("shared-encoder")
    _patch_encoder(monkeypatch, encoder)
    clear_raw_text_cache()
    text = _large_text("shared")

    first = TokenEstimator(model="cache-model").estimate_text(text)
    second = TokenEstimator(model="cache-model").estimate_text(text)

    assert first == second
    assert encoder.calls == 1
    info = raw_text_cache_info()
    assert info == {"hits": 1, "misses": 1, "maxsize": 2048, "currsize": 1}


def test_raw_tools_cache_hits_new_schema_object_and_misses_same_length_change(monkeypatch):
    encoder = _CountingEncoder("tools-encoder")
    _patch_encoder(monkeypatch, encoder)
    clear_raw_text_cache()
    estimator = TokenEstimator(model="tools-model")
    schema = [
        {
            "type": "function",
            "function": {
                "name": "demo",
                "description": "one",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    first = estimator.raw_tools_tokens(schema)
    assert encoder.calls == 1

    same_content = copy.deepcopy(schema)
    assert estimator.raw_tools_tokens(same_content) == first
    assert encoder.calls == 1, "相同内容的新 list 应命中共享缓存"

    same_length_changed = copy.deepcopy(schema)
    same_length_changed[0]["function"]["description"] = "two"
    estimator.raw_tools_tokens(same_length_changed)
    assert encoder.calls == 2, "同长度但内容变化必须重新 encode"


def test_raw_cache_separates_model_and_backend(monkeypatch):
    encoder_a = _CountingEncoder("backend-a", divisor=1)
    encoder_b = _CountingEncoder("backend-b", divisor=2)
    current = {"encoder": encoder_a}
    monkeypatch.setattr(
        token_budget_module,
        "_encoding_for_model",
        lambda model="": current["encoder"],
    )
    clear_raw_text_cache()
    text = "x" * 200

    from_a = raw_text_tokens(text, "model-a")
    from_a_again = raw_text_tokens(text, "model-a")
    assert from_a == from_a_again == 200
    assert encoder_a.calls == 1

    current["encoder"] = encoder_b
    from_b_backend = raw_text_tokens(text, "model-a")
    assert from_b_backend == 100
    assert encoder_b.calls == 1

    from_b_model = raw_text_tokens(text, "model-b")
    assert from_b_model == 100
    assert encoder_b.calls == 2, "不同 model 不能共享同一缓存值"


def test_calibration_change_keeps_raw_cache_but_changes_estimate(monkeypatch):
    encoder = _CountingEncoder("calibration-encoder")
    _patch_encoder(monkeypatch, encoder)
    clear_raw_text_cache()
    text = "y" * 1000
    estimator = TokenEstimator(model="cal-model")

    first = estimator.estimate_text(text)
    assert encoder.calls == 1

    estimator.set_calibration(0.5)
    second = estimator.estimate_text(text)
    assert second < first
    assert encoder.calls == 1, "校准变化不能清空 raw 缓存"

    estimator.set_calibration(1.5)
    third = estimator.estimate_text(text)
    assert third > first
    assert encoder.calls == 1


def test_raw_cache_evicts_lru_and_never_stores_source_text(monkeypatch):
    encoder = _CountingEncoder("eviction-encoder")
    _patch_encoder(monkeypatch, encoder)
    monkeypatch.setattr(token_budget_module, "_RAW_TEXT_CACHE_MAXSIZE", 2)
    clear_raw_text_cache()
    texts = [
        "MARKER-ALPHA " * 100,
        "MARKER-BETA " * 100,
        "MARKER-GAMMA " * 100,
    ]

    for text in texts:
        raw_text_tokens(text, "evict-model")

    info = raw_text_cache_info()
    assert info["maxsize"] == 2
    assert info["currsize"] == 2
    assert info["misses"] == 3
    assert encoder.calls == 3

    keys_repr = repr(list(token_budget_module._RAW_TEXT_CACHE.keys()))
    for text in texts:
        assert text not in keys_repr, "缓存键不能保留完整原文"
    assert all(
        isinstance(value, int)
        for value in token_budget_module._RAW_TEXT_CACHE.values()
    )


def test_concurrent_same_key_encodes_only_once(monkeypatch):
    encoder = _CountingEncoder("concurrent-encoder", delay=0.05)
    _patch_encoder(monkeypatch, encoder)
    clear_raw_text_cache()
    text = "并发相同键 " * 200
    results: list[int] = []

    def worker() -> None:
        results.append(raw_text_tokens(text, "concurrent-model"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert len(set(results)) == 1
    assert encoder.calls == 1, "并发同键只能冷算一次"
    assert raw_text_cache_info()["hits"] == 7


class _FlakyEncoder:
    name = "flaky-encoder"

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, text: str) -> list[int]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient tokenizer failure")
        return [0] * 7


def test_tokenizer_failure_fallback_is_not_cached(monkeypatch):
    encoder = _FlakyEncoder()
    _patch_encoder(monkeypatch, encoder)
    clear_raw_text_cache()
    text = "z" * 100

    fallback = raw_text_tokens(text, "flaky-model")
    recovered = raw_text_tokens(text, "flaky-model")

    assert fallback == 50  # len // 2 粗估
    assert recovered == 7
    assert encoder.calls == 2
    info = raw_text_cache_info()
    assert info["currsize"] == 1
    assert info["misses"] == 2


# ============================================================
# 阶段 2：图片预算
# ============================================================


def test_history_image_refs_count_only_when_native_images_enabled():
    message = _history_image_message()
    text_only = [{"role": "user", "content": "用户发来一张照片"}]
    disabled = TokenEstimator(model="vision-model", include_image_assets=False)
    enabled = TokenEstimator(model="vision-model", include_image_assets=True)

    assert disabled.estimate_messages([message]) == disabled.estimate_messages(text_only)
    assert enabled.estimate_messages([message]) > disabled.estimate_messages([message])

    parts = enabled.estimate_prompt_parts([message])
    # 没有已核实供应商公式时使用显式 reserve_v1：默认 8192/图。
    assert parts.image_tokens == 8192


def test_invalid_history_image_ref_is_ignored():
    estimator = TokenEstimator(model="vision-model", include_image_assets=True)
    for overrides in (
        {"sha256": "not-a-sha"},
        {"mime": "text/plain"},
        {"path": ""},
        {"byte_size": 0},
        {"byte_size": True},
    ):
        message = _history_image_message(**overrides)
        assert estimator.image_tokens([message]) == 0


def test_provider_image_block_always_counted_and_metadata_not_double_counted():
    data_url = "data:image/png;base64," + ("A" * 100)
    estimator = TokenEstimator(model="vision-model", include_image_assets=False)

    with_metadata = _provider_image_message(data_url, metadata=True)
    without_metadata = _provider_image_message(data_url, metadata=False)

    with_parts = estimator.estimate_prompt_parts([with_metadata])
    without_parts = estimator.estimate_prompt_parts([without_metadata])

    assert with_parts.image_tokens == 8192, "实际图片块始终按显式 reserve 计图"
    assert without_parts.image_tokens == 8192, "无metadata也使用同一显式 reserve"
    assert with_parts.image_tokens != 8192 + 8192, "图片块与 metadata 不能重复计费"


def test_image_base64_growth_does_not_inflate_text_tokens():
    small = "data:image/png;base64," + ("A" * 100)
    large = "data:image/png;base64," + ("A" * 100_000)
    estimator = TokenEstimator(model="vision-model", include_image_assets=False)

    small_message = _provider_image_message(small)
    large_message = _provider_image_message(large)

    assert estimator.raw_message_tokens([small_message]) == estimator.raw_message_tokens(
        [large_message]
    )
    assert estimator.estimate_messages([small_message]) == estimator.estimate_messages(
        [large_message]
    )


def test_image_reserve_does_not_depend_on_declared_dimensions():
    estimator = TokenEstimator(model="vision-model", include_image_assets=True)
    for overrides in (
        {"width": 0},
        {"height": None},
        {"width": "640"},
        {"height": True},
    ):
        message = _history_image_message(**overrides)
        assert estimator.image_tokens([message]) == 8192


def test_image_tokens_are_not_multiplied_by_text_calibration():
    estimator = TokenEstimator(
        model="vision-model",
        calib_ratio=2.0,
        include_image_assets=True,
    )
    message = _history_image_message()
    parts = estimator.estimate_prompt_parts([message])
    text_raw = (
        parts.message_tokens_raw
        + parts.tool_tokens_raw
        + parts.overhead_tokens
    )

    assert parts.image_tokens == 8192
    assert estimator.estimate_prompt([message]) == max(
        1,
        round(text_raw * 2.0) + parts.image_tokens,
    )


def test_prompt_token_parts_keeps_three_field_compatibility():
    parts = PromptTokenParts(1, 2, 3)
    assert parts.image_tokens == 0
    assert parts.raw_total == 6


def test_has_image_inputs_detects_blocks_and_trusted_refs_only():
    assert not has_image_inputs([{"role": "user", "content": "纯文字"}])
    assert has_image_inputs([_history_image_message()])
    assert has_image_inputs(
        [_provider_image_message("data:image/png;base64,AAAA")]
    )
    assert not has_image_inputs([_history_image_message(sha256="bad")])
