from __future__ import annotations

import re
from dataclasses import dataclass
import time
from typing import Any, Iterable, List

from .formula_compiler import compile_formula_markup


@dataclass(frozen=True)
class QualityIssue:
    agent: str
    stage: str
    severity: str
    detail: str

    def warning(self) -> str:
        return f"quality:{self.agent}:{self.stage}:{self.severity}:{self.detail}"


class PrePublishQualityError(RuntimeError):
    """The content was generated, but deterministic publication checks failed."""


class QualityAgent:
    name = "base"

    def inspect(self, markdown: str, xml: str = "") -> List[QualityIssue]:
        return []


class FormulaQualityAgent(QualityAgent):
    name = "formula"

    def inspect(self, markdown: str, xml: str = "") -> List[QualityIssue]:
        issues: List[QualityIssue] = []
        for diagnostic in compile_formula_markup(markdown or "").diagnostics:
            if diagnostic.severity == "high":
                issues.append(QualityIssue(self.name, "markdown", diagnostic.severity, diagnostic.code))
        for stage, text in (("markdown", markdown or ""), ("xml", xml or "")):
            for body in _latex_bodies(text):
                issues.extend(_inspect_latex_body(stage, body))
            if _raw_display_math(text):
                issues.append(QualityIssue(self.name, stage, "medium", "raw-dollar-display-math"))
        return _dedupe_issues(issues)


class XmlQualityAgent(QualityAgent):
    name = "xml"

    def inspect(self, markdown: str, xml: str = "") -> List[QualityIssue]:
        issues: List[QualityIssue] = []
        if not xml:
            return issues
        if "<code>" in xml and re.search(r"<code>[^<]*(?:\\[A-Za-z]+|[_^]\{|\\frac|\\sum|\\mathbb)", xml):
            issues.append(QualityIssue(self.name, "xml", "high", "latex-downgraded-to-code"))
        for marker in re.findall(r"\[MaxReadFigure:[^\]]+\]", xml):
            issues.append(QualityIssue(self.name, "xml", "medium", f"unpublished-marker:{marker}"))
        return _dedupe_issues(issues)


class FormattingQualityAgent(QualityAgent):
    name = "format"

    def inspect(self, markdown: str, xml: str = "") -> List[QualityIssue]:
        issues: List[QualityIssue] = []
        command_pattern = r"\\(?:textnormal|operatorname|textbf|textit|textsc|textrm|emph|mathcal|mathbb|mathsf|mathit|mathrm|mathbf|mathtt)"
        for stage, text in (("markdown", markdown or ""), ("xml", xml or "")):
            if any(re.search(command_pattern, segment) for segment in _unprotected_text_segments(text)):
                issues.append(QualityIssue(self.name, stage, "high", "raw-tex-formatting-command"))
            if stage == "markdown" and re.search(r"</?p(?:\s[^>]*)?>", text, flags=re.I):
                issues.append(QualityIssue(self.name, stage, "high", "raw-html-paragraph-tag"))
            if stage == "xml" and re.search(r"&lt;/?p(?:\s[^&]*)&gt;", text, flags=re.I):
                issues.append(QualityIssue(self.name, stage, "high", "escaped-html-paragraph-tag"))
            if stage == "xml" and re.search(
                r"<p(?:\s[^>]*)?>.*?<br\s*/?>\s*#{2,6}\s+",
                text,
                flags=re.S | re.I,
            ):
                issues.append(QualityIssue(self.name, stage, "high", "markdown-heading-inside-paragraph"))
            if stage == "xml" and re.search(
                r"<p(?:\s[^>]*)?>.*?<br\s*/?>\s*\|[^\n<]*\|\s*<br\s*/?>\s*\|[\s:|\-]+\|",
                text,
                flags=re.S | re.I,
            ):
                issues.append(QualityIssue(self.name, stage, "high", "markdown-table-inside-paragraph"))
            if stage == "xml" and re.search(
                r"<p(?:\s[^>]*)?>.*?<br\s*/?>\s*\[MaxReadFigure:[^\]]+\]",
                text,
                flags=re.S | re.I,
            ):
                issues.append(QualityIssue(self.name, stage, "high", "figure-marker-inside-paragraph"))
            if _has_raw_table_uncertainty(text, stage):
                issues.append(QualityIssue(self.name, stage, "high", "raw-table-math"))
            if stage == "markdown" and _has_long_english_prose_block(text):
                issues.append(QualityIssue(self.name, stage, "high", "long-english-prose"))
        return _dedupe_issues(issues)


