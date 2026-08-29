from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

from .feishu import normalize_doc_url
from .openai_client import OpenAIClient
from .render import _is_valid_latex_body, _normalize_latex_body, _strip_latex_for_text
from .workflow import WorkflowEvent


class VisualFeishuClient(Protocol):
    def fetch_docx(self, doc_url: str, doc_format: str = "xml", scope: str = "", detail: str = "simple") -> Dict[str, Any]:
        ...

    def block_replace(self, doc_url: str, block_id: str, content: str) -> Dict[str, Any]:
        ...


@dataclass
class VisualFinding:
    kind: str
    severity: str = "medium"
    detail: str = ""
    section: str = ""
    image_name: str = ""
    block_id: str = ""
    screenshot: str = ""
    autofixable: bool = False
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "VisualFinding":
        return cls(
            kind=str(payload.get("kind") or "unknown"),
            severity=str(payload.get("severity") or "medium"),
            detail=str(payload.get("detail") or ""),
            section=str(payload.get("section") or ""),
            image_name=str(payload.get("image_name") or ""),
            block_id=str(payload.get("block_id") or ""),
            screenshot=str(payload.get("screenshot") or ""),
            autofixable=bool(payload.get("autofixable")),
            data=dict(payload.get("data") or {}),
        )


@dataclass
class RemoteVisualResult:
    status: str = "disabled"
    findings: List[VisualFinding] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class VisualRepairResult:
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    repaired_blocks: List[str] = field(default_factory=list)
    remote: Optional[RemoteVisualResult] = None
    rounds: List["VisualRepairRound"] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.remote is None or bool(not self.remote.error and not self.remote.findings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "changed": self.changed,
            "warnings": list(self.warnings),
            "repaired_blocks": list(self.repaired_blocks),
            "remote": _remote_to_dict(self.remote) if self.remote else None,
            "rounds": [item.to_dict() for item in self.rounds],
        }


@dataclass
class VisualRepairRound:
    round_index: int
    status: str
    findings: List[Dict[str, Any]] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)
    repair_strategy: str = ""
    changed: bool = False
    repaired_blocks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    model_used: bool = False
    model_response: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round": self.round_index,
            "status": self.status,
            "findings": list(self.findings),
            "screenshots": list(self.screenshots),
            "repair_strategy": self.repair_strategy,
            "changed": self.changed,
            "repaired_blocks": list(self.repaired_blocks),
            "warnings": list(self.warnings),
            "model_used": self.model_used,
            "model_response": self.model_response,
            "error": self.error,
        }


def _visual_qa_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("MAXREAD_VISUAL_QA_CONCURRENCY", "1")))
    except ValueError:
        return 1


_QA_LOCK = threading.BoundedSemaphore(_visual_qa_concurrency())
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "table"}
_RESOURCE_TAGS = {"img", "source", "whiteboard", "sheet", "bitable", "cite", "synced_reference"}
_FORMULA_RE = re.compile(r"<latex>(.*?)</latex>", flags=re.S | re.I)
_RAW_UNCERTAINTY_RE = re.compile(
    r"(?<![\w.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:"
    r"\^\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}\s*_\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}"
    r"|_\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}\s*\^\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}"
    r")(?![\w.])"
)


