from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image


INVALID_TEXTS = ("无效公式", "Invalid formula")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rendered PDF QA for a published Feishu Docx")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-sections", type=int, default=12)
    parser.add_argument("--expected-images", type=int, default=0)
    parser.add_argument("--expected-formulas", type=int, default=0)
    parser.add_argument("--expected-tables", type=int, default=0)
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=1000)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {
        "status": "ok",
        "url": args.url,
        "title": "",
        "findings": [],
        "screenshots": [],
        "sections_checked": 0,
        "metrics": {},
    }
    try:
        inventory = _fetch_docx_inventory(args.url)
        pdf_path = _export_pdf(args.url, output_dir)
        pages = _render_pages(pdf_path, output_dir)
        page_text = _extract_page_text(pdf_path)
        selected = _select_evenly(pages, max(1, int(args.max_sections)))
        findings: List[Dict[str, Any]] = report["findings"]
        for page_index, page_path in selected:
            report["screenshots"].append(str(page_path))
            report["sections_checked"] += 1
            text = page_text[page_index - 1] if page_index - 1 < len(page_text) else ""
            if any(token in text for token in INVALID_TEXTS):
                findings.append(
                    _finding(
                        "invalid-formula",
                        "high",
                        f"PDF 第 {page_index} 页出现无效公式提示",
                        section=f"第 {page_index} 页",
                        screenshot=str(page_path),
                        autofixable=True,
                    )
                )
            artifacts = _raw_formatting_artifacts(text)
            if artifacts:
                findings.append(
                    _finding(
                        "raw-formatting",
                        "high",
                        "PDF 显示了格式化控制字符：" + ", ".join(artifacts[:4]),
                        section=f"第 {page_index} 页",
                        screenshot=str(page_path),
                        autofixable=True,
                    )
                )
            if page_index < len(pages) and _page_is_blank(page_path):
                findings.append(
                    _finding(
                        "large-empty-viewport",
                        "high",
                        f"PDF 第 {page_index} 页几乎为空白，疑似异常分页或内容未渲染",
                        section=f"第 {page_index} 页",
                        screenshot=str(page_path),
                    )
                )
        report["status"] = "issues" if findings else "ok"
        report["metrics"] = {
            "transport": "feishu-pdf-export",
            "pdf_pages": len(pages),
            "pages_checked": len(selected),
            # Counts are diagnostic only. Acceptance is based on concrete
            # visible rendering failures in the exported PDF.
            "persisted_image_count": inventory["image"],
            "persisted_formula_count": inventory["formula"],
            "persisted_table_count": inventory["table"],
            "expected_image_min": max(0, args.expected_images),
            "expected_formula_min": max(0, args.expected_formulas),
            "expected_table_min": max(0, args.expected_tables),
            "pdf_size_bytes": pdf_path.stat().st_size,
        }
    except Exception as exc:
        report["status"] = "error"
        report["error"] = _clip(str(exc), 1000)

    temporary = output_dir / ".report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_dir / "report.json")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if report["status"] in {"ok", "issues"} else 2


def _fetch_docx_inventory(url: str) -> Dict[str, int]:
    lark_cli = os.environ.get("MAXREAD_LARK_CLI", "lark-cli")
    identity = os.environ.get("MAXREAD_FEISHU_AS", "bot")
    completed = subprocess.run(
        [
            lark_cli,
            "docs",
            "+fetch",
            "--doc",
            url,
            "--doc-format",
            "xml",
            "--detail",
            "simple",
            "--scope",
            "full",
            "--as",
            identity,
            "--format",
            "json",
        ],
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"post-publish XML fetch failed: {_clip(completed.stderr or completed.stdout, 800)}")
    payload = _last_json(completed.stdout)
    content = str(payload.get("data", {}).get("document", {}).get("content") or "")
    if not content:
        raise RuntimeError("post-publish XML fetch returned empty content")
    return {
        "image": len(re.findall(r"<img\b", content, flags=re.I)),
        "formula": len(re.findall(r"<latex>", content, flags=re.I)),
        "table": len(re.findall(r"<table\b", content, flags=re.I)),
    }


def _export_pdf(url: str, output_dir: Path) -> Path:
    lark_cli = os.environ.get("MAXREAD_LARK_CLI", "lark-cli")
    identity = os.environ.get("MAXREAD_FEISHU_AS", "bot")
    pdf_path = output_dir / "rendered.pdf"
    completed = subprocess.run(
        [
            lark_cli,
            "drive",
            "+export",
            "--url",
            url,
            "--file-extension",
            "pdf",
            "--file-name",
            pdf_path.name,
            "--output-dir",
            ".",
            "--overwrite",
            "--as",
            identity,
            "--format",
            "json",
        ],
        cwd=output_dir,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not pdf_path.exists():
        detail = completed.stderr.strip() or completed.stdout.strip() or f"export exited {completed.returncode}"
        raise RuntimeError(f"Feishu PDF export failed: {_clip(detail, 900)}")
    return pdf_path


def _render_pages(pdf_path: Path, output_dir: Path) -> List[Path]:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError("pdftoppm is required for PDF visual QA")
    completed = subprocess.run(
        [pdftoppm, "-png", "-r", "110", str(pdf_path), str(output_dir / "page")],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"PDF page render failed: {_clip(completed.stderr, 700)}")
    pages = sorted(output_dir.glob("page-*.png"), key=lambda path: int(re.search(r"(\d+)$", path.stem).group(1)))
    if not pages:
        raise RuntimeError("PDF export rendered zero pages")
    return pages


def _extract_page_text(pdf_path: Path) -> List[str]:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return []
    completed = subprocess.run(
        [pdftotext, "-layout", str(pdf_path), "-"],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    return completed.stdout.split("\f") if completed.returncode == 0 else []


def _select_evenly(pages: List[Path], limit: int) -> List[tuple[int, Path]]:
    indexed = list(enumerate(pages, start=1))
    if len(indexed) <= limit:
        return indexed
    if limit == 1:
        return [indexed[0]]
    positions = sorted({round(index * (len(indexed) - 1) / (limit - 1)) for index in range(limit)})
    return [indexed[position] for position in positions]


def _page_is_blank(path: Path) -> bool:
    with Image.open(path) as image:
        gray = image.convert("L")
        gray.thumbnail((700, 1000))
        ink = sum(gray.histogram()[:242])
        return ink / max(1, gray.width * gray.height) < 0.0025


def _raw_formatting_artifacts(text: str) -> List[str]:
    patterns = [
        r"\\(?:textbf|textit|textsc|mathrm|operatorname|mathbf|mathcal)(?:\b|(?=[A-Z]))",
        r"(?<!\\)\$\$",
        r"\\\(|\\\)|\\\[|\\\]",
        r"(?m)^\s*\|\s*[-:]+(?:\s*\|\s*[-:]+)+\s*\|?\s*$",
    ]
    output = []
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            output.append(match.group(0)[:50])
    return output


def _last_json(stdout: str) -> Dict[str, Any]:
    text = str(stdout or "").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
    return {}


def _finding(kind: str, severity: str, detail: str, **fields: Any) -> Dict[str, Any]:
    return {"kind": kind, "severity": severity, "detail": detail, **fields}


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