class FigureQualityAgent(QualityAgent):
    name = "figure"

    def inspect(self, markdown: str, xml: str = "") -> List[QualityIssue]:
        issues: List[QualityIssue] = []
        if "图表补充" in (markdown or ""):
            issues.append(QualityIssue(self.name, "markdown", "medium", "figure-supplement-section"))
        if re.search(r"(logo|brand|icon).{0,60}\[MaxReadFigure:", markdown or "", flags=re.I):
            issues.append(QualityIssue(self.name, "markdown", "medium", "possible-logo-figure"))
        return issues


class TextQualityAgent(QualityAgent):
    name = "text"

    def inspect(self, markdown: str, xml: str = "") -> List[QualityIssue]:
        issues: List[QualityIssue] = []
        for stage, text in (("markdown", markdown or ""), ("xml", xml or "")):
            if re.search(r"(?<!\?)\?\?(?!\?)", text):
                issues.append(QualityIssue(self.name, stage, "medium", "unresolved-question-placeholder"))
            if "[TRUNCATED]" in text:
                issues.append(QualityIssue(self.name, stage, "medium", "source-truncated-marker"))
            if _has_dangling_english_fragment(text):
                issues.append(QualityIssue(self.name, stage, "medium", "possible-truncated-english-tail"))
        return _dedupe_issues(issues)


QUALITY_AGENTS: tuple[QualityAgent, ...] = (
    FormulaQualityAgent(),
    XmlQualityAgent(),
    FormattingQualityAgent(),
    FigureQualityAgent(),
    TextQualityAgent(),
)


def inspect_document_quality(markdown: str, xml: str = "", agents: Iterable[QualityAgent] = QUALITY_AGENTS) -> List[QualityIssue]:
    issues: List[QualityIssue] = []
    for agent in agents:
        issues.extend(agent.inspect(markdown, xml))
    return _dedupe_issues(issues)


def quality_warnings(markdown: str, xml: str = "") -> List[str]:
    return [issue.warning() for issue in inspect_document_quality(markdown, xml)]


def pre_publish_quality_warnings(markdown: str, xml: str = "") -> List[str]:
    return [
        warning
        for warning in quality_warnings(markdown, xml)
        if ":unpublished-marker:" not in warning
    ]


def blocking_quality_warnings(warnings: Iterable[str]) -> List[str]:
    blocking: List[str] = []
    for warning in warnings:
        quality_warning = warning.removeprefix("post-publish:")
        if quality_warning.startswith("quality:") and ":high:" in quality_warning:
            blocking.append(warning)
            continue
        if _is_table_geometry_warning(warning):
            continue
        if warning.startswith("visual-qa:high:") or warning.startswith("visual-qa:recheck:high:"):
            blocking.append(warning)
            continue
        if warning.startswith(("visual-qa:remote-error:", "visual-qa:recheck-error:")):
            blocking.append(warning)
            continue
        if warning.startswith("visual-qa:infrastructure:"):
            blocking.append(warning)
            continue
        if warning.startswith((
            "image-anchor-lookup-failed:",
            "image-anchor-missing:",
            "image-insert-failed:",
            "image-block-id-missing:",
            "image-anchor-refresh-failed:",
            "image-move-failed:",
            "image-marker-remove-failed:",
            "post-publish:marker-left-after-publish",
        )):
            blocking.append(warning)
    return blocking


def _is_table_geometry_warning(warning: str) -> bool:
    value = str(warning or "").removeprefix("post-publish:")
    return value.startswith((
        "visual-qa:high:table-overflow:",
        "visual-qa:recheck:high:table-overflow:",
        "visual-qa:high:table-clipped:",
        "visual-qa:recheck:high:table-clipped:",
    ))