class VisualQAController:
    """Run screenshot QA, repair the published document, and verify the repair."""

    def __init__(
        self,
        enabled: bool = False,
        host: str = "",
        runner: str = "run_visual_qa.sh",
        remote_root: str = "",
        timeout: int = 90,
        inspect_retries: int = 2,
        max_sections: int = 12,
        max_repairs: int = 2,
        repair_rounds: int = 2,
        llm=None,
        reasoning_effort: str = "high",
    ):
        self.enabled = bool(enabled)
        self.host = str(host or "").strip()
        self.runner = str(runner)
        self.remote_root = str(remote_root).rstrip("/")
        self.timeout = max(15, int(timeout or 90))
        self.inspect_retries = max(0, int(inspect_retries or 0))
        self.max_sections = max(1, int(max_sections or 12))
        self.max_repairs = max(0, int(max_repairs or 2))
        self.repair_rounds = max(0, int(repair_rounds if repair_rounds is not None else 2))
        self.llm = llm
        self.reasoning_effort = str(reasoning_effort or "high")

    @classmethod
    def from_settings(cls, settings, llm=None) -> "VisualQAController":
        visual_key = str(getattr(settings, "visual_openai_api_key", "") or "").strip()
        if visual_key:
            llm = OpenAIClient(
                visual_key,
                getattr(settings, "visual_model", "") or getattr(settings, "model", "gpt-4.1"),
                timeout=getattr(settings, "openai_timeout", 180),
                base_url=getattr(settings, "visual_openai_base_url", "") or getattr(settings, "openai_base_url", "https://api.openai.com/v1"),
                sub_module=getattr(settings, "visual_openai_sub_module", "") or getattr(settings, "openai_sub_module", ""),
                reasoning_effort=getattr(settings, "openai_reasoning_effort", "high"),
                api_mode=getattr(settings, "visual_openai_api_mode", "") or getattr(settings, "openai_api_mode", "responses"),
            )
        return cls(
            enabled=getattr(settings, "visual_qa_enabled", False),
            host=getattr(settings, "visual_qa_host", ""),
            runner=getattr(settings, "visual_qa_runner", ""),
            remote_root=getattr(settings, "visual_qa_remote_root", ""),
            timeout=getattr(settings, "visual_qa_timeout", 90),
            inspect_retries=getattr(settings, "visual_qa_inspect_retries", 2),
            max_sections=getattr(settings, "visual_qa_max_sections", 12),
            max_repairs=getattr(settings, "visual_qa_max_repairs", 2),
            repair_rounds=getattr(settings, "visual_qa_repair_rounds", 2),
            llm=llm,
            reasoning_effort=getattr(settings, "openai_reasoning_effort", "high"),
        )

    def run(
        self,
        feishu: VisualFeishuClient,
        doc_url: str,
        initial_warnings: Iterable[str] = (),
        source_id: str = "",
        expected_image_min: int = 0,
        expected_formula_min: int = 0,
        expected_table_min: int = 0,
        previous_feedback: Iterable[str] = (),
        on_workflow_event=None,
    ) -> VisualRepairResult:
        result = VisualRepairResult()
        initial = list(initial_warnings)
        feedback_history = _dedupe_text(previous_feedback)

        # Structural repair is useful even when the remote browser is disabled.
        structural_changed, structural_warnings, repaired = repair_structural_blocks(
            feishu, doc_url, initial, max_repairs=self.max_repairs
        )
        result.changed = structural_changed
        result.warnings.extend(structural_warnings)
        result.repaired_blocks.extend(repaired)

        if not self.enabled:
            return result

        # A visual repair round always has the same shape: inspect the real
        # document, make at most one bounded patch, then inspect again. Older
        # code mixed the first and second pass, which left stale findings in
        # the final warning list and made the configured retry count ineffective.
        for round_index in range(self.repair_rounds + 1):
            suffix = source_id if round_index == 0 else f"{source_id}-visual-r{round_index}"
            remote = self.inspect_remote(
                doc_url,
                source_id=suffix,
                expected_image_min=expected_image_min,
                expected_formula_min=expected_formula_min,
                expected_table_min=expected_table_min,
            )
            result.remote = remote
            audit_round = VisualRepairRound(
                round_index=round_index,
                status=remote.status,
                findings=[_finding_to_dict(item) for item in remote.findings],
                screenshots=list(remote.screenshots),
                error=remote.error,
            )
            result.rounds.append(audit_round)
            if remote.status == "infrastructure_pending":
                result.warnings.append(
                    f"visual-qa:infrastructure:export-pending:{_clip(remote.error or 'Feishu PDF export is still processing')}"
                )
                break
            if remote.error:
                result.warnings.append(f"visual-qa:remote-error:{_clip(remote.error)}")
                break
            ignored = [
                finding
                for finding in remote.findings
                if _is_nonblocking_visual_finding(
                    finding,
                    remote=remote,
                    api_warnings=initial,
                    max_sections=self.max_sections,
                )
            ]
            if ignored:
                result.warnings.extend(
                    _nonblocking_finding_warning(finding, remote, initial, self.max_sections)
                    for finding in ignored
                )
                remote.findings = [
                    finding
                    for finding in remote.findings
                    if not _is_nonblocking_visual_finding(
                        finding,
                        remote=remote,
                        api_warnings=initial,
                        max_sections=self.max_sections,
                    )
                ]
            if not remote.findings:
                audit_round.status = "passed-with-warnings" if ignored else "passed"
                break
            if round_index >= self.repair_rounds:
                result.warnings.extend(_finding_warning("visual-qa", finding) for finding in remote.findings)
                break

            if on_workflow_event is not None:
                on_workflow_event(
                    WorkflowEvent.VISUAL_REPAIR_REQUIRED,
                    f"round={round_index + 1}; findings={len(remote.findings)}",
                )
            changed, repair_warnings, repaired, strategy, model_response = self._repair_remote_findings(
                feishu,
                doc_url,
                remote.findings,
                previous_feedback=feedback_history,
            )
            audit_round.repair_strategy = strategy
            audit_round.changed = changed
            audit_round.repaired_blocks = list(repaired)
            audit_round.warnings = list(repair_warnings)
            audit_round.model_used = bool(model_response)
            audit_round.model_response = model_response
            result.warnings.extend(
                warning for warning in repair_warnings if warning.startswith("visual-repair:")
            )
            if changed:
                result.changed = True
                result.repaired_blocks.extend(repaired)
            feedback_history = _dedupe_text(
                feedback_history
                + [_visual_finding_feedback(item, round_index) for item in remote.findings]
                + list(repair_warnings)
            )
            if on_workflow_event is not None:
                on_workflow_event(WorkflowEvent.VISUAL_RECHECK, f"round={round_index + 1}")
            if not changed:
                retryable = any(
                    any(
                        token in warning
                        for token in (
                            "visual-repair:model-call-failed",
                            "visual-repair:model-fetch-failed",
                            "visual-repair:model-invalid-json",
                            "visual-repair:model-invalid-schema",
                        )
                    )
                    for warning in repair_warnings
                )
                if retryable and round_index < self.repair_rounds:
                    audit_round.status = "retryable-failure"
                    continue
                result.warnings.extend(_finding_warning("visual-qa", finding) for finding in remote.findings)
                audit_round.status = "stalled"
                break

        if result.passed:
            _cleanup_successful_visual_runs(self.remote_root, result)
        return result

    def _repair_remote_findings(
        self,
        feishu: VisualFeishuClient,
        doc_url: str,
        findings: List[VisualFinding],
        previous_feedback: Iterable[str] = (),
    ) -> tuple[bool, List[str], List[str], str, str]:
        structural = [item for item in findings if item.kind in {"invalid-formula", "raw-formatting"}]
        image_findings = [item for item in findings if item.kind == "image-overflow"]
        changed = False
        warnings: List[str] = []
        blocks: List[str] = []
        strategies: List[str] = []
        model_response = ""
        if structural:
            structural_changed, structural_warnings, structural_blocks = repair_structural_blocks(
                feishu,
                doc_url,
                ["visual-qa:repairable-structural"],
                max_repairs=self.max_repairs,
            )
            changed = changed or structural_changed
            warnings.extend(structural_warnings)
            blocks.extend(structural_blocks)
            if structural_changed:
                strategies.append("deterministic-structural")
            elif self.llm is not None:
                model_changed, model_warnings, model_blocks, model_response = repair_formula_blocks_with_llm(
                    self.llm,
                    feishu,
                    doc_url,
                    findings,
                    max_repairs=self.max_repairs,
                    reasoning_effort=self.reasoning_effort,
                    previous_feedback=previous_feedback,
                )
                changed = changed or model_changed
                warnings.extend(model_warnings)
                blocks.extend(model_blocks)
                strategies.append("model-formula" if model_response else "model-formula-unavailable")
            else:
                strategies.append("deterministic-structural-no-change")

        if image_findings:
            image_changed, image_warnings, image_blocks = repair_image_findings(
                feishu,
                doc_url,
                image_findings,
                max_repairs=self.max_repairs,
            )
            changed = changed or image_changed
            warnings.extend(image_warnings)
            blocks.extend(image_blocks)
            strategies.append("deterministic-image" if image_changed else "deterministic-image-no-change")

        if not strategies:
            strategies.append("no-supported-repair")
        return changed, warnings, _dedupe_text(blocks), "+".join(strategies), model_response

    def inspect_remote(
        self,
        doc_url: str,
        source_id: str = "",
        expected_image_min: int = 0,
        expected_formula_min: int = 0,
        expected_table_min: int = 0,
    ) -> RemoteVisualResult:
        if not self.enabled:
            return RemoteVisualResult(status="disabled")
        if not self.runner.strip():
            return RemoteVisualResult(
                status="error",
                error="visual QA is enabled but MAXREAD_VISUAL_QA_RUNNER is not configured",
            )
        local_hosts = {"", "local", "localhost", "127.0.0.1", "::1", socket.gethostname().lower()}
        if self.host.strip().lower() not in local_hosts and not self.remote_root:
            return RemoteVisualResult(
                status="error",
                error="remote visual QA requires MAXREAD_VISUAL_QA_REMOTE_ROOT",
            )
        normalized_url = normalize_doc_url(doc_url)
        if not normalized_url:
            return RemoteVisualResult(status="error", error=f"invalid Feishu doc URL: {_clip(doc_url)}")
        doc_url = normalized_url
        tag = _safe_tag(source_id or doc_url)
        attempt_audit: List[Dict[str, Any]] = []
        last_error = "visual runner failed"
        for attempt_index in range(self.inspect_retries + 1):
            run_id = f"{tag}-inspect-a{attempt_index + 1}-{uuid.uuid4().hex[:8]}"
            remote_dir = f"{self.remote_root}/runs/{run_id}"
            command = " ".join(
                [
                    shlex.quote(self.runner),
                    "--url",
                    shlex.quote(doc_url),
                    "--output-dir",
                    shlex.quote(remote_dir),
                    "--max-sections",
                    str(self.max_sections),
                    "--expected-images",
                    str(max(0, int(expected_image_min or 0))),
                    "--expected-formulas",
                    str(max(0, int(expected_formula_min or 0))),
                    "--expected-tables",
                    str(max(0, int(expected_table_min or 0))),
                ]
            )
            attempt_timeout = self.timeout if attempt_index == 0 else self.timeout * 2
            try:
                if self.host.strip().lower() in local_hosts:
                    argv = shlex.split(command)
                else:
                    argv = [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        f"ConnectTimeout={min(15, self.timeout)}",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=1",
                        self.host,
                        command,
                    ]
                with _QA_LOCK:
                    completed = subprocess.run(
                        argv,
                        text=True,
                        capture_output=True,
                        timeout=attempt_timeout,
                        check=False,
                    )
            except Exception as exc:
                last_error = str(exc)
                attempt_audit.append(
                    {"attempt": attempt_index + 1, "timeout": attempt_timeout, "status": "error", "error": _clip(last_error, 1000)}
                )
                continue
            if completed.returncode != 0:
                last_error = completed.stderr.strip() or completed.stdout.strip() or f"runner exit {completed.returncode}"
                attempt_audit.append(
                    {"attempt": attempt_index + 1, "timeout": attempt_timeout, "status": "error", "error": _clip(last_error, 1000)}
                )
                continue
            payload = _last_json_object(completed.stdout)
            if not payload:
                last_error = "remote runner returned no JSON"
                attempt_audit.append(
                    {"attempt": attempt_index + 1, "timeout": attempt_timeout, "status": "error", "error": last_error}
                )
                continue
            screenshots = [str(item) for item in payload.get("screenshots", [])]
            if str(payload.get("status") or "") == "ok" and not screenshots:
                last_error = "remote runner returned no screenshots"
                attempt_audit.append(
                    {"attempt": attempt_index + 1, "timeout": attempt_timeout, "status": "error", "error": last_error}
                )
                continue
            attempt_audit.append(
                {"attempt": attempt_index + 1, "timeout": attempt_timeout, "status": "ok", "run_dir": remote_dir}
            )
            payload["inspect_attempts"] = list(attempt_audit)
            findings = [VisualFinding.from_dict(item) for item in payload.get("findings", []) if isinstance(item, dict)]
            return RemoteVisualResult(
                status=str(payload.get("status") or "ok"),
                findings=findings,
                screenshots=screenshots,
                raw=payload,
                error=str(payload.get("error") or ""),
            )
        return RemoteVisualResult(
            status="error",
            raw={"inspect_attempts": attempt_audit},
            error=f"visual runner failed after {len(attempt_audit)} attempts: {last_error}",
        )


