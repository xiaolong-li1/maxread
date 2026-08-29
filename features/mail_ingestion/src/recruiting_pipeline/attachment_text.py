from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .pdf_text import extract_pdf_text


SUPPORTED_DOCUMENT_SUFFIXES = frozenset({".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt"})


def extract_attachment_text(path: Path, max_chars: int = 60000) -> str:
    """Extract candidate evidence from common document attachments."""

    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        return ""
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        return extract_pdf_text(path, max_chars=max_chars)
    if suffix == ".docx":
        return _extract_office_xml(path, "word/")[:max_chars]
    if suffix == ".odt":
        return _extract_office_xml(path, "content.xml")[:max_chars]
    if suffix == ".doc":
        return _extract_legacy_doc(path, max_chars)
    if suffix == ".rtf":
        return _extract_rtf(path, max_chars)
    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="replace").replace("\x00", "").strip()[:max_chars]
    return ""


def _extract_office_xml(path: Path, prefix: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name == prefix or (prefix.endswith("/") and name.startswith(prefix) and name.endswith(".xml"))
            ]
            preferred = sorted(names, key=_office_part_order)
            chunks = [_xml_text(archive.read(name)) for name in preferred]
    except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return ""
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _office_part_order(name: str) -> tuple[int, str]:
    if name.endswith("document.xml") or name == "content.xml":
        return (0, name)
    if "/header" in name or "/footer" in name:
        return (1, name)
    return (2, name)


def _xml_text(payload: bytes) -> str:
    root = ElementTree.fromstring(payload)
    lines: list[str] = []
    current: list[str] = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local in {"t", "tab"}:
            current.append("\t" if local == "tab" else str(node.text or ""))
        elif local in {"p", "tr"} and current:
            line = "".join(current).strip()
            if line:
                lines.append(line)
            current = []
    if current:
        line = "".join(current).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _extract_legacy_doc(path: Path, max_chars: int) -> str:
    antiword = shutil.which("antiword")
    if antiword:
        try:
            completed = subprocess.run(
                [antiword, "-w", "0", str(path)],
                check=False,
                capture_output=True,
                timeout=60,
            )
            text = _decode_bytes(completed.stdout)
            if text:
                return text[:max_chars]
        except (OSError, subprocess.SubprocessError):
            pass
    return _extract_with_libreoffice(path, max_chars)


def _extract_rtf(path: Path, max_chars: int) -> str:
    unrtf = shutil.which("unrtf")
    if unrtf:
        try:
            completed = subprocess.run(
                [unrtf, "--text", str(path)],
                check=False,
                capture_output=True,
                timeout=60,
            )
            text = _decode_bytes(completed.stdout)
            text = re.sub(r"^-{8,}.*?-{8,}\s*", "", text, flags=re.S)
            if text:
                return text[:max_chars]
        except (OSError, subprocess.SubprocessError):
            pass
    return _extract_with_libreoffice(path, max_chars)


def _extract_with_libreoffice(path: Path, max_chars: int) -> str:
    binary = shutil.which("libreoffice") or shutil.which("soffice")
    if not binary:
        return ""
    try:
        with tempfile.TemporaryDirectory(prefix="maxread-office-") as temp:
            completed = subprocess.run(
                [binary, "--headless", "--convert-to", "txt:Text", "--outdir", temp, str(path)],
                check=False,
                capture_output=True,
                timeout=90,
            )
            if completed.returncode != 0:
                return ""
            target = Path(temp) / f"{path.stem}.txt"
            if not target.exists():
                return ""
            return target.read_text(encoding="utf-8", errors="replace").replace("\x00", "").strip()[:max_chars]
    except (OSError, subprocess.SubprocessError):
        return ""


def _decode_bytes(payload: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return payload.decode(encoding).replace("\x00", "").strip()
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace").replace("\x00", "").strip()
