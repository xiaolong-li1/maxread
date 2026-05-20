from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .models import FeishuEvent


DOCX_TOKEN_RE = re.compile(r"/docx/([A-Za-z0-9]+)")


PROGRESS_STAGES = (
    ("start", "[了解]", "已收到 / 已入队", "Get"),
    ("downloading", "[在做了]", "正在下载论文 source / 抓取网页", "OnIt"),
    ("reading", "[精神补给]", "正在读论文 / 文章", "StatusReading"),
    ("reviewing", "[思考]", "正在审阅和修订", "THINKING"),
    ("writing", "[敲键盘]", "正在写飞书文档", "Typing"),
)

PROGRESS_EMOJI_TYPES = {stage: emoji_type for stage, _label, _desc, emoji_type in PROGRESS_STAGES}
PROGRESS_EMOJI_TYPES.update({"queued": "Get", "claimed": "Get", "running": "OnIt"})


def progress_emoji_type(stage: str) -> str:
    return PROGRESS_EMOJI_TYPES.get(str(stage or "").strip().lower(), "")


def progress_help_lines() -> List[str]:
    return [f"- {label}：{description}" for _stage, label, description, _emoji_type in PROGRESS_STAGES]


class LarkCliError(RuntimeError):
    pass


@dataclass
class CommandResult:
    data: Dict[str, Any]
    stdout: str