def repair_structural_blocks(
    feishu: VisualFeishuClient,
    doc_url: str,
    warnings: Iterable[str],
    max_repairs: int = 2,
) -> tuple[bool, List[str], List[str]]:
    """Repair round-trip formula corruption using fresh block IDs."""
    if max_repairs <= 0 or not hasattr(feishu, "block_replace"):
        return False, [], []
    relevant = [str(item) for item in warnings if _structural_warning_is_repairable(str(item))]
    if not relevant:
        return False, [], []
    try:
        content = _fetch_xml(feishu, doc_url)
        root = ET.fromstring(f"<root>{content}</root>")
    except Exception as exc:
        return False, [f"visual-repair:fetch-failed:{_clip(str(exc))}"], []

    candidates: List[tuple[str, str]] = []
    for element in root.iter():
        block_id = str(element.attrib.get("id") or "")
        if not block_id or element.tag.lower() not in _BLOCK_TAGS:
            continue
        if any(child.tag.lower() in _RESOURCE_TAGS for child in element.iter() if child is not element):
            continue
        serialized = ET.tostring(element, encoding="unicode", short_empty_elements=True)
        repaired = _repair_formula_xml_block(serialized)
        repaired = _repair_raw_formatting_xml_block(repaired)
        if repaired and repaired != serialized:
            candidates.append((block_id, repaired))

    changed = False
    repaired_blocks: List[str] = []
    audit: List[str] = []
    for block_id, replacement in candidates[:max_repairs]:
        try:
            feishu.block_replace(doc_url, block_id, replacement)
            changed = True
            repaired_blocks.append(block_id)
            audit.append(f"visual-repair:structural-block:{block_id}")
        except Exception as exc:
            audit.append(f"visual-repair:block-failed:{block_id}:{_clip(str(exc))}")
    return changed, audit, repaired_blocks


