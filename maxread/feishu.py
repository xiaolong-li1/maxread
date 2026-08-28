from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .models import FeishuEvent


DOCX_TOKEN_RE = re.compile(r"/docx/([A-Za-z0-9]+)")
DOCX_URL_RE = re.compile(r"https?://[^\s<>()]+/docx/[A-Za-z0-9]+(?:\?[^\s<>()]*)?")


PROGRESS_STAGES = (
    ("start", "[了解]", "已收到 / 已入队", "Get"),
    ("downloading", "[在做了]", "正在下载论文 source / 抓取网页", "OnIt"),
    ("reading", "[精神补给]", "正在读论文 / 文章", "StatusReading"),
    ("reviewing", "[思考]", "正在审阅和修订", "THINKING"),
    ("writing", "[敲键盘]", "正在写飞书文档", "Typing"),
)

PROGRESS_EMOJI_TYPES = {stage: emoji_type for stage, _label, _desc, emoji_type in PROGRESS_STAGES}
PROGRESS_EMOJI_TYPES.update({"queued": "Get", "claimed": "Get", "running": "OnIt"})
PROGRESS_EMOJI_TYPE_SET = set(PROGRESS_EMOJI_TYPES.values())


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

    def send_text_to_chat(self, chat_id: str, text: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        args = [
            self.cli,
            "im",
            "+messages-send",
            "--as",
            self.identity,
            "--chat-id",
            chat_id,
            "--text",
            text,
        ]
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

    def list_reactions(self, message_id: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "im",
            "reactions",
            "list",
            "--as",
            self.identity,
            "--params",
            json.dumps({"message_id": message_id, "page_size": 50}, ensure_ascii=False),
        ]).data

    def delete_reaction(self, message_id: str, reaction_id: str) -> Dict[str, Any]:
        return self._json([
            self.cli,
            "im",
            "reactions",
            "delete",
            "--as",
            self.identity,
            "--params",
            json.dumps({"message_id": message_id, "reaction_id": reaction_id}, ensure_ascii=False),
            "--data",
            "{}",
        ]).data

    def react_progress(self, message_id: str, stage: str) -> Dict[str, Any]:
        emoji_type = progress_emoji_type(stage)
        if not emoji_type:
            return {}
        return self.add_reaction(message_id, emoji_type)

    def set_progress_reaction(self, message_id: str, stage: str) -> Dict[str, Any]:
        emoji_type = progress_emoji_type(stage)
        if not emoji_type:
            return {}
        try:
            payload = self.list_reactions(message_id)
            for item in _reaction_items(payload):
                item_emoji = item.get("reaction_type", {}).get("emoji_type", "")
                if item_emoji not in PROGRESS_EMOJI_TYPE_SET or item_emoji == emoji_type:
                    continue
                if not _reaction_from_current_identity(item, self.identity):
                    continue
                reaction_id = str(item.get("reaction_id") or "")
                if reaction_id:
                    try:
                        self.delete_reaction(message_id, reaction_id)
                    except Exception:
                        pass
        except Exception:
            pass
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
        url = normalize_doc_url(doc.get("url", ""))
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
        height: int = 0,
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
        if height:
            args += ["--height", str(height)]
        if caption:
            args += ["--caption", caption]
        return self._json(args).data

    def find_text_block_id(self, doc_url: str, text: str) -> str:
        payload = self._json([
            self.cli,
            "docs",
            "+fetch",
            "--as",
            self.identity,
            "--doc",
            doc_url,
            "--scope",
            "keyword",
            "--keyword",
            text,
            "--detail",
            "with-ids",
            "--format",
            "json",
        ]).data
        content = _document_content(payload)
        block_id = _find_exact_text_block_id(content, text)
        if block_id:
            return block_id

        # Newly created marker blocks are not always immediately available in
        # keyword scope, especially when the marker contains punctuation. A
        # full fetch is slower but authoritative and prevents false misses.
        payload = self._json([
            self.cli,
            "docs",
            "+fetch",
            "--as",
            self.identity,
            "--doc",
            doc_url,
            "--detail",
            "with-ids",
            "--format",
            "json",
        ]).data
        return _find_exact_text_block_id(_document_content(payload), text)

    def move_block_after(self, doc_url: str, anchor_block_id: str, source_block_id: str) -> Dict[str, Any]:
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
            "block_move_after",
            "--block-id",
            anchor_block_id,
            "--src-block-ids",
            source_block_id,
        ]).data

    def delete_block(self, doc_url: str, block_id: str) -> Dict[str, Any]:
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
            "block_delete",
            "--block-id",
            block_id,
        ]).data

    def block_replace(self, doc_url: str, block_id: str, content: str) -> Dict[str, Any]:
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
            "block_replace",
            "--block-id",
            block_id,
            "--doc-format",
            "xml",
            "--content",
            content,
        ]).data

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
                    "link_share_entity": "anyone_editable",
                    "external_access": True,
                    "security_entity": "anyone_can_edit",
                    "comment_entity": "anyone_can_edit",
                    "share_entity": "anyone",
                },
                ensure_ascii=False,
            ),
            "--yes",
        ]).data

    def fetch_docx(self, doc_url: str, doc_format: str = "xml", scope: str = "", detail: str = "simple") -> Dict[str, Any]:
        args = [
            self.cli,
            "docs",
            "+fetch",
            "--api-version",
            "v2",
            "--as",
            self.identity,
            "--doc",
            doc_url,
            "--doc-format",
            doc_format,
            "--detail",
            detail,
        ]
        if scope:
            args += ["--scope", scope]
        return self._json(args).data

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

    def fetch_related_message_ids(self, event: FeishuEvent) -> List[str]:
        """Resolve concrete message IDs for a topic without reading its text."""
        exclude = {str(event.message_id or "")}
        ids = _related_message_ids(event.raw, exclude=exclude)
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
                ids.extend(item for item in _related_message_ids(current, exclude=exclude) if item not in ids)
            except Exception:
                pass
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
                    "50",
                    "--format",
                    "json",
                ]).data
                ids.extend(item for item in _message_ids_from_payload(fetched, exclude=exclude) if item not in ids)
            except Exception:
                continue
        return ids

    def event_stream(self) -> Iterator[FeishuEvent]:
        while True:
            proc = subprocess.Popen(
                [self.cli, "event", "consume", "im.message.receive_v1", "--as", self.identity],
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    yield parse_event(payload)
                proc.wait(timeout=5)
            except GeneratorExit:
                _terminate_process(proc)
                raise
            except KeyboardInterrupt:
                _terminate_process(proc)
                raise
            finally:
                if proc.poll() is None:
                    _terminate_process(proc)
            time.sleep(2)

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


def _document_content(payload: Dict[str, Any]) -> str:
    current: Any = payload
    for key in ("data", "document"):
        if not isinstance(current, dict):
            return ""
        current = current.get(key, current)
    if not isinstance(current, dict):
        return ""
    return str(current.get("content") or current.get("markdown") or current.get("text") or "")


def _find_exact_text_block_id(content: str, text: str) -> str:
    if not content:
        return ""
    try:
        root = ET.fromstring(f"<root>{content}</root>")
    except ET.ParseError:
        return ""
    for block in root.iter():
        block_id = str(block.attrib.get("id") or "")
        block_text = "".join(block.itertext()).strip()
        if block_id and block_text == text:
            return block_id
    return ""



def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _retry_attempts(args: List[str]) -> int:
    joined = " ".join(args)
    if "docs +media-insert" in joined:
        return 1
    if "docs +update" in joined or "docs +create" in joined or "permission.public patch" in joined:
        return int(os.environ.get("MAXREAD_FEISHU_WRITE_RETRIES", "3"))
    return int(os.environ.get("MAXREAD_FEISHU_DEFAULT_RETRIES", "2"))


def _reaction_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    items = data.get("items", []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _reaction_from_current_identity(item: Dict[str, Any], identity: str) -> bool:
    operator = item.get("operator") or {}
    operator_type = str(operator.get("operator_type") or "").lower()
    if str(identity).lower() == "bot":
        return operator_type == "app"
    if str(identity).lower() == "user":
        return operator_type == "user"
    return False


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


def _message_ids_from_payload(payload: Any, exclude: set[str] | None = None) -> List[str]:
    excluded = {item for item in (exclude or set()) if item}
    ids: List[str] = []
    for key, value in _iter_key_values(payload):
        if str(key).lower() != "message_id":
            continue
        message_id = str(value or "")
        if message_id.startswith("om_") and message_id not in excluded and message_id not in ids:
            ids.append(message_id)
    return ids


def doc_token_from_url(url: str) -> str:
    match = DOCX_TOKEN_RE.search(url)
    return match.group(1) if match else ""


def normalize_doc_url(value: Any) -> str:
    """Extract a plain Feishu docx URL from CLI text or Markdown links."""
    text = str(value or "").strip()
    match = DOCX_URL_RE.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;!?")


def _safe_relative_path(path: str) -> str:
    raw = Path(path).expanduser()
    cwd = Path.cwd().resolve()
    source = raw.resolve() if raw.is_absolute() else (Path.cwd() / raw).resolve()
    if not raw.is_absolute():
        try:
            source.relative_to(cwd)
            return raw.as_posix()
        except ValueError:
            pass
    try:
        return source.relative_to(cwd).as_posix()
    except ValueError:
        pass
    try:
        cached = _copy_into_upload_cache(source, cwd)
        return cached.relative_to(cwd).as_posix()
    except OSError:
        return str(raw)


def _copy_into_upload_cache(source: Path, cwd: Path) -> Path:
    cache_dir = cwd / "var" / "feishu_uploads"
    cache_dir.mkdir(parents=True, exist_ok=True)
    _prune_upload_cache(cache_dir)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", source.stem).strip(".-") or "upload"
    digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
    suffix = source.suffix.lower() or ".bin"
    target = cache_dir / f"{stem}-{digest}{suffix}"
    should_copy = True
    if target.exists():
        try:
            should_copy = source.stat().st_mtime > target.stat().st_mtime or source.stat().st_size != target.stat().st_size
        except OSError:
            should_copy = True
    if should_copy:
        shutil.copy2(source, target)
    return target


def _prune_upload_cache(cache_dir: Path) -> None:
    try:
        ttl = int(os.environ.get("MAXREAD_FEISHU_UPLOAD_CACHE_TTL_SECONDS", "172800"))
    except ValueError:
        ttl = 172800
    if ttl <= 0:
        return
    cutoff = time.time() - ttl
    try:
        entries = list(cache_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def _xml_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
