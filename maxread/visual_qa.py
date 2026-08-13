from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol

from .render import _is_valid_latex_body, _normalize_latex_body, _strip_latex_for_text


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


_QA_LOCK = threading.BoundedSemaphore(1)
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
_RESOURCE_TAGS = {"img", "source", "whiteboard", "sheet", "bitable", "cite", "synced_reference"}
_FORMULA_RE = re.compile(r"<latex>(.*?)</latex>", flags=re.S | re.I)


class VisualQAController:
    """Run remote browser QA and apply only deterministic, bounded repairs."""

    def __init__(
        self,
        enabled: bool = False,
        host: str = "ziplab-5090",
        runner: str = "/home/lixiaolong/.local/share/maxread-browser/run_visual_qa.sh",
        remote_root: str = "/home/lixiaolong/.local/share/maxread-browser",
        timeout: int = 90,
        max_sections: int = 12,
        max_repairs: int = 2,
    ):
        self.enabled = bool(enabled)
        self.host = str(host or "ziplab-5090")
        self.runner = str(runner)
        self.remote_root = str(remote_root).rstrip("/")
        self.timeout = max(15, int(timeout or 90))
        self.max_sections = max(1, int(max_sections or 12))
        self.max_repairs = max(0, int(max_repairs or 2))

    @classmethod
    def from_settings(cls, settings) -> "VisualQAController":
        return cls(
            enabled=getattr(settings, "visual_qa_enabled", False),
            host=getattr(settings, "visual_qa_host", "ziplab-5090"),
            runner=getattr(settings, "visual_qa_runner", "/home/lixiaolong/.local/share/maxread-browser/run_visual_qa.sh"),
            remote_root=getattr(settings, "visual_qa_remote_root", "/home/lixiaolong/.local/share/maxread-browser"),
            timeout=getattr(settings, "visual_qa_timeout", 90),
            max_sections=getattr(settings, "visual_qa_max_sections", 12),
            max_repairs=getattr(settings, "visual_qa_max_repairs", 2),
        )

    def run(
        self,
        feishu: VisualFeishuClient,
        doc_url: str,
        initial_warnings: Iterable[str] = (),
        source_id: str = "",
    ) -> VisualRepairResult:
        result = VisualRepairResult()
        initial = list(initial_warnings)

        # Structural repair is useful even when the remote browser is disabled.
        structural_changed, structural_warnings, repaired = repair_structural_blocks(
            feishu, doc_url, initial, max_repairs=self.max_repairs
        )
        result.changed = structural_changed
        result.warnings.extend(structural_warnings)
        result.repaired_blocks.extend(repaired)

        if not self.enabled:
            return result

        remote = self.inspect_remote(doc_url, source_id=source_id)
        result.remote = remote
        if remote.error:
            result.warnings.append(f"visual-qa:remote-error:{_clip(remote.error)}")
            return result
        structural_findings = [
            finding for finding in remote.findings if finding.kind in {"invalid-formula", "raw-formatting"}
        ]
        if structural_findings and len(result.repaired_blocks) < self.max_repairs:
            changed, structural_warnings, repaired = repair_structural_blocks(
                feishu,
                doc_url,
                ["visual-qa:repairable-structural"],
                max_repairs=self.max_repairs - len(result.repaired_blocks),
            )
            result.warnings.extend(structural_warnings)
            if changed:
                result.changed = True
                result.repaired_blocks.extend(repaired)
                remote = self.inspect_remote(doc_url, source_id=f"{source_id}-formula-recheck")
                result.remote = remote
                if remote.error:
                    result.warnings.append(f"visual-qa:recheck-error:{_clip(remote.error)}")
                    return result
        result.warnings.extend(
            _finding_warning("visual-qa", finding)
            for finding in remote.findings
        )

        image_changed, image_warnings, image_blocks = repair_image_findings(
            feishu,
            doc_url,
            remote.findings,
            max_repairs=max(0, self.max_repairs - len(result.repaired_blocks)),
        )
        if image_changed:
            result.changed = True
            result.repaired_blocks.extend(image_blocks)
            # A second browser pass is mandatory after a visual patch. This
            # prevents reporting success based on the pre-patch screenshot.
            second = self.inspect_remote(doc_url, source_id=f"{source_id}-recheck")
            result.remote = second
            if second.error:
                result.warnings.append(f"visual-qa:recheck-error:{_clip(second.error)}")
            elif second.findings:
                result.warnings.extend(
                    _finding_warning("visual-qa:recheck", finding)
                    for finding in second.findings
                )
            else:
                result.warnings.append("visual-qa:recheck:passed")
        result.warnings.extend(image_warnings)
        return result

    def inspect_remote(self, doc_url: str, source_id: str = "") -> RemoteVisualResult:
        if not self.enabled:
            return RemoteVisualResult(status="disabled")
        tag = _safe_tag(source_id or doc_url)
        run_id = f"{tag}-{uuid.uuid4().hex[:8]}"
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
            ]
        )
        try:
            with _QA_LOCK:
                completed = subprocess.run(
                    [
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
                    ],
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
        except Exception as exc:
            return RemoteVisualResult(status="error", error=str(exc))
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"ssh exit {completed.returncode}"
            return RemoteVisualResult(status="error", error=detail)
        payload = _last_json_object(completed.stdout)
        if not payload:
            return RemoteVisualResult(status="error", error="remote runner returned no JSON")
        findings = [VisualFinding.from_dict(item) for item in payload.get("findings", []) if isinstance(item, dict)]
        return RemoteVisualResult(
            status=str(payload.get("status") or "ok"),
            findings=findings,
            screenshots=[str(item) for item in payload.get("screenshots", [])],
            raw=payload,
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
            "visual-qa:repairable-structural",
        )
    )


def _fetch_xml(feishu: VisualFeishuClient, doc_url: str) -> str:
    payload = feishu.fetch_docx(doc_url, doc_format="xml", detail="with-ids")
    document = payload.get("data", {}).get("document", {}) if isinstance(payload, dict) else {}
    return str(document.get("content") or "") if isinstance(document, dict) else ""


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


def _finding_warning(prefix: str, finding: VisualFinding) -> str:
    location = f" [section={_clip(finding.section, 80)}]" if finding.section else ""
    screenshot = f" [screenshot={finding.screenshot}]" if finding.screenshot else ""
    return f"{prefix}:{finding.severity}:{finding.kind}:{_clip(finding.detail)}{location}{screenshot}"
