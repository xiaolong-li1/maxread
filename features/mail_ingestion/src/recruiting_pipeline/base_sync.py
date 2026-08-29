from __future__ import annotations

import json
import re
import subprocess
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
            "是否985": [fields.is_985],
            "是否C9": [fields.is_c9],
            "是否已回复": bool(has_replied),
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

    def _load_existing_index(self) -> tuple[dict[tuple[str, str], list[str]], dict[str, str]]:
        completed = subprocess.run(
            [
                self.settings.lark_cli,
                "base",
                "+record-list",
                "--base-token",
                self.settings.base_token,
                "--table-id",
                self.settings.table_id,
                "--field-id",
                "姓名",
                "--field-id",
                "最新邮件时间",
                "--field-id",
                "材料文档",
                "--field-id",
                "筛选状态",
                "--field-id",
                "是否已分配面试",
                "--field-id",
                "面试结果",
                "--limit",
                "200",
                "--format",
                "json",
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
            self._record_states = {}
            return {}, {}
        try:
            data = json.loads(completed.stdout).get("data", {})
            ids = data.get("record_id_list", [])
            fields = data.get("fields", [])
            rows = data.get("data", [])
            key_index: dict[tuple[str, str], list[str]] = defaultdict(list)
            document_index: dict[str, str] = {}
            name_index = fields.index("姓名")
            time_index = fields.index("最新邮件时间")
            document_field_index = fields.index("材料文档") if "材料文档" in fields else None
            status_index = fields.index("筛选状态") if "筛选状态" in fields else None
            assigned_index = fields.index("是否已分配面试") if "是否已分配面试" in fields else None
            result_index = fields.index("面试结果") if "面试结果" in fields else None
            self._record_states = {}
            for record_id, row in zip(ids, rows):
                name = str(row[name_index] or "")
                raw_time = row[time_index]
                normalized = str(raw_time or "")
                if isinstance(raw_time, str):
                    try:
                        normalized = datetime.fromisoformat(raw_time).strftime("%Y-%m-%d %H:%M")
                    except ValueError:
                        normalized = raw_time[:16].replace("T", " ")
                record_id = str(record_id)
                key_index[(name, normalized)].append(record_id)
                if document_field_index is not None:
                    document = str(row[document_field_index] or "")
                    if document:
                        document_index[document] = record_id
                        for url in re.findall(r"https?://[^)\\s]+", document):
                            document_index[url] = record_id
                self._record_states[record_id] = {
                    "screening_status": _cell_text(row[status_index]) if status_index is not None else "",
                    "interview_assigned": bool(row[assigned_index]) if assigned_index is not None else False,
                    "interview_result": _cell_text(row[result_index]) if result_index is not None else "",
                }
            return dict(key_index), document_index
        except (KeyError, IndexError, TypeError, ValueError):
            return {}, {}

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
    return str(value or "")
