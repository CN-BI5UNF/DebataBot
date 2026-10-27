"""Compatibility alias for :mod:`utils.emoji_store`.

The pure filesystem implementation moved to ``utils/`` so tools (the LLM-facing
emoji tools) can use it without importing the WebUI layer. Aliasing the module
object (instead of re-exporting names) keeps existing monkeypatches, constants
and identity checks pointed at the real implementation.
"""

from __future__ import annotations

import sys

from utils import emoji_store as _impl

sys.modules[__name__] = _impl
