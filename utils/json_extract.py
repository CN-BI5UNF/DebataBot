"""Tolerant JSON extraction for model output.

Models often wrap JSON in prose or Markdown fences. These helpers parse only the
first balanced array/object and never execute or evaluate the content.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MAX_INPUT = 400_000


def extract_json_array(text: str, *, max_input: int = _MAX_INPUT) -> list[Any]:
    """Parse a JSON array, tolerating prose/code fences around it."""
    raw = (text or "").strip()
    if not raw or len(raw) > max_input:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return value
    except ValueError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
        try:
            value = json.loads(candidate)
            if isinstance(value, list):
                return value
        except ValueError:
            pass
    starts = [match.start() for match in re.finditer(r"\[", raw)][:64]
    for start in starts:
        parsed = _balanced_json(raw, start, "[", "]")
        if isinstance(parsed, list):
            return parsed
    return []


def extract_json_object(
    text: str,
    *,
    max_input: int = _MAX_INPUT,
) -> dict[str, Any] | None:
    """Parse a JSON object, tolerating prose/code fences around it."""
    raw = (text or "").strip()
    if not raw or len(raw) > max_input:
        return None
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    starts = [match.start() for match in re.finditer(r"\{", raw)][:64]
    for start in starts:
        parsed = _balanced_json(raw, start, "{", "}")
        if isinstance(parsed, dict):
            return parsed
    return None


def _balanced_json(
    raw: str,
    start: int,
    opener: str,
    closer: str,
) -> Any | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : index + 1])
                except ValueError:
                    return None
    return None


__all__ = ["extract_json_array", "extract_json_object"]
