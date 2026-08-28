from __future__ import annotations

import subprocess
from pathlib import Path


def extract_pdf_text(path: Path, max_pages: int = 64, max_chars: int = 60000) -> str:
    """Extract text without mutating the source PDF.

    A scanned/image-only PDF returns an empty string; the caller can then route
    it to the configured vision fallback or retain an explicit unknown value.
    """
    if not path.exists() or path.stat().st_size == 0:
        return ""
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", str(max_pages), "-layout", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    text = result.stdout.replace("\x00", "").strip() if result is not None else ""
    if text:
        return text[:max_chars]

    # 5090 does not necessarily have the poppler `pdftotext` binary, while
    # its bundled Python environment provides PyMuPDF (`fitz`).  Falling back
    # here ensures the downloaded PDF bytes actually reach the AI extractor.
    try:
        import fitz  # type: ignore

        document = fitz.open(str(path))
        try:
            chunks = [document[index].get_text("text") for index in range(min(max_pages, len(document)))]
        finally:
            document.close()
        return "\n".join(chunks).replace("\x00", "").strip()[:max_chars]
    except (ImportError, OSError, RuntimeError, ValueError):
        return ""