class FeishuClient:
    def __init__(self, cli: str = "lark-cli", identity: str = "bot"):
        self.cli = cli
        self.identity = identity

    def doctor(self) -> Dict[str, Any]:
        return self._json([self.cli, "doctor"]).data

    def reply_text(self, message_id: str, text: str, idempotency_key: Optional[str] = None, reply_in_thread: bool = True) -> Dict[str, Any]:
        args = [self.cli, "im", "+messages-reply", "--as", self.identity, "--message-id", message_id, "--text", text]
        if reply_in_thread:
            args += ["--reply-in-thread"]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key]
        return self._json(args).data

    def add_reaction(self, message_id: str, emoji_type: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "im",
            "reactions",
            "create",
            "--as",
            self.identity,
            "--params",
            json.dumps({"message_id": message_id}, ensure_ascii=False),
            "--data",
            json.dumps({"reaction_type": {"emoji_type": emoji_type}}, ensure_ascii=False),
        ]).data

    def react_progress(self, message_id: str, stage: str) -> Dict[str, Any]:
        emoji_type = progress_emoji_type(stage)
        if not emoji_type:
            return {}
        return self.add_reaction(message_id, emoji_type)

    def create_docx(self, title: str) -> Dict[str, str]:
        created = self._json([
            self.cli,
            "docs",
            "+create",
            "--api-version",
            "v2",
            "--as",
            self.identity,
            "--content",
            f"<title>{_xml_escape(title)}</title>",
        ]).data
        doc = created.get("data", {}).get("document", {})
        url = doc.get("url", "")
        token = doc.get("document_id") or doc_token_from_url(url)
        if not url or not token:
            raise LarkCliError(f"Unable to parse created document: {created}")
        return {"url": url, "token": token}

    def overwrite_docx(self, doc_url: str, markdown: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "docs",
            "+update",
            "--api-version",
            "v2",
            "--as",
            self.identity,
            "--doc",
            doc_url,
            "--command",
            "overwrite",
            "--doc-format",
            "markdown",
            "--content",
            markdown,
        ]).data

    def overwrite_docx_xml(self, doc_url: str, xml: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "docs",
            "+update",
            "--api-version",
            "v2",
            "--as",
            self.identity,
            "--doc",
            doc_url,
            "--command",
            "overwrite",
            "--doc-format",
            "xml",
            "--content",
            xml,
        ]).data

    def insert_image(
        self,
        doc_url: str,
        image_path: str,
        caption: str = "",
        width: int = 720,
        selection: str = "",
    ) -> Dict[str, Any]:
        args = [
            self.cli,
            "docs",
            "+media-insert",
            "--as",
            self.identity,
            "--doc",
            doc_url,
            "--type",
            "image",
            "--file",
            _safe_relative_path(image_path),
            "--align",
            "center",
            "--width",
            str(width),
        ]
        if caption:
            args += ["--caption", caption]
        if selection:
            args += ["--selection-with-ellipsis", selection]
        return self._json(args).data

    def remove_text(self, doc_url: str, text: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "docs",
            "+update",
            "--api-version",
            "v2",
            "--as",
            self.identity,
            "--doc",
            doc_url,
            "--command",
            "str_replace",
            "--pattern",
            text,
            "--content",
            "",
        ]).data

    def publish_docx(self, token: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "drive",
            "permission.public",
            "patch",
            "--as",
            self.identity,
            "--params",
            json.dumps({"token": token, "type": "docx"}, ensure_ascii=False),
            "--data",
            json.dumps(
                {
                    "link_share_entity": "anyone_readable",
                    "external_access": True,
                    "security_entity": "anyone_can_view",
                    "comment_entity": "anyone_can_view",
                    "share_entity": "anyone",
                },
                ensure_ascii=False,
            ),
            "--yes",
        ]).data

    def fetch_docx(self, doc_url: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "docs",
            "+fetch",
            "--api-version",
            "v2",
            "--as",
            self.identity,
            "--doc",
            doc_url,
        ]).data

    def fetch_related_message_text(self, event: FeishuEvent) -> str:
        parts: List[str] = []
        thread_ids = _related_thread_ids(event.raw)

        if not thread_ids and event.message_id:
            try:
                current = self._json([
                    self.cli,
                    "im",
                    "+messages-mget",
                    "--as",
                    self.identity,
                    "--message-ids",
                    event.message_id,
                    "--format",
                    "json",
                ]).data
                thread_ids = _related_thread_ids(current)
            except Exception:
                thread_ids = []

        for thread_id in thread_ids[:3]:
            try:
                fetched = self._json([
                    self.cli,
                    "im",
                    "+threads-messages-list",
                    "--as",
                    self.identity,
                    "--thread",
                    thread_id,
                    "--sort",
                    "asc",
                    "--page-size",
                    "12",
                    "--format",
                    "json",
                ]).data
                parts.extend(_message_texts_from_payload(fetched))
            except Exception:
                continue

        message_ids = _related_message_ids(event.raw, exclude={event.message_id})
        if message_ids:
            try:
                fetched = self._json([
                    self.cli,
                    "im",
                    "+messages-mget",
                    "--as",
                    self.identity,
                    "--message-ids",
                    ",".join(message_ids[:5]),
                    "--format",
                    "json",
                ]).data
                parts.extend(_message_texts_from_payload(fetched))
            except Exception:
                pass

        return "\n".join(part for part in parts if part).strip()

    def event_stream(self) -> Iterator[FeishuEvent]:
        proc = subprocess.Popen(
            [self.cli, "event", "consume", "im.message.receive_v1", "--as", self.identity, "--quiet"],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            yield parse_event(payload)

    def _json(self, args: List[str]) -> CommandResult:
        attempts = _retry_attempts(args)
        last_error = ""
        for attempt in range(1, attempts + 1):
            result = subprocess.run(args, text=True, capture_output=True, check=False)
            if result.returncode == 0:
                try:
                    return CommandResult(data=json.loads(result.stdout), stdout=result.stdout)
                except json.JSONDecodeError as exc:
                    last_error = f"Expected JSON from {' '.join(args)}: {result.stdout}"
                    if attempt >= attempts:
                        raise LarkCliError(last_error) from exc
            else:
                last_error = result.stderr.strip() or result.stdout.strip() or f"command failed: {args}"
                if attempt >= attempts or not _is_retryable_error(last_error):
                    raise LarkCliError(last_error)
            time.sleep(min(2 ** attempt, 8))
        raise LarkCliError(last_error or f"command failed: {args}")



def _retry_attempts(args: List[str]) -> int:
    joined = " ".join(args)
    if "docs +media-insert" in joined:
        return int(os.environ.get("MAXREAD_FEISHU_MEDIA_RETRIES", "4"))
    if "docs +update" in joined or "docs +create" in joined or "permission.public patch" in joined:
        return int(os.environ.get("MAXREAD_FEISHU_WRITE_RETRIES", "3"))
    return int(os.environ.get("MAXREAD_FEISHU_DEFAULT_RETRIES", "2"))


def _is_retryable_error(error: str) -> bool:
    lowered = error.lower()
    retry_words = [
        "429",
        "rate limit",
        "too many",
        "timeout",
        "temporarily",
        "connection",
        "mcp_error",
        "1771001",
        "server internal error",
        "internal error",
        "VALIDATION:1101".lower(),
    ]
    return any(word in lowered for word in retry_words)

def parse_event(payload: Dict[str, Any]) -> FeishuEvent:
    return FeishuEvent(
        event_id=str(payload.get("event_id", "")),
        message_id=str(payload.get("message_id") or payload.get("id") or ""),
        chat_id=str(payload.get("chat_id", "")),
        chat_type=str(payload.get("chat_type", "")),
        message_type=str(payload.get("message_type", "")),
        sender_id=str(payload.get("sender_id", "")),
        content=str(payload.get("content", "")),
        raw=payload,
        mentioned_bot=_mentions_current_bot(payload),
    )


def _mentions_current_bot(payload: Dict[str, Any]) -> bool:
    mention_items = list(_iter_mention_items(payload))
    content_text = _message_text(payload.get("content", ""))
    if _text_mentions_bot(content_text):
        return True
    if not mention_items:
        return False

    configured_ids = _configured_bot_ids()
    if configured_ids:
        for item in mention_items:
            if _mention_contains_any(item, configured_ids):
                return True

    configured_names = _configured_bot_names()
    for item in mention_items:
        texts = [value.lower() for value in _iter_string_values(item)]
        if any(name in text for name in configured_names for text in texts):
            return True

    # Some event adapters expose only opaque mention keys like @_user_1. In that
    # case the event has already been delivered as an @ event, so treat it as a
    # valid bot mention rather than dropping legitimate group requests.
    return not any(_has_human_readable_mention_name(item) for item in mention_items)


def _iter_mention_items(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {"mentions", "mention"}:
                if isinstance(child, list):
                    yield from child
                elif isinstance(child, dict):
                    yield child
            yield from _iter_mention_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_mention_items(child)


def _configured_bot_ids() -> set[str]:
    raw = ",".join(
        value
        for value in [
            os.environ.get("MAXREAD_BOT_OPEN_ID", ""),
            os.environ.get("MAXREAD_BOT_UNION_ID", ""),
            os.environ.get("MAXREAD_BOT_APP_ID", ""),
        ]
        if value
    )
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _configured_bot_names() -> list[str]:
    raw = os.environ.get("MAXREAD_BOT_NAMES", "读不动了,MaxRead")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _message_text(content: Any) -> str:
    text = str(content or "")
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if isinstance(payload, dict):
        parts = []
        for key in ("text", "content"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
        return "\n".join(parts) if parts else text
    return text


def _text_mentions_bot(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(("@" + name) in lowered for name in _configured_bot_names())


def _mention_contains_any(value: Any, needles: set[str]) -> bool:
    return any(text.lower() in needles for text in _iter_string_values(value))


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_string_values(child)
    elif isinstance(value, str):
        yield value


def _has_human_readable_mention_name(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("name", "display_name", "user_name", "alias", "en_name", "zh_name"):
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            return True
    return False


def _related_message_ids(payload: Dict[str, Any], exclude: set[str] | None = None) -> List[str]:
    exclude = {item for item in (exclude or set()) if item}
    keys = {"parent_id", "root_id", "root_message_id", "parent_message_id", "thread_root_id"}
    ids: List[str] = []
    for key, value in _iter_key_values(payload):
        if str(key).lower() in keys:
            text = str(value or "")
            if text.startswith("om_") and text not in exclude and text not in ids:
                ids.append(text)
    return ids


def _related_thread_ids(payload: Dict[str, Any]) -> List[str]:
    keys = {"thread_id", "root_id", "parent_id"}
    ids: List[str] = []
    for key, value in _iter_key_values(payload):
        if str(key).lower() in keys:
            text = str(value or "")
            if (text.startswith("omt_") or text.startswith("om_")) and text not in ids:
                ids.append(text)
    return ids


def _iter_key_values(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _iter_key_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_key_values(child)


def _message_texts_from_payload(payload: Any) -> List[str]:
    texts: List[str] = []
    for key, value in _iter_key_values(payload):
        if str(key).lower() == "content" and isinstance(value, str):
            text = _message_text(value)
            if text and text not in texts:
                texts.append(text)
    return texts


def doc_token_from_url(url: str) -> str:
    match = DOCX_TOKEN_RE.search(url)
    return match.group(1) if match else ""


def _safe_relative_path(path: str) -> str:
    value = Path(path).expanduser()
    if not value.is_absolute():
        return str(value)
    value = value.resolve()
    cwd = Path.cwd().resolve()
    rel = os.path.relpath(value, cwd)
    if rel == os.curdir or rel.startswith(".."):
        return str(value)
    return rel


def _xml_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
