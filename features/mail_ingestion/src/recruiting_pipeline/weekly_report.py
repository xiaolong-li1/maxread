from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_URL = "https://ccnsbbr30xgq.feishu.cn/base/S4v4bdOCuaWvAQs90vCcek4anHh?table=tblJtH3AVWn0Gar8&view=vewhN2XnwI"
RECENT_URL = "https://ccnsbbr30xgq.feishu.cn/base/S4v4bdOCuaWvAQs90vCcek4anHh?table=tblJtH3AVWn0Gar8&view=vewVVbQsCs"
TOPIC_URLS = {
    "MLSys": "https://ccnsbbr30xgq.feishu.cn/base/S4v4bdOCuaWvAQs90vCcek4anHh?table=tblJtH3AVWn0Gar8&view=vewaFIevDP",
    "Agentic Infrastructure": "https://ccnsbbr30xgq.feishu.cn/base/S4v4bdOCuaWvAQs90vCcek4anHh?table=tblJtH3AVWn0Gar8&view=vewclBCsP4",
    "Kernel Efficiency": "https://ccnsbbr30xgq.feishu.cn/base/S4v4bdOCuaWvAQs90vCcek4anHh?table=tblJtH3AVWn0Gar8&view=vewJL5BjVw",
    "World Model": "https://ccnsbbr30xgq.feishu.cn/base/S4v4bdOCuaWvAQs90vCcek4anHh?table=tblJtH3AVWn0Gar8&view=vewVuhfX3m",
}
PORTAL_URL = "https://xiaolong-dev.me/maxread/mail"


def render_weekly_report(db_path: Path, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    start = now - timedelta(days=7)
    conn = sqlite3.connect(db_path)
    rows = []
    for fields_json, latest_time, status, doc_url, base_record_id, row_status in conn.execute(
        "select fields_json,latest_time,screening_status,doc_url,base_record_id,status from recruiting_threads"
    ):
        fields = json.loads(fields_json)
        if fields.get("mail_type") == "other" or not base_record_id or row_status == "inactive":
            continue
        parsed = _parse_time(latest_time)
        if parsed and parsed >= start:
            rows.append((parsed, fields, status or "未筛选", doc_url or ""))
    rows.sort(key=lambda item: item[0], reverse=True)
    all_rows = []
    for fields_json, latest_time, status, doc_url, base_record_id, row_status in conn.execute(
        "select fields_json,latest_time,screening_status,doc_url,base_record_id,status from recruiting_threads"
    ):
        fields = json.loads(fields_json)
        if fields.get("mail_type") != "other" and base_record_id and row_status != "inactive":
            all_rows.append((fields, status or "未筛选"))
    counts = {status: sum(1 for _, value in all_rows if value == status) for status in ("未筛选", "面试资格", "面试通过", "实习生", "未通过")}
    period = f"{start.astimezone().strftime('%Y-%m-%d %H:%M')} – {now.astimezone().strftime('%Y-%m-%d %H:%M')}"
    title = f"ZIP Lab 招聘周报｜{now.astimezone().strftime('%Y-%m-%d')}"
    lines = [
        f"## {title}",
        "",
        f"统计窗口：{period}",
        "",
        f"候选池 **{len(all_rows)} 人**　·　未筛选 **{counts['未筛选']} 人**　·　最近一周新增 **{len(rows)} 人**",
        "",
        "",
        f"**[打开招聘邮件页面]({PORTAL_URL})**　查看候选池、最近一周、其他邮件和各方向表格。",
    ]
    period_key = start.astimezone().strftime("%Y%m%d")
    return "\n".join(lines), period_key


def markdown_to_post(markdown: str) -> dict[str, dict[str, object]]:
    """Convert the compact report to native Feishu post nodes.

    The Feishu post API does not parse Markdown.  Keep links as native ``a``
    nodes, strip Markdown-only heading/blockquote/list markers, and use bold
    text for section headings so the message remains readable in chat.
    """
    lines = markdown.splitlines()
    title = next((line.lstrip("# ").strip() for line in lines if line.startswith("## ")), "ZIP Lab 招聘周报")
    paragraphs: list[list[dict[str, object]]] = []
    link_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("## "):
            continue
        is_heading = line.startswith("### ")
        if is_heading:
            line = line[4:].strip()
        elif line.startswith("> "):
            # Blockquotes are Markdown syntax only; present as a quiet note.
            line = f"说明：{line[2:].strip()}"
        elif line.startswith("- "):
            line = f"• {line[2:].strip()}"
        elements: list[dict[str, object]] = []
        cursor = 0
        for match in link_pattern.finditer(line):
            text = line[cursor:match.start()].replace("**", "")
            if text:
                elements.append({"tag": "text", "text": text})
            elements.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
            cursor = match.end()
        tail = line[cursor:].replace("**", "")
        if tail:
            elements.append({"tag": "text", "text": tail})
        if elements:
            if is_heading:
                for element in elements:
                    if element["tag"] == "text":
                        element["style"] = ["bold"]
            paragraphs.append(elements)
    return {"zh_cn": {"title": title, "content": paragraphs}}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return None