_RAW_TABLE_UNCERTAINTY_PATTERN = re.compile(
    r"(?<![\w.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:"
    r"\^\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}\s*_\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}"
    r"|_\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}\s*\^\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}"
    r")(?![\w.])"
)


def _has_raw_table_uncertainty(text: str, stage: str) -> bool:
    protected = re.sub(r"<latex>.*?</latex>|<code>.*?</code>", " ", str(text or ""), flags=re.S | re.I)
    if stage == "xml":
        return any(_RAW_TABLE_UNCERTAINTY_PATTERN.search(block) for block in re.findall(r"<table\b.*?</table>", protected, re.S | re.I))
    return any("|" in line and _RAW_TABLE_UNCERTAINTY_PATTERN.search(line) for line in protected.splitlines())


def _has_long_english_prose_block(markdown: str) -> bool:
    for block in re.split(r"\n{2,}", str(markdown or "")):
        for candidate in block.splitlines() or [block]:
            value = candidate.strip()
            if not value or value.startswith(("#", "```", "[MaxReadFigure:")):
                continue
            if "|" in value or "英文标题" in value or re.search(r"(?i)(?:原文|repository|github|arxiv)\s*[：:]", value):
                continue
            plain = re.sub(r"<latex>.*?</latex>|`[^`]*`|https?://\S+", " ", value, flags=re.S | re.I)
            words = re.findall(r"\b[A-Za-z][A-Za-z0-9+./-]*\b", plain)
            letters = len(re.findall(r"[A-Za-z]", plain))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", plain))
            if len(words) >= 16 and letters >= 90 and cjk < 12:
                return True
    return False


def paper_markdown_completeness_errors(markdown: str, expected_markers: Iterable[str] = ()) -> List[str]:
    text = str(markdown or "")
    errors: List[str] = []
    if len(text.strip()) < 1800:
        errors.append("document-too-short")
    first_line = _first_nonempty_markdown_line(text)
    if not re.match(r"^#\s+\S", first_line):
        errors.append("missing-h1")
    if first_line.startswith("```"):
        errors.append("leading-code-fence")
    if re.search(
        r"(?i)(?:the user wants me to|let me carefully follow(?: all)? the rules|i(?:'|’)m instructed to generate)",
        text,
    ):
        errors.append("prompt-leak")
    h1_lines = re.findall(r"(?m)^\s*#\s+\S", text)
    if len(h1_lines) > 1 or len(re.findall(r"#\s+\[[^\]\n]+\]", text)) > 1:
        errors.append("duplicate-h1")
    if "TL;DR" not in text:
        errors.append("missing-tldr")
    for number in range(1, 8):
        if not re.search(rf"(?m)^##\s+{number}(?:[.、]|\s)", text):
            errors.append(f"missing-section-{number}")
    markers = list(expected_markers)
    required = min(3, len(markers))
    present = sum(1 for marker in markers if marker in text)
    if present < required:
        errors.append(f"too-few-figures:{present}/{required}")
    return errors


def _first_nonempty_markdown_line(markdown: str) -> str:
    for line in str(markdown or "").lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def verify_published_docx(
    feishu,
    doc_url: str,
    expected_title: str = "",
    required_terms: Iterable[str] = (),
    expected_image_min: int = 0,
    expected_latex_min: int = 0,
    expected_table_min: int = 0,
    attempts: int = 2,
    retry_delay: float = 1.0,
) -> List[str]:
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            payload = feishu.fetch_docx(doc_url, doc_format="xml", detail="simple")
            content = _fetch_content(payload)
            warnings = validate_fetched_docx_content(
                content,
                expected_title=expected_title,
                required_terms=required_terms,
                expected_image_min=expected_image_min,
                expected_latex_min=expected_latex_min,
                expected_table_min=expected_table_min,
            )
            warnings.extend(quality_warnings("", content))
            warnings = _dedupe_strings(warnings)
            if attempt + 1 < max(1, attempts) and _retryable_roundtrip_warnings(warnings):
                time.sleep(max(0.0, float(retry_delay)))
                continue
            return [f"post-publish:{warning}" for warning in warnings]
        except Exception as exc:
            last_error = str(exc)
            if attempt + 1 < max(1, attempts):
                time.sleep(1.0)
    return [f"post-publish:fetch-failed:{_clip(last_error, 240)}"]


