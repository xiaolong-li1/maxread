from __future__ import annotations

import re

from .help import plain_message_text


FEEDBACK_RE = re.compile(r"^\s*(?:反馈|建议|问题|bug|吐槽|许愿|需求|feature|issue)\s*[:：,， ]", re.I)


def is_feedback_text(content: str) -> bool:
    text = plain_message_text(content).strip()
    return bool(FEEDBACK_RE.search(text))


def visible_feedback_rows(rows):
    return [row for row in rows if is_feedback_text(str(row.get("content", "")))]


def count_feedback_by_status(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in visible_feedback_rows(rows):
        status = str(row.get("status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts
