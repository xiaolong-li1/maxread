from __future__ import annotations

from typing import Any


def sanitize_unicode_text(value: str) -> tuple[str, int]:
    """Return UTF-8 encodable text, preserving valid surrogate pairs."""
    text = str(value or "")
    output: list[str] = []
    replacements = 0
    index = 0
    while index < len(text):
        code = ord(text[index])
        if 0xD800 <= code <= 0xDBFF:
            if index + 1 < len(text):
                low = ord(text[index + 1])
                if 0xDC00 <= low <= 0xDFFF:
                    scalar = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    output.append(chr(scalar))
                    index += 2
                    continue
            output.append("\uFFFD")
            replacements += 1
        elif 0xDC00 <= code <= 0xDFFF:
            output.append("\uFFFD")
            replacements += 1
        else:
            output.append(text[index])
        index += 1
    return "".join(output), replacements


def sanitize_unicode_value(value: Any) -> tuple[Any, int]:
    """Recursively sanitize strings in a JSON-like value."""
    if isinstance(value, str):
        return sanitize_unicode_text(value)
    if isinstance(value, dict):
        output = {}
        total = 0
        for key, item in value.items():
            safe_key, key_count = sanitize_unicode_text(key) if isinstance(key, str) else (key, 0)
            safe_item, item_count = sanitize_unicode_value(item)
            output[safe_key] = safe_item
            total += key_count + item_count
        return output, total
    if isinstance(value, (list, tuple)):
        output = []
        total = 0
        for item in value:
            safe_item, count = sanitize_unicode_value(item)
            output.append(safe_item)
            total += count
        return (tuple(output) if isinstance(value, tuple) else output), total
    return value, 0