_VISUAL_FORMULA_REPAIR_SYSTEM = """你是飞书文档的公式和格式字符修复器。你只负责修复浏览器截图已经确认的“无效公式”或直接显示出来的 TeX/Markdown 控制字符。

只输出一个 JSON 对象，不要解释，不要代码围栏：
{"repairs":[{"id":"已提供的 block id","mode":"latex|text|plain","value":"修复后的内容"}]}

硬约束：
1. 只能使用输入中出现的 block id；不确定就返回空 repairs。
2. mode=latex 时 value 只能是公式体，不要包 <latex>，不得包含 HTML、中文、Markdown 或美元符号。
3. mode=text 时把公式降级为短文本/代码，只有原内容明显是代码或无法保持数学含义时才使用。
4. mode=plain 只用于 KIND=raw 的正文 block，返回去掉控制命令后的可读纯文本，不要返回 Markdown、TeX 或 HTML。
5. 不改变变量、上下标、数值、运算关系和语义；只修复不支持的宏、粘连命令、HTML/CJK 混入或非法转义。
6. 不要返回 XML，不要修改未发现问题的 block。
"""


def repair_formula_blocks_with_llm(
    llm,
    feishu: VisualFeishuClient,
    doc_url: str,
    findings: Iterable[VisualFinding],
    max_repairs: int = 2,
    reasoning_effort: str = "high",
    previous_feedback: Iterable[str] = (),
) -> tuple[bool, List[str], List[str], str]:
    """Use the model only after deterministic XML repair made no progress.

    The model returns formula bodies keyed by fresh Feishu block IDs. We still
    validate the IDs and the resulting LaTeX locally before touching the doc.
    This keeps model output from becoming arbitrary document XML.
    """
    if max_repairs <= 0 or not hasattr(feishu, "block_replace"):
        return False, [], [], ""
    findings = list(findings)
    try:
        content = _fetch_xml(feishu, doc_url)
        root = ET.fromstring(f"<root>{content}</root>")
    except Exception as exc:
        return False, [f"visual-repair:model-fetch-failed:{_clip(str(exc))}"], [], ""

    candidates = _structural_block_candidates(root, max_candidates=max(64, max_repairs * 16))
    if not candidates:
        return False, ["visual-repair:model-no-structural-block"], [], ""
    hinted_ids = _finding_block_ids(findings)
    if hinted_ids:
        candidates.sort(key=lambda item: (item["id"] not in hinted_ids, item["id"]))
    candidate_by_id = {item["id"]: item for item in candidates}
    finding_text = "\n".join(
        f"- {finding.kind}: {finding.detail} [section={finding.section}] "
        f"[dom={json.dumps(finding.data, ensure_ascii=False)}]"
        for finding in findings
        if finding.kind in {"invalid-formula", "raw-formatting"}
    ) or "- 浏览器发现公式或格式控制字符无法正常渲染"
    formula_text = "\n".join(
        f"BLOCK id={item['id']}\nKIND={item['kind']}\nFORMULA={item['formula']}\nCONTEXT={item['context']}"
        for item in candidates
    )
    history_text = "\n".join(f"- {item}" for item in _dedupe_text(previous_feedback)) or "- 无"
    user = f"""浏览器截图质检发现以下问题：
{finding_text}

之前轮次的失败与修复记录（不得重复同样的无效修改）：
{history_text}

下面是当前文档中可疑的公式 block。只修复确实相关的 block：
{formula_text}
"""

    raw = ""
    try:
        screenshot = next(
            (Path(finding.screenshot) for finding in findings if finding.screenshot and Path(finding.screenshot).exists()),
            None,
        )
        if screenshot is not None and hasattr(llm, "responses_image_text"):
            raw = llm.responses_image_text(_VISUAL_FORMULA_REPAIR_SYSTEM, user, screenshot)
        else:
            raw = llm.responses_text(
                _VISUAL_FORMULA_REPAIR_SYSTEM,
                user,
                reasoning_effort=reasoning_effort,
            )
    except Exception as exc:
        return False, [f"visual-repair:model-call-failed:{_clip(str(exc))}"], [], ""

    payload = _parse_model_json(raw)
    if not isinstance(payload, dict):
        return False, ["visual-repair:model-invalid-json"], [], raw[:20000]
    repairs = payload.get("repairs")
    if not isinstance(repairs, list):
        return False, ["visual-repair:model-invalid-schema"], [], raw[:20000]

    changed = False
    warnings: List[str] = []
    repaired_blocks: List[str] = []
    for item in repairs[:max_repairs]:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("id") or "")
        candidate = candidate_by_id.get(block_id)
        if candidate is None:
            warnings.append(f"visual-repair:model-unknown-block:{_clip(block_id)}")
            continue
        mode = str(item.get("mode") or "latex").strip().lower()
        value = html.unescape(str(item.get("value") or "")).strip()
        value = re.sub(r"</?latex[^>]*>", "", value, flags=re.I).strip()
        if mode == "plain" and candidate["kind"] == "raw":
            plain = re.sub(r"<[^>]+>", "", value)
            plain = " ".join(plain.split())
            if not plain or _raw_block_has_formatting_artifact(plain):
                warnings.append(f"visual-repair:model-invalid-plain:{block_id}")
                continue
            replacement = _plain_block_xml(candidate, plain)
        elif mode == "text" and candidate["kind"] == "formula":
            replacement = re.sub(
                _FORMULA_RE,
                lambda _match: f"<code>{html.escape(_strip_latex_for_text(value), quote=False)}</code>",
                candidate["xml"],
                count=1,
            )
        elif mode == "latex" and candidate["kind"] == "formula":
            body = _normalize_latex_body(value)
            if not _is_valid_latex_body(body):
                warnings.append(f"visual-repair:model-invalid-latex:{block_id}")
                continue
            replacement = re.sub(
                _FORMULA_RE,
                lambda _match: f"<latex>{html.escape(body, quote=False)}</latex>",
                candidate["xml"],
                count=1,
            )
        else:
            warnings.append(f"visual-repair:model-invalid-mode:{block_id}")
            continue
        if replacement == candidate["xml"]:
            continue
        replacement = re.sub(r'\s+id="[^"]+"', "", replacement, count=1)
        try:
            feishu.block_replace(doc_url, block_id, replacement)
            changed = True
            repaired_blocks.append(block_id)
            warnings.append(f"visual-repair:model-block:{block_id}")
        except Exception as exc:
            warnings.append(f"visual-repair:model-block-failed:{block_id}:{_clip(str(exc))}")
    if not changed and not warnings:
        warnings.append("visual-repair:model-no-change")
    return changed, warnings, repaired_blocks, raw[:20000]