def _retryable_roundtrip_warnings(warnings: Iterable[str]) -> bool:
    markers = (
        "html-tag-in-formula",
        "nested-latex-tag",
        "missing-latex:",
        "missing-tables:",
        "marker-left-after-publish",
    )
    return any(any(marker in str(warning) for marker in markers) for warning in warnings)


def validate_fetched_docx_content(
    content: str,
    expected_title: str = "",
    required_terms: Iterable[str] = (),
    expected_image_min: int = 0,
    expected_latex_min: int = 0,
    expected_table_min: int = 0,
) -> List[str]:
    warnings: List[str] = []
    text = str(content or "")
    if not text.strip():
        warnings.append("fetch-empty")
        return warnings
    # The generated document title is localized and intentionally differs from
    # arXiv's English metadata title. Validate the title block itself instead of
    # comparing unrelated display strings.
    title_match = re.search(r"<title(?:\s[^>]*)?>(.*?)</title>", text, flags=re.S | re.I)
    title_text = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""
    if expected_title and not title_text:
        warnings.append("missing-title")
    for term in required_terms:
        term = str(term or "").strip()
        if term and term not in text:
            warnings.append(f"missing-term:{_clip(term, 80)}")
    if re.findall(r"\[MaxReadFigure:[^\]]+\]", text):
        warnings.append("marker-left-after-publish")
    # docs +fetch omits media resource blocks from document.content, even when
    # images are present. Residual MaxReadFigure markers are the reliable signal
    # that image publication failed; counting <img> here produces false alarms.
    latex_count = len(re.findall(r"<latex>", text))
    if expected_latex_min and latex_count < expected_latex_min:
        warnings.append(f"missing-latex:{latex_count}/{expected_latex_min}")
    table_count = len(re.findall(r"<table(?:\s|>)", text, flags=re.I))
    if expected_table_min and table_count < expected_table_min:
        warnings.append(f"missing-tables:{table_count}/{expected_table_min}")
    return _dedupe_strings(warnings)


def _latex_bodies(text: str) -> List[str]:
    source = str(text or "")
    bodies = re.findall(r"<latex>(.*?)</latex>", source, flags=re.S)
    # Inspect raw math only as a fallback. A loose dollar-pair regex treats
    # currency such as "$19,627.77" as one huge formula spanning prose and
    # XML tags, which creates false CJK/HTML-in-formula failures.
    raw = re.sub(r"<latex>.*?</latex>", " ", source, flags=re.S)
    raw = re.sub(
        r"(?<!\\)\$(?=\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:USD|CNY|RMB|美元|元))?(?=\s*(?:[^\d\w]|$)))",
        "",
        raw,
        flags=re.I,
    )
    bodies.extend(re.findall(r"\$\$(?!\$)([^$\n].{0,1200}?)\$\$(?!\$)", raw, flags=re.S))
    bodies.extend(re.findall(r"(?<!\$)\$([^$\n]{1,240}?)\$(?!\$)", raw))
    return bodies


def _has_dangling_english_fragment(text: str) -> bool:
    clean = re.sub(r"<[^>]+>", "\n", text or "")
    clean = re.sub(r"[*_`#>\-]+", " ", clean)
    suspicious = {
        "comput",
        "eval",
        "gener",
        "gen",
        "informat",
        "meas",
        "measu",
        "oper",
        "pred",
        "represent",
        "rep",
        "verbal",
    }
    for line in re.split(r"[\n。！？；.!?;]", clean):
        line = line.strip()
        if not line or not re.search(r"[\u4e00-\u9fff]", line):
            continue
        match = re.search(r"\b([A-Za-z]{3,12})\s*$", line)
        if match and match.group(1).lower() in suspicious:
            return True
    return False


