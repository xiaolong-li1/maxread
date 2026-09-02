from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from collections import defaultdict
from typing import Any

from .config import PipelineSettings
from .models import CandidateFields
from .retry import is_transient_error, retry_call


MAIL_TYPES = {
    "candidate": "候选人来信",
    "internship_application": "候选人来信",
    "general_inquiry": "候选人来信",
    "other": "其他",
}

BASE_RECORD_FIELDS = (
    "姓名",
    "最新邮件时间",
    "院校 / 就读信息",
    "院校",
    "专业信息",
    "邮件类型",
    "申请项目",
    "学业表现",
    "排名",
    "排名依据",
    "是否985",
    "是否C9",
    "是否已回复",
    "来源邮箱",
    "申请目的 / 科研摘要",
    "材料文档",
    "筛选状态",
    "是否已分配面试",
    "面试结果",
)
BASE_PAGE_SIZE = 200


@dataclass
class BaseSyncResult:
    record_id: str
    created: bool


class BaseSync:
    def __init__(self, settings: PipelineSettings):
        self.settings = settings
        self._existing_index: tuple[dict[tuple[str, str], list[str]], dict[str, str]] | None = None
        self._record_states: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        *,
        record_id: str | None,
        fields: CandidateFields,
        latest_time: str | None,
        document_url: str | None,
        status: str = "未筛选",
        interview_assigned: bool = False,
        interview_result: str = "未开始",
        has_replied: bool = False,
    ) -> BaseSyncResult:
        is_other = fields.mail_type == "other"
        if not fields.source_accounts:
            raise ValueError("source mailbox tag is required")
        payload = {
            "姓名": fields.name,
            "最新邮件时间": latest_time,
            "院校 / 就读信息": "—" if is_other else fields.school_study_display,
            "院校": "—" if is_other else fields.school,
            "专业信息": "—" if is_other else fields.major,
            "邮件类型": [MAIL_TYPES.get(fields.mail_type, "其他")],
            "申请项目": [] if is_other else fields.projects,
            "学业表现": "—" if is_other else fields.academic_display,
            "排名": fields.rank,
            "排名依据": fields.rank_evidence,
            "是否985": [fields.is_985],
            "是否C9": [fields.is_c9],
            "是否已回复": bool(has_replied),
            "来源邮箱": fields.source_accounts,
            "申请目的 / 科研摘要": fields.purpose_summary,
            "材料文档": document_url,
            "筛选状态": [status],
            "是否已分配面试": interview_assigned,
            "面试结果": [interview_result],
        }
        if not record_id:
            record_id = self.find_existing(fields.name, latest_time, document_url)
        if record_id:
            body = {"update_records": {record_id: payload}}
            self._call("+record-batch-update", body)
            return BaseSyncResult(record_id=record_id, created=False)
        result = self._call("+record-batch-create", {"create_records": [payload]})
        ids = result.get("data", {}).get("record_id_list", [])
        if not ids:
            raise RuntimeError(f"Base create returned no record ID: {result}")
        return BaseSyncResult(record_id=str(ids[0]), created=True)

    def find_existing(self, name: str, latest_time: str | None, document_url: str | None = None) -> str | None:
        if not latest_time:
            return None
        if self._existing_index is None:
            self._existing_index = self._load_existing_index()
        key_index, document_index = self._existing_index
        if document_url:
            for candidate, record_id in document_index.items():
                if document_url in candidate:
                    return record_id
        candidates = key_index.get((name, latest_time[:16]), [])
        # Same-name/same-minute messages can be separate threads (especially
        # system notifications).  Never choose one arbitrarily when the key
        # is ambiguous; create a row unless the material document matched.
        return candidates[0] if len(candidates) == 1 else None

    def current_state(self, record_id: str | None) -> dict[str, Any] | None:
        """Return current Base-managed state, preserving manual edits."""
        if not record_id:
            return None
        if self._existing_index is None:
            self._existing_index = self._load_existing_index()
        return (self._record_states or {}).get(record_id)

    def all_states(self, *, refresh: bool = False) -> dict[str, dict[str, Any]]:
        """Return a complete Base snapshot keyed by record ID."""
        if refresh or self._existing_index is None:
            self._existing_index = self._load_existing_index()
        return dict(self._record_states)

    def _load_existing_index(self) -> tuple[dict[tuple[str, str], list[str]], dict[str, str]]:
        key_index: dict[tuple[str, str], list[str]] = defaultdict(list)
        document_index: dict[str, str] = {}
        record_states: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        offset = 0
        while True:
            data = self._list_page(offset)
            ids = list(data.get("record_id_list") or [])
            fields = [str(value) for value in (data.get("fields") or [])]
            rows = list(data.get("data") or [])
            positions = {name: index for index, name in enumerate(fields)}
            if "姓名" not in positions or "最新邮件时间" not in positions:
                raise RuntimeError("Base record list omitted identity fields")
            fresh_ids = 0
            for record_id, row in zip(ids, rows):
                record_id = str(record_id)
                if not record_id or record_id in seen:
                    continue
                seen.add(record_id)
                fresh_ids += 1
                values = {name: _row_value(row, positions.get(name)) for name in BASE_RECORD_FIELDS}
                name = _cell_text(values["姓名"])
                normalized = _normalized_time(values["最新邮件时间"])
                key_index[(name, normalized)].append(record_id)
                document = _cell_text(values["材料文档"])
                if document:
                    document_index[document] = record_id
                    for url in re.findall(r"https?://[^\s()<>\[\]]+", document):
                        document_index[url] = record_id
                record_states[record_id] = _record_state(record_id, values)
            if len(ids) < BASE_PAGE_SIZE:
                break
            if fresh_ids == 0:
                raise RuntimeError("Base pagination returned a repeated page")
            offset += len(ids)
        self._record_states = record_states
        return dict(key_index), document_index

    def _list_page(self, offset: int) -> dict[str, Any]:
        command = [
            self.settings.lark_cli,
            "base",
            "+record-list",
            "--base-token",
            self.settings.base_token,
            "--table-id",
            self.settings.table_id,
        ]
        for field_name in BASE_RECORD_FIELDS:
            command.extend(("--field-id", field_name))
        command.extend((
            "--limit",
            str(BASE_PAGE_SIZE),
            "--offset",
            str(offset),
            "--format",
            "json",
            "--as",
            self.settings.feishu_as,
        ))
        completed = subprocess.run(
            command,
            cwd=self.settings.root,
            env=self.settings.command_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "record-list failed"
            raise RuntimeError(f"Base snapshot failed at offset {offset}: {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Base snapshot returned invalid JSON at offset {offset}") from exc
        if payload.get("ok") is False:
            raise RuntimeError(json.dumps(payload, ensure_ascii=False))
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise RuntimeError("Base snapshot returned invalid data")
        return data

    def _call(self, command: str, body: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            completed = subprocess.run(
                [
                    self.settings.lark_cli,
                    "base",
                    command,
                    "--base-token",
                    self.settings.base_token,
                    "--table-id",
                    self.settings.table_id,
                    "--json",
                    json.dumps(body, ensure_ascii=False),
                    "--as",
                    self.settings.feishu_as,
            ],
                cwd=self.settings.root,
                env=self.settings.command_env(),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"base {command}: {completed.stderr.strip() or completed.stdout.strip() or f'{command} failed'}")
            value = json.loads(completed.stdout)
            if value.get("ok") is False:
                raise RuntimeError(json.dumps(value, ensure_ascii=False))
            return value

        return retry_call(operation, attempts=self.settings.retry_attempts, base_seconds=self.settings.retry_base_seconds, retryable=is_transient_error)


def _cell_text(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or value.get("url") or "")
    return str(value or "")


def _cell_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = _cell_text(value).strip()
    return [text] if text else []


def _cell_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _cell_text(value).strip().casefold() in {"1", "true", "yes", "是"}


def _row_value(row: Any, index: int | None) -> Any:
    if index is None or not isinstance(row, (list, tuple)) or index >= len(row):
        return None
    return row[index]


def _normalized_time(value: Any) -> str:
    text = _cell_text(value)
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16].replace("T", " ")


def _record_state(record_id: str, values: dict[str, Any]) -> dict[str, Any]:
    mail_type_text = _cell_text(values["邮件类型"])
    mail_type = "other" if mail_type_text in {"其他", "other"} else "candidate"
    return {
        "record_id": record_id,
        "name": _cell_text(values["姓名"]),
        "latest_time": _cell_text(values["最新邮件时间"]),
        "study": _cell_text(values["院校 / 就读信息"]),
        "school": _cell_text(values["院校"]),
        "major": _cell_text(values["专业信息"]),
        "mail_type": mail_type,
        "projects": _cell_list(values["申请项目"]),
        "academic_display": _cell_text(values["学业表现"]),
        "rank": _cell_text(values["排名"]),
        "rank_evidence": _cell_text(values["排名依据"]),
        "is_985": _cell_text(values["是否985"]),
        "is_c9": _cell_text(values["是否C9"]),
        "has_replied": _cell_bool(values["是否已回复"]),
        "source_accounts": _cell_list(values["来源邮箱"]),
        "purpose_summary": _cell_text(values["申请目的 / 科研摘要"]),
        "document_url": _cell_url(values["材料文档"]),
        "screening_status": _cell_text(values["筛选状态"]),
        "interview_assigned": _cell_bool(values["是否已分配面试"]),
        "interview_result": _cell_text(values["面试结果"]),
    }


def merge_base_profile(fields: CandidateFields, state: dict[str, Any] | None) -> CandidateFields:
    """Overlay non-empty Base-owned profile fields onto extracted mail data."""
    if not state:
        return fields
    values = asdict(fields)
    for key in ("name", "school", "major", "academic_display", "rank", "rank_evidence", "purpose_summary"):
        value = str(state.get(key) or "").strip()
        if _is_meaningful(value):
            values[key] = value
    projects = [str(value).strip() for value in state.get("projects") or [] if str(value).strip()]
    if projects:
        values["projects"] = projects
    mail_type = str(state.get("mail_type") or "").strip()
    if mail_type in {"candidate", "other"}:
        values["mail_type"] = mail_type
    return CandidateFields(**values).normalized()


def _is_meaningful(value: str) -> bool:
    return value.casefold() not in {"", "—", "-", "unknown", "none", "n/a", "未提供", "不适用"}


def _cell_url(value: Any) -> str:
    text = _cell_text(value).strip()
    match = re.search(r"https?://[^\s()<>\[\]]+", text)
    return match.group(0).rstrip(".,;，。；") if match else text