def _structural_block_candidates(root: ET.Element, max_candidates: int = 24) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    for element in root.iter():
        block_id = str(element.attrib.get("id") or "")
        if not block_id or element.tag.lower() not in _BLOCK_TAGS:
            continue
        if any(child.tag.lower() in _RESOURCE_TAGS for child in element.iter() if child is not element):
            continue
        serialized = ET.tostring(element, encoding="unicode", short_empty_elements=True)
        match = _FORMULA_RE.search(serialized)
        context = " ".join(str(text or "") for text in element.itertext())
        kind = "formula" if match else "raw"
        if not match and not _raw_block_has_formatting_artifact(context):
            continue
        formula = html.unescape(match.group(1)) if match else ""
        formula = re.sub(r"<[^>]+>", " ", formula)
        attrs = " ".join(
            f'{key}="{html.escape(str(value), quote=True)}"'
            for key, value in element.attrib.items()
            if key != "id"
        )
        candidates.append(
            {
                "id": block_id,
                "kind": kind,
                "formula": _clip(formula, 600),
                "context": _clip(context, 360),
                "xml": serialized,
                "tag": element.tag,
                "attrs": attrs,
            }
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def _raw_block_has_formatting_artifact(text: str) -> bool:
    value = html.unescape(str(text or ""))
    return bool(
        re.search(
            r"\\(?:textbf|textit|textsc|mathrm|operatorname|mathbf|mathcal)(?:\b|(?=[A-Z]))"
            r"|(?<!\\)\$\$|\\\(|\\\)|\\\[|\\\]"
            r"|^\s*\|\s*[-:]+(?:\s*\|\s*[-:]+)+\s*\|?\s*$",
            value,
            flags=re.M,
        )
    )


def _plain_block_xml(candidate: Dict[str, str], value: str) -> str:
    tag = candidate.get("tag") or "p"
    attrs = candidate.get("attrs") or ""
    suffix = f" {attrs}" if attrs else ""
    return f"<{tag}{suffix}>{html.escape(value, quote=False)}</{tag}>"


def _finding_block_ids(findings: Iterable[VisualFinding]) -> set[str]:
    output: set[str] = set()
    for finding in findings:
        if finding.block_id:
            output.add(finding.block_id)
        contexts = finding.data.get("contexts") if isinstance(finding.data, dict) else None
        if not isinstance(contexts, list):
            continue
        for item in contexts:
            if isinstance(item, dict) and item.get("block_id"):
                output.add(str(item["block_id"]))
    return output


def repair_image_findings(
    feishu: VisualFeishuClient,
    doc_url: str,
    findings: Iterable[VisualFinding],
    max_repairs: int = 1,
) -> tuple[bool, List[str], List[str]]:
    if max_repairs <= 0 or not hasattr(feishu, "block_replace"):
        return False, [], []
    candidates = [item for item in findings if item.autofixable and item.kind == "image-overflow"]
    if not candidates:
        return False, [], []
    try:
        content = _fetch_xml(feishu, doc_url)
        root = ET.fromstring(f"<root>{content}</root>")
    except Exception as exc:
        return False, [f"visual-repair:image-fetch-failed:{_clip(str(exc))}"], []
    by_name = {str(item.attrib.get("name") or ""): item for item in root.iter() if item.tag.lower() == "img"}
    by_id = {str(item.attrib.get("id") or ""): item for item in root.iter() if item.tag.lower() == "img"}
    changed = False
    warnings: List[str] = []
    blocks: List[str] = []
    for finding in candidates[:max_repairs]:
        image = by_id.get(finding.block_id)
        if image is None:
            image = by_name.get(finding.image_name)
        if image is None:
            warnings.append(f"visual-repair:image-not-found:{finding.image_name or finding.block_id}")
            continue
        block_id = str(image.attrib.get("id") or "")
        if not block_id:
            warnings.append(f"visual-repair:image-no-block-id:{finding.image_name}")
            continue
        current_width = _positive_int(image.attrib.get("width"), 0)
        current_height = _positive_int(image.attrib.get("height"), 0)
        editor_width = _positive_int(finding.data.get("editor_width"), 0)
        if not editor_width:
            continue
        target_width = max(320, min(current_width or editor_width, int(editor_width * 0.82)))
        if current_width and target_width >= current_width - 4:
            continue
        target_height = current_height
        if current_width and current_height:
            target_height = max(1, round(current_height * target_width / current_width))
        replacement = _image_xml_without_id(image, target_width, target_height)
        try:
            feishu.block_replace(doc_url, block_id, replacement)
            changed = True
            blocks.append(block_id)
            warnings.append(f"visual-repair:image-width:{finding.image_name}:{current_width}->{target_width}")
        except Exception as exc:
            warnings.append(f"visual-repair:image-block-failed:{finding.image_name}:{_clip(str(exc))}")
    return changed, warnings, blocks


def _repair_formula_xml_block(serialized: str) -> str:
    def replace(match: re.Match[str]) -> str:
        encoded = match.group(1)
        has_nested_markup = bool(re.search(r"<[^>]+>", encoded))
        raw = html.unescape(encoded)
        raw = re.sub(r"<br\s*/?>", " ", raw, flags=re.I)
        raw = re.sub(r"<[^>]+>", "", raw)
        body = _normalize_latex_body(raw.strip())
        if not has_nested_markup and body == raw.strip() and _is_valid_latex_body(body):
            return match.group(0)
        if _looks_like_code_formula(raw):
            return f"<code>{html.escape(_strip_latex_for_text(raw), quote=False)}</code>"
        if _is_valid_latex_body(body):
            return f"<latex>{html.escape(body, quote=False)}</latex>"
        return f"<code>{html.escape(_strip_latex_for_text(body), quote=False)}</code>"

    repaired = _FORMULA_RE.sub(replace, serialized)
    if repaired == serialized:
        return serialized
    return re.sub(r'\s+id="[^"]+"', "", repaired, count=1)


def _repair_raw_formatting_xml_block(serialized: str) -> str:
    original = str(serialized or "")
    replacements = (
        (r"\\textbf\{([^{}<>]{1,600})\}", r"<b>\1</b>"),
        (r"\\(?:textit|emph)\{([^{}<>]{1,600})\}", r"<em>\1</em>"),
        (r"\\(?:texttt|mathtt)\{([^{}<>]{1,600})\}", r"<code>\1</code>"),
        (r"\\(?:textnormal|textsc|textrm|mathrm|mathbf|operatorname)\{([^{}<>]{1,600})\}", r"\1"),
    )
    protected = r"((?:<latex(?:\s[^>]*)?>.*?</latex>|<code(?:\s[^>]*)?>.*?</code>|<pre(?:\s[^>]*)?>.*?</pre>))"
    parts = re.split(protected, original, flags=re.S | re.I)
    for index in range(0, len(parts), 2):
        repaired = parts[index]
        repaired = _RAW_UNCERTAINTY_RE.sub(lambda match: f"<latex>{match.group(0)}</latex>", repaired)
        for pattern, replacement in replacements:
            repaired = re.sub(pattern, replacement, repaired)
        parts[index] = re.sub(
            r"\\(?:textbf|textit|textsc|textrm|texttt|textnormal|emph|mathrm|mathbf|mathtt|operatorname)(?:\b|(?=[A-Z]))\s*",
            "",
            repaired,
        )
    repaired = "".join(parts)
    if repaired == original:
        return original
    return re.sub(r'\s+id="[^"]+"', "", repaired, count=1)


def _looks_like_code_formula(text: str) -> bool:
    value = str(text or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) and "_" in value:
        return True
    return bool(re.search(r"\b(?:int|float|len|range|torch|numpy)\s*\(|//|:=", value))


def _image_xml_without_id(element: ET.Element, width: int, height: int) -> str:
    attrs: Dict[str, str] = {}
    for key, value in element.attrib.items():
        if key == "id":
            continue
        attrs[key] = value
    attrs["width"] = str(width)
    if height > 0:
        attrs["height"] = str(height)
    rendered = " ".join(f'{key}="{html.escape(str(value), quote=True)}"' for key, value in attrs.items())
    return f"<img {rendered}/>"


def _structural_warning_is_repairable(warning: str) -> bool:
    return any(
        token in warning
        for token in (
            "html-tag-in-formula",
            "fused-accent-command",
            "joined-spacing-command",
            "split-latex-command",
            "unsupported-paper-macro",
            "unsupported-tensor-macro",
            "unsupported-position-macro",
            "internal-display-delimiter",
            "visual-qa:repairable-structural",
        )
    )


def _fetch_xml(feishu: VisualFeishuClient, doc_url: str) -> str:
    payload = feishu.fetch_docx(doc_url, doc_format="xml", detail="with-ids")
    document = payload.get("data", {}).get("document", {}) if isinstance(payload, dict) else {}
    return str(document.get("content") or "") if isinstance(document, dict) else ""


def _finding_to_dict(finding: VisualFinding) -> Dict[str, Any]:
    return {
        "kind": finding.kind,
        "severity": finding.severity,
        "detail": finding.detail,
        "section": finding.section,
        "image_name": finding.image_name,
        "block_id": finding.block_id,
        "screenshot": finding.screenshot,
        "autofixable": finding.autofixable,
        "data": dict(finding.data),
    }


def _remote_to_dict(remote: RemoteVisualResult) -> Dict[str, Any]:
    return {
        "status": remote.status,
        "findings": [_finding_to_dict(item) for item in remote.findings],
        "screenshots": list(remote.screenshots),
        "raw": dict(remote.raw),
        "error": remote.error,
    }


def _cleanup_successful_visual_runs(remote_root: str, result: VisualRepairResult) -> int:
    """Remove exported PDFs and page images only after final visual success."""

    if not remote_root or not result.passed:
        return 0
    runs_root = (Path(remote_root).expanduser().resolve() / "runs").resolve()
    candidates = set()
    for audit_round in result.rounds:
        for screenshot in audit_round.screenshots:
            try:
                parent = Path(screenshot).expanduser().resolve().parent
            except OSError:
                continue
            if parent.parent == runs_root:
                candidates.add(parent)
    removed = 0
    for directory in candidates:
        if not directory.exists() or not directory.is_dir():
            continue
        shutil.rmtree(directory)
        removed += 1
    return removed


def _parse_model_json(text: str) -> Dict[str, Any]:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.I | re.S).strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _last_json_object(stdout: str) -> Dict[str, Any]:
    text = str(stdout or "").strip()
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _positive_int(value: Any, default: int = 0) -> int:
    try:
        number = int(float(value))
        return number if number > 0 else default
    except (TypeError, ValueError):
        return default


