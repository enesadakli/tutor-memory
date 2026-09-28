from __future__ import annotations

import re

_LEADING_MARKER_RE = re.compile(r"^(?:(?:[#>*-]+|\d+\.)\s*)+")


def sanitize_brief_text(text: str, max_len: int) -> str:
    """Reduce model-produced brief text to one bounded plain-text line."""
    value = " ".join(text.split())
    value = _LEADING_MARKER_RE.sub("", value)
    value = value.replace("`", "").replace("<", "").replace(">", "")
    value = " ".join(value.split())
    return value[:max_len].rstrip()