def _fetch_content(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    document = payload.get("data", {}).get("document", {})
    if isinstance(document, dict):
        return str(document.get("content") or document.get("markdown") or document.get("text") or "")
    return str(payload.get("content") or "")


def _inspect_latex_body(stage: str, body: str) -> List[QualityIssue]:
    text = str(body or "")
    issues: List[QualityIssue] = []
    unsupported_patterns = [
        # \bmod and \bmatrix share the \bm prefix but are standard LaTeX
        # commands. Keep flagging the paper macro forms \bmX and \bm{...}.
        (r"\\bm(?!athbf\b|od\b|atrix\b)", "unsupported-bm-macro"),
        (r"\\(?:rv|erv|erm)[A-Za-z]+|\\et[A-Z][A-Za-z]*|\\t[A-Z][A-Za-z]+|\\rm(?!athrm\b)[A-Za-z]+", "unsupported-paper-macro"),
        (r"\\(?:tens|etens|mathsfit)\s*\{", "unsupported-tensor-macro"),
        (r"\\(?:matrix|vector|tr)\s*\{", "unsupported-position-macro"),
        (r"\\(?:le\s+ft|ri\s+ght|in\s+fty|ap\s+prox)\b|\^\\to\s+p\b", "split-latex-command"),
        (r"\\(?:qquad|quad)[A-Za-z]", "joined-spacing-command"),
        (r"(?<!\\)\\(?:\[|\])", "internal-display-delimiter"),
        (r"\\(?:overline|underline|hat|widehat|tilde|widetilde|bar|vec|dot|ddot|check|breve|acute|grave)(?:\s+[A-Za-z]|[A-Z])", "fused-accent-command"),
        (r"<\s*/?\s*(?:br|p|div)\b[^<>]*>", "html-tag-in-formula"),
        (r"<\s*/?\s*latex\b[^<>]*>", "nested-latex-tag"),
        (r"\\mbox\s*\{", "mbox-command-in-formula"),
        (r"\\boldsymbol\s*\{", "boldsymbol-command-in-formula"),
        (r"\\(?:text|textnormal|textbf|textit|textsc|textrm|emph)\s*\{", "text-command-in-formula"),
        (r"\\operatorname\s*\{", "operatorname-command-in-formula"),
        (r"\\(?:label|ref|eqref|tag)\s*\{", "publish-metadata-command"),
        (r"\\(?:def|newcommand|renewcommand|usepackage|RequirePackage)\b", "latex-definition-command"),
        (r"\\begin\s*\{(?!aligned|align|array|matrix|pmatrix|bmatrix|cases)", "unsupported-begin-env"),
        (r"[\u4e00-\u9fff]", "cjk-inside-formula"),
    ]
    for pattern, label in unsupported_patterns:
        if re.search(pattern, text):
            issues.append(QualityIssue("formula", stage, "high", label))
    if _latex_command_count(text, "left") != _latex_command_count(text, "right"):
        issues.append(QualityIssue("formula", stage, "high", "unbalanced-left-right"))
    if not _balanced_latex_braces(text):
        issues.append(QualityIssue("formula", stage, "high", "unbalanced-braces"))
    return issues


def _unprotected_text_segments(text: str) -> List[str]:
    parts = re.split(r"(<latex>.*?</latex>|<code>.*?</code>)", text or "", flags=re.S)
    return parts[0::2]


def _latex_command_count(text: str, command: str) -> int:
    return len(re.findall(rf"\\{re.escape(command)}(?![A-Za-z])", text or ""))


def _raw_display_math(text: str) -> bool:
    return bool(re.search(r"(?<!\\)\$\$|(?<!\\)\\\[|(?<!\\)\\\]", text or ""))


def _balanced_latex_braces(text: str) -> bool:
    depth = 0
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _dedupe_issues(issues: Iterable[QualityIssue]) -> List[QualityIssue]:
    seen = set()
    out: List[QualityIssue] = []
    for issue in issues:
        key = (issue.agent, issue.stage, issue.severity, issue.detail)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def _dedupe_strings(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."
