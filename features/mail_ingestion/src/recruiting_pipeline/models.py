from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .institution_tags import classify_institution


@dataclass(frozen=True)
class StoredMessage:
    id: int
    source_uid: str
    mailbox: str
    subject: str
    sender_name: str
    sender_address: str
    received_at: str | None
    body_text: str
    raw_path: Path
    attachments: tuple[Path, ...] = ()
    source_account: str = ""


@dataclass(frozen=True)
class ThreadEnvelope:
    key: str
    candidate_address: str
    subject: str
    messages: tuple[StoredMessage, ...]
    incoming: tuple[StoredMessage, ...]
    outgoing: tuple[StoredMessage, ...]
    folders: frozenset[str]
    source_accounts: frozenset[str] = frozenset()

    @property
    def latest_time(self) -> str | None:
        values = [(parse_datetime(message.received_at), message.received_at) for message in self.messages if message.received_at]
        values = [item for item in values if item[0] is not None]
        return max(values, key=lambda item: item[0])[1] if values else None


@dataclass
class CandidateFields:
    name: str = "unknown"
    school: str = "unknown"
    education_stage: str = "unknown"
    entry_year: str = "unknown"
    expected_grad_year: str = "unknown"
    current_grade: str = "unknown"
    major: str = "unknown"
    mail_type: str = "other"
    projects: list[str] = field(default_factory=lambda: ["unknown"])
    academic_display: str = "unknown"
    rank: str = "未提供"
    rank_evidence: str = "未提供"
    is_985: str = "未知"
    is_c9: str = "未知"
    source_accounts: list[str] = field(default_factory=list)
    purpose_summary: str = "unknown"
    rejection_recommendation: str = "none"

    def normalized(self) -> "CandidateFields":
        self.name = (self.name or "unknown").strip()
        self.school = (self.school or "unknown").strip()
        self.education_stage = (self.education_stage or "unknown").strip()
        self.entry_year = (self.entry_year or "unknown").strip()
        self.expected_grad_year = (self.expected_grad_year or "unknown").strip()
        self.current_grade = (self.current_grade or "unknown").strip()
        self.major = (self.major or "unknown").strip()
        self.academic_display = (self.academic_display or "unknown").strip()
        self.purpose_summary = (self.purpose_summary or "unknown").strip()
        self.rejection_recommendation = (self.rejection_recommendation or "none").strip()
        if self.rejection_recommendation not in {"none", "未通过"}:
            self.rejection_recommendation = "none"
        if self.mail_type in {"internship_application", "general_inquiry", "candidate", "候选人来信"}:
            self.mail_type = "candidate"
        elif self.mail_type in {"other", "其他"}:
            self.mail_type = "other"
        else:
            self.mail_type = "other"
        allowed = {"MLSys", "Agentic Infrastructure", "Kernel Efficiency", "World Model", "unknown"}
        self.projects = [item for item in dict.fromkeys(self.projects or ["unknown"]) if item in allowed] or ["unknown"]
        if self.mail_type != "other":
            # For candidate mail, always route to the closest lab topic.  An
            # unknown/omitted project is not a reason to leave the row grey;
            # infer from the extracted research background instead.
            explicit = [item for item in self.projects if item != "unknown"]
            self.projects = explicit or [_infer_project(self.purpose_summary, self.major, self.projects)]
        tags = classify_institution(self.school, applicable=self.mail_type == "candidate")
        self.is_985 = tags.is_985
        self.is_c9 = tags.is_c9
        if self.mail_type == "candidate":
            self.rank = (self.rank or "未提供").strip()
            self.rank_evidence = (self.rank_evidence or "未提供").strip()
            if self.rank.casefold() in {"", "unknown", "none", "n/a", "—"}:
                self.rank = "未提供"
            if self.rank_evidence.casefold() in {"", "unknown", "none", "n/a", "—"}:
                self.rank_evidence = "未提供"
        else:
            self.rank = "不适用"
            self.rank_evidence = "不适用"
        normalized_sources: list[str] = []
        for value in self.source_accounts:
            raw = str(value).strip()
            label = {"zip lab": "ZIP Lab", "bohan": "Bohan"}.get(raw.casefold())
            if label:
                normalized_sources.append(label)
            elif "@" in raw:
                normalized_sources.append(raw.casefold())
        self.source_accounts = list(dict.fromkeys(normalized_sources))
        return self

    @property
    def school_study_display(self) -> str:
        pieces = [self.school]
        if self.education_stage != "unknown":
            pieces.append(self.education_stage)
        if self.current_grade != "unknown":
            pieces.append(self.current_grade)
        if self.entry_year != "unknown":
            pieces.append(f"入学 {self.entry_year}")
        if self.expected_grad_year != "unknown":
            pieces.append(f"预计毕业 {self.expected_grad_year}")
        return "｜".join(pieces)


def _infer_project(summary: str, major: str, projects: list[str]) -> str:
    explicit = [item for item in projects if item != "unknown"]
    if explicit:
        return explicit[0]
    text = f"{summary} {major}".casefold()
    scores = {
        "World Model": sum(token in text for token in ("3d", "world model", "高斯", "gaussian", "vlm", "video", "机器人", "robot", "视觉")),
        "Agentic Infrastructure": sum(token in text for token in ("agent", "moe", "rag", "kv cache", "serving", "推理系统", "分布式")),
        "Kernel Efficiency": sum(token in text for token in ("cuda", "triton", "kernel", "量化", "quant", "speculative", "推理加速", "gpu")),
        "MLSys": sum(token in text for token in ("mlsys", "machine learning system", "pytorch", "系统", "systems")),
    }
    # Stable tie-break order keeps inferred labels deterministic.
    priority = ("MLSys", "Agentic Infrastructure", "Kernel Efficiency", "World Model")
    return max(scores, key=lambda name: (scores[name], -priority.index(name)))


@dataclass(frozen=True)
class ProcessedThread:
    thread_key: str
    candidate_address: str
    latest_time: str | None
    fields: CandidateFields
    folder_status: str | None
    interview_assigned: bool | None
    document_id: str | None
    document_url: str | None
    changed: bool


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None
