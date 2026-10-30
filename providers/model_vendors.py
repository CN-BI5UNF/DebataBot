"""上游模型厂商识别（中转站/自定义 OpenAI 兼容端点）。

一个中转站可以同时挂 Gemini、Claude、GPT、DeepSeek 等模型。provider 本身没有
``preset`` 时，能力表不知道该怎么判断 vision/video/context_length。这里提供：

- 固定的厂商选项（给添加提供商/模型时手动选择）；
- 根据模型 ID 前缀的确定性自动识别（如 ``gemini-*``、``claude-*``）。

识别结果只用于查能力表，不会改变协议或地址；中转站仍按用户填的协议请求。
"""

from __future__ import annotations

import re

VENDOR_CHOICES: tuple[tuple[str, str], ...] = (
    ("gemini", "Google Gemini"),
    ("anthropic", "Anthropic Claude"),
    ("openai", "OpenAI"),
    ("deepseek", "DeepSeek"),
    ("qwen", "通义千问 Qwen"),
    ("glm", "智谱 GLM"),
    ("moonshot", "月之暗面 Kimi"),
    ("xai", "xAI Grok"),
    ("volcengine", "火山引擎豆包"),
    ("mimo", "小米 MiMo"),
)

VENDOR_DISPLAY_NAMES: dict[str, str] = dict(VENDOR_CHOICES)

_ALIASES = {
    "google": "gemini",
    "google-gemini": "gemini",
    "claude": "anthropic",
    "anthropic-claude": "anthropic",
    "kimi": "moonshot",
    "moonshotai": "moonshot",
    "qwen3": "qwen",
    "qwq": "qwen",
    "chatglm": "glm",
    "zhipu": "glm",
    "doubao": "volcengine",
    "bytedance": "volcengine",
}

_PREFIX_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gemini", "gemma"), "gemini"),
    (("claude",), "anthropic"),
    (("gpt", "chatgpt", "o1", "o3", "o4"), "openai"),
    (("deepseek",), "deepseek"),
    (("qwen", "qwq"), "qwen"),
    (("glm", "chatglm"), "glm"),
    (("kimi", "moonshot"), "moonshot"),
    (("grok",), "xai"),
    (("doubao", "skylark"), "volcengine"),
    (("mimo",), "mimo"),
)

_VENDOR_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def normalize_vendor(value: str | None) -> str:
    """Normalize an explicit vendor value; empty/unknown returns ``""``."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = _ALIASES.get(text, text)
    return text if _VENDOR_RE.fullmatch(text) else ""


def vendor_choices() -> list[dict[str, str]]:
    return [
        {"id": vendor_id, "label": label}
        for vendor_id, label in VENDOR_CHOICES
    ]


def detect_model_vendor(model_id: str | None) -> str:
    """Return a vendor family from a model ID; empty when unknown."""
    text = str(model_id or "").strip().lower()
    if not text:
        return ""
    # OpenRouter/SiliconFlow use vendor/model; Google SDK may use models/model.
    text = text.rsplit("/", 1)[-1]
    text = text.split(":", 1)[0]
    for prefixes, vendor in _PREFIX_RULES:
        if any(text.startswith(prefix) for prefix in prefixes):
            return vendor
    return ""


def resolve_model_vendor(
    model_id: str | None,
    *,
    explicit: str | None = None,
    preset: str | None = None,
) -> str:
    """Resolution order: explicit vendor > provider preset > model-ID detection."""
    return (
        normalize_vendor(explicit)
        or normalize_vendor(preset)
        or detect_model_vendor(model_id)
    )


__all__ = [
    "VENDOR_CHOICES",
    "VENDOR_DISPLAY_NAMES",
    "detect_model_vendor",
    "normalize_vendor",
    "resolve_model_vendor",
    "vendor_choices",
]