def _safe_tag(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or ""))
    return value.strip("-")[:80] or "doc"


def _clip(value: str, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _dedupe_text(values: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values:
        text = _clip(str(value or ""), 500)
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output[-20:]


def _visual_finding_feedback(finding: VisualFinding, round_index: int) -> str:
    location = f" [section={_clip(finding.section, 80)}]" if finding.section else ""
    block = f" [block={finding.block_id}]" if finding.block_id else ""
    return f"visual round {round_index}: {finding.kind}: {_clip(finding.detail, 320)}{location}{block}"


def _is_nonblocking_visual_finding(
    finding: VisualFinding,
    remote: Optional[RemoteVisualResult] = None,
    api_warnings: Iterable[str] = (),
    max_sections: int = 0,
) -> bool:
    if finding.kind in {"table-overflow", "table-clipped", "formula-count-drift", "image-large-white-border"}:
        return True
    if finding.kind not in {"missing-formula", "missing-table", "missing-image"}:
        return False
    if not _api_verified_count(finding.kind, api_warnings):
        return False
    raw = dict((remote.raw if remote else {}) or {})
    metrics = dict(raw.get("metrics") or {})
    try:
        actual = int((finding.data or {}).get("actual") or 0)
        invalid_formula_count = int(metrics.get("invalid_formula_count") or 0)
        raw_formatting_count = int(metrics.get("raw_formatting_count") or 0)
    except (TypeError, ValueError):
        return False
    # Feishu virtualizes formulas and tables even on medium documents. The
    # authoritative API fetch has already verified the full persisted count;
    # browser DOM counts are useful only alongside an actual render-error
    # signal. Keep zero-render cases blocking, and keep invalid formulas/raw
    # Markdown as their own high-severity findings.
    if actual <= 0:
        return False
    if finding.kind == "missing-formula":
        return invalid_formula_count == 0
    if finding.kind == "missing-image":
        findings = list((remote.findings if remote else []) or [])
        return not any(item.kind == "image-render-failed" for item in findings)
    return raw_formatting_count == 0


def _api_verified_count(kind: str, warnings: Iterable[str]) -> bool:
    values = [str(item or "") for item in warnings]
    if any("fetch-failed" in item or "fetch-empty" in item for item in values):
        return False
    token = {
        "missing-formula": "missing-latex",
        "missing-table": "missing-tables",
        "missing-image": "marker-left-after-publish",
    }.get(kind, "")
    if not token:
        return False
    return not any(token in item for item in values)


def _nonblocking_finding_warning(
    finding: VisualFinding,
    remote: RemoteVisualResult,
    api_warnings: Iterable[str],
    max_sections: int,
) -> str:
    if finding.kind in {"missing-formula", "missing-table", "missing-image"} and _is_nonblocking_visual_finding(
        finding,
        remote=remote,
        api_warnings=api_warnings,
        max_sections=max_sections,
    ):
        actual = int((finding.data or {}).get("actual") or 0)
        expected = int((finding.data or {}).get("expected") or 0)
        return (
            f"visual-qa:medium:{finding.kind}-sampling-drift:"
            f"长文抽样 DOM 仅加载 {actual}/{expected}；API 全文结构计数已通过"
        )
    return _finding_warning("visual-qa", finding, severity="medium")


def _finding_warning(prefix: str, finding: VisualFinding, severity: str = "") -> str:
    location = f" [section={_clip(finding.section, 80)}]" if finding.section else ""
    screenshot = f" [screenshot={finding.screenshot}]" if finding.screenshot else ""
    return f"{prefix}:{severity or finding.severity}:{finding.kind}:{_clip(finding.detail)}{location}{screenshot}"
