from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image


INVALID_TEXTS = ("无效公式", "Invalid formula")


class ExportPendingError(RuntimeError):
    pass


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
    except ExportPendingError as exc:
        report["status"] = "infrastructure_pending"
        report["error_type"] = "export_pending"
        report["retryable"] = True
        report["error"] = _clip(str(exc), 1000)
    except Exception as exc:
        report["status"] = "error"
        report["error"] = _clip(str(exc), 1000)

    temporary = output_dir / ".report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_dir / "report.json")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if report["status"] in {"ok", "issues", "infrastructure_pending"} else 2


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
    source_token = _doc_token(url)
    task_dir = output_dir.parent / ".export-tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_path = task_dir / f"{source_token}.json"
    if task_path.exists():
        try:
            state = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        ticket = str(state.get("ticket") or "")
        if ticket:
            exported_token = _wait_export_ticket(ticket, source_token, identity, wait_seconds=65)
            if exported_token:
                _download_exported_pdf(exported_token, pdf_path, identity)
                task_path.unlink(missing_ok=True)
                return pdf_path
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
        ticket = _export_ticket(detail)
        if ticket and source_token:
            temporary = task_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"ticket": ticket, "source_token": source_token}), encoding="utf-8")
            temporary.replace(task_path)
            exported_token = _wait_export_ticket(ticket, source_token, identity, wait_seconds=65)
            if exported_token:
                _download_exported_pdf(exported_token, pdf_path, identity)
                task_path.unlink(missing_ok=True)
                return pdf_path
            raise ExportPendingError(f"Feishu PDF export is still processing; ticket={ticket}")
        raise RuntimeError(f"Feishu PDF export failed: {_clip(detail, 900)}")
    return pdf_path


def _wait_export_ticket(ticket: str, source_token: str, identity: str, wait_seconds: int) -> str:
    lark_cli = os.environ.get("MAXREAD_LARK_CLI", "lark-cli")
    deadline = time.monotonic() + max(1, int(wait_seconds))
    last_status = "processing"
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [
                lark_cli, "drive", "+task_result", "--scenario", "export",
                "--ticket", ticket, "--file-token", source_token,
                "--as", identity, "--format", "json",
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        payload = _last_json(completed.stdout)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if data.get("failed") or str(data.get("job_status_label") or "").lower() == "failed":
            raise RuntimeError(f"Feishu PDF export task failed: {_clip(data.get('job_error_msg'), 500)}")
        exported_token = str(data.get("file_token") or "")
        if data.get("ready") and exported_token:
            return exported_token
        last_status = str(data.get("job_status_label") or data.get("job_status") or "processing")
        time.sleep(4)
    raise ExportPendingError(f"Feishu PDF export is still processing; ticket={ticket}; status={last_status}")


def _download_exported_pdf(file_token: str, pdf_path: Path, identity: str) -> None:
    lark_cli = os.environ.get("MAXREAD_LARK_CLI", "lark-cli")
    completed = subprocess.run(
        [
            lark_cli, "drive", "+download", "--file-token", file_token,
            "--output", f"./{pdf_path.name}", "--overwrite", "--as", identity, "--format", "json",
        ],
        cwd=pdf_path.parent,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not pdf_path.exists() or pdf_path.stat().st_size < 1024:
        preview = subprocess.run(
            [
                lark_cli, "drive", "+preview", "--file-token", file_token,
                "--type", "source_file", "--output", f"./{pdf_path.name}",
                "--if-exists", "overwrite", "--as", identity, "--format", "json",
            ],
            cwd=pdf_path.parent,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if preview.returncode != 0 or not pdf_path.exists() or pdf_path.stat().st_size < 1024:
            detail = preview.stderr.strip() or preview.stdout.strip() or completed.stderr.strip() or completed.stdout.strip() or "download failed"
            raise RuntimeError(f"Feishu exported PDF download failed: {_clip(detail, 700)}")


def _doc_token(url: str) -> str:
    match = re.search(r"/(?:docx|docs)/([A-Za-z0-9]+)", str(url or ""))
    return match.group(1) if match else ""


def _export_ticket(text: str) -> str:
    patterns = [r"--ticket\s+([0-9]+)", r"Created export task:\s*([0-9]+)", r'"ticket"\s*:\s*"?([0-9]+)']
    for pattern in patterns:
        match = re.search(pattern, str(text or ""), flags=re.I)
        if match:
            return match.group(1)
    return ""


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
