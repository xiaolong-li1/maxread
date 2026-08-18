"""A small, deterministic compiler front-end for document formula markup.

This is intentionally a bounded language rather than a TeX implementation.  It
owns the boundary between Markdown/HTML presentation markup and Feishu's
``<latex>...</latex>`` formula nodes.  The existing LaTeX normalizer remains
responsible for supported commands inside a formula.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from enum import Enum
from typing import List


class FormulaTokenKind(str, Enum):
    TEXT = "text"
    FORMULA = "formula"


@dataclass(frozen=True)
class FormulaToken:
    kind: FormulaTokenKind
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class FormulaDiagnostic:
    code: str
    severity: str
    message: str
    start: int
    end: int


@dataclass(frozen=True)
class FormulaCompilation:
    text: str
    tokens: List[FormulaToken]
    diagnostics: List[FormulaDiagnostic]


_LATEX_BLOCK_RE = re.compile(r"<latex\b[^>]*>(.*?)</latex\s*>", re.I | re.S)
_KNOWN_WRAPPER_RE = re.compile(
    r"<\s*/?\s*(?:p|div|br)\b[^>]*>",
    re.I,
)
_ANY_HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^<>]*>")
_ENTITY_WRAPPER_RE = re.compile(
    r"(?is)(?:&lt;|&#0*60;|&#x0*3c;)\s*"
    r"(/?\s*(?:p|div|br)\b[^<>]*)"
    r"(?:&gt;|&#0*62;|&#x0*3e;)",
)
_FORMULA_WRAPPER_RE = re.compile(
    r"<\s*p\b[^>]*>\s*<latex>.*?</latex>\s*</\s*p\s*>",
    re.I | re.S,
)


def compile_formula_markup(markdown: str) -> FormulaCompilation:
    """Normalize formula boundaries and return compiler-style diagnostics.

    Accepted presentation syntax is deliberately small:

    * ``<p><latex>...</latex></p>`` and escaped equivalents are wrappers;
    * ``<br/>`` inside a formula becomes a space;
    * paragraph/div wrappers outside formulas are removed, since the renderer
      creates the document paragraphs itself.

    Unknown HTML in a formula is retained and diagnosed instead of silently
    corrupting the mathematical source.
    """

    source = str(markdown or "")
    source = _decode_wrapper_entities(source)
    diagnostics: List[FormulaDiagnostic] = []
    if _FORMULA_WRAPPER_RE.search(source):
        diagnostics.append(
            FormulaDiagnostic(
                code="recovered-formula-wrapper",
                severity="info",
                message="removed an HTML paragraph wrapper around a formula",
                start=0,
                end=len(source),
            )
        )
    pieces: List[str] = []
    tokens: List[FormulaToken] = []
    cursor = 0

    for match in _LATEX_BLOCK_RE.finditer(source):
        prose = source[cursor:match.start()]
        cleaned_prose = _clean_prose_markup(prose)
        if cleaned_prose:
            pieces.append(cleaned_prose)
            tokens.append(FormulaToken(FormulaTokenKind.TEXT, cleaned_prose, cursor, match.start()))

        body = match.group(1)
        cleaned_body, body_diagnostics = _compile_formula_body(body, match.start(1))
        diagnostics.extend(body_diagnostics)
        formula = f"<latex>{cleaned_body}</latex>"
        pieces.append(formula)
        tokens.append(FormulaToken(FormulaTokenKind.FORMULA, cleaned_body, match.start(), match.end()))
        cursor = match.end()

    tail = _clean_prose_markup(source[cursor:])
    if tail:
        pieces.append(tail)
        tokens.append(FormulaToken(FormulaTokenKind.TEXT, tail, cursor, len(source)))

    text = "".join(pieces)
    text, wrapper_diagnostics = _unwrap_formula_paragraphs(text, source)
    diagnostics.extend(wrapper_diagnostics)
    return FormulaCompilation(text=text, tokens=tokens, diagnostics=diagnostics)


def _decode_wrapper_entities(text: str) -> str:
    def decode(match: re.Match[str]) -> str:
        candidate = html.unescape(match.group(0))
        return candidate if re.fullmatch(r"<\s*/?\s*(?:p|div|br)\b[^>]*>", candidate, re.I) else match.group(0)

    return _ENTITY_WRAPPER_RE.sub(decode, text)


def _clean_prose_markup(text: str) -> str:
    text = re.sub(r"<\s*/?\s*(?:p|div)\b[^>]*>", "", text, flags=re.I)
    # Leave breaks untouched until raw $...$ / \\(...\\) math has been
    # tokenized by the existing normalizer. Replacing them here can split a
    # formula before the formula lexer sees it.
    return text


def _compile_formula_body(body: str, offset: int) -> tuple[str, List[FormulaDiagnostic]]:
    diagnostics: List[FormulaDiagnostic] = []
    value = str(body or "")
    for tag in _ANY_HTML_TAG_RE.finditer(value):
        tag_text = tag.group(0)
        if _KNOWN_WRAPPER_RE.fullmatch(tag_text):
            continue
        diagnostics.append(
            FormulaDiagnostic(
                code="unknown-html-in-formula",
                severity="high",
                message=f"unsupported HTML tag in formula: {tag_text}",
                start=offset + tag.start(),
                end=offset + tag.end(),
            )
        )
    value = _KNOWN_WRAPPER_RE.sub(lambda match: " " if re.match(r"<\s*br", match.group(0), re.I) else "", value)
    value = re.sub(r"<\s*/?\s*latex\b[^>]*>", "", value, flags=re.I)
    if not _balanced_braces(value):
        diagnostics.append(
            FormulaDiagnostic(
                code="unbalanced-braces",
                severity="high",
                message="formula braces are not balanced",
                start=offset,
                end=offset + len(body),
            )
        )
    return value, diagnostics


def _unwrap_formula_paragraphs(text: str, source: str) -> tuple[str, List[FormulaDiagnostic]]:
    diagnostics: List[FormulaDiagnostic] = []
    pattern = re.compile(
        r"<\s*p\b[^>]*>\s*(<latex>.*?</latex>)\s*</\s*p\s*>",
        re.I | re.S,
    )
    previous = None
    while previous != text:
        previous = text
        text, count = pattern.subn(r"\1", text)
        if count:
            diagnostics.append(
                FormulaDiagnostic(
                    code="recovered-formula-wrapper",
                    severity="info",
                    message="removed an HTML paragraph wrapper around a formula",
                    start=0,
                    end=len(source),
                )
            )
    return text, diagnostics


def _balanced_braces(text: str) -> bool:
    depth = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0
