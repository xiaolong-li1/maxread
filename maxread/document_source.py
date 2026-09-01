from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

from .arxiv import ArxivClient
from .models import ArxivMetadata, PaperBundle, PaperFigure, PaperRef
from .render import constrain_rendered_image
from .sources import DOCUMENT_SOURCE_HOSTS, canonical_document_url
from .text_safety import sanitize_unicode_text


ARXIV_LINK_RE = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
PDF_CAPTION_RE = re.compile(r"^\s*(?:figure|fig\.)\s*(?P<number>\d+[a-z]?)\s*[.:\-]?\s*(?P<body>.*)", re.I | re.S)
TABLE_CAPTION_RE = re.compile(r"^\s*table\s+(?P<number>\d+[a-z]?)\s*[.:\-]?\s*(?P<body>.*)", re.I | re.S)
MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc=['\"](?P<url>[^'\"]+)['\"][^>]*>", re.I)
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")


class DocumentSourceClient:
    """Resolve approved PDF/repository inputs into a standard paper bundle."""

    def __init__(
        self,
        workdir: Path,
        *,
        arxiv: Optional[ArxivClient] = None,
        timeout: int = 60,
        max_download_bytes: int = 200 * 1024 * 1024,
    ):
        self.workdir = Path(workdir)
        self.arxiv = arxiv
        self.timeout = max(10, int(timeout))
        self.max_download_bytes = max(1024 * 1024, int(max_download_bytes))
        configured = {
            host.strip().lower()
            for host in os.environ.get("MAXREAD_DOCUMENT_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        }
        self.allowed_hosts = DOCUMENT_SOURCE_HOSTS | configured
        proxy_url = (
            os.environ.get("MAXREAD_DOCUMENT_PROXY_URL", "").strip()
            or os.environ.get("MAXREAD_ARXIV_PROXY_URL", "").strip()
        )
        handlers = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        self.opener = urllib.request.build_opener(*handlers)

    def fetch(self, ref: PaperRef) -> PaperBundle:
        url = canonical_document_url(ref.url)
        if _huggingface_model_repo(url):
            return self._fetch_huggingface_model(ref, url)
        return self._fetch_pdf(ref, url)

    def _fetch_huggingface_model(self, ref: PaperRef, url: str) -> PaperBundle:
        owner, repo = _huggingface_repo_parts(url)
        paper_dir = self.workdir / "papers" / ref.paper_id
        source_dir = paper_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        siblings: list[str] = []
        try:
            payload = json.loads(self._get(f"https://huggingface.co/api/models/{owner}/{repo}").decode("utf-8"))
            siblings = [str(item.get("rfilename") or "") for item in payload.get("siblings", []) if isinstance(item, dict)]
        except Exception as exc:
            warnings.append(f"Hugging Face metadata unavailable; continued with model card: {exc}")
        readme_url = f"https://huggingface.co/{owner}/{repo}/resolve/main/README.md"
        readme = sanitize_unicode_text(self._get(readme_url).decode("utf-8", errors="replace"))[0]
        source_path = paper_dir / "README.md"
        paper_dir.mkdir(parents=True, exist_ok=True)
        source_path.write_text(readme, encoding="utf-8")

        report = _preferred_pdf(siblings, readme)
        if report:
            report_url = report if report.startswith(("http://", "https://")) else (
                f"https://huggingface.co/{owner}/{repo}/resolve/main/{urllib.parse.quote(report, safe='/')}"
            )
            try:
                return self._fetch_pdf(ref, report_url, inherited_warnings=warnings)
            except Exception as exc:
                warnings.append(f"Technical-report PDF failed; used official model card: {exc}")

        arxiv_id = _arxiv_id(readme)
        upgraded = self._try_arxiv_upgrade(arxiv_id, warnings)
        if upgraded:
            return upgraded

        figures = self._download_markdown_images(readme, readme_url, source_dir, warnings)
        metadata = ArxivMetadata(
            paper_id=ref.paper_id,
            title=_markdown_title(readme) or repo,
            authors=[owner],
            summary=_markdown_summary(readme),
            published="",
            updated="",
            categories=["Hugging Face model card"],
            pdf_url="",
            abs_url=url,
            source_kind="document",
            source_label=f"Hugging Face {owner}/{repo}",
        )
        return PaperBundle(
            metadata=metadata,
            pdf_path=None,
            source_path=source_path,
            source_dir=source_dir,
            source_text=readme,
            pdf_text="",
            source_tree="README.md\n" + "\n".join(siblings[:200]),
            source_assets=[figure.asset for figure in figures],
            source_captions=[figure.caption for figure in figures],
            source_figures=figures,
            source_tables=_markdown_tables(readme),
            parse_warnings=warnings + ["Official Hugging Face model card used as the technical evidence source"],
        )

    def _fetch_pdf(
        self,
        ref: PaperRef,
        url: str,
        *,
        inherited_warnings: Optional[list[str]] = None,
    ) -> PaperBundle:
        paper_dir = self.workdir / "papers" / ref.paper_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = paper_dir / "document.pdf"
        self._download(url, pdf_path)
        warnings = list(inherited_warnings or [])
        # Only the title page may identify this PDF. Searching the whole report
        # can accidentally upgrade to a cited paper from the references.
        arxiv_id = _arxiv_id(_pdf_plain_text(pdf_path, max_pages=1))
        upgraded = self._try_arxiv_upgrade(arxiv_id, warnings)
        if upgraded:
            if upgraded.pdf_path is None:
                upgraded.pdf_path = pdf_path
            upgraded.parse_warnings.insert(0, f"Resolved PDF input to arXiv {arxiv_id}; upgraded to TeX-first pipeline")
            return upgraded
        return _pdf_bundle(ref, url, pdf_path, warnings)

    def _try_arxiv_upgrade(self, paper_id: str, warnings: list[str]) -> Optional[PaperBundle]:
        if not paper_id or not self.arxiv:
            return None
        try:
            bundle = self.arxiv.fetch(paper_id)
        except Exception as exc:
            warnings.append(f"arXiv upgrade failed; kept original document source: {exc}")
            return None
        if not str(bundle.source_text or "").strip():
            warnings.append(f"arXiv {paper_id} detected but TeX source was unavailable; kept original document source")
            return None
        return bundle

    def _download_markdown_images(
        self,
        markdown: str,
        readme_url: str,
        source_dir: Path,
        warnings: list[str],
    ) -> list[PaperFigure]:
        figures: list[PaperFigure] = []
        current_section = ""
        for line in markdown.splitlines():
            heading = HEADING_RE.match(line.strip())
            if heading:
                current_section = heading.group("title").strip()
            items = [(match.group("url"), match.group("alt").strip()) for match in MARKDOWN_IMAGE_RE.finditer(line)]
            items.extend((match.group("url"), "") for match in HTML_IMAGE_RE.finditer(line))
            for raw_url, alt in items:
                if len(figures) >= 48:
                    warnings.append("Model card image limit reached; remaining decorative images were skipped")
                    return figures
                resolved = _resolve_markdown_asset(readme_url, raw_url)
                if not _looks_like_image_url(resolved):
                    continue
                suffix = Path(urllib.parse.urlparse(resolved).path).suffix.lower()
                local_path = source_dir / f"model-card-{len(figures) + 1:03d}{suffix}"
                try:
                    self._download(resolved, local_path, require_pdf=False)
                    constrain_rendered_image(local_path)
                except Exception as exc:
                    warnings.append(f"Model card image skipped ({resolved}): {exc}")
                    continue
                figures.append(PaperFigure(
                    asset=local_path.relative_to(source_dir).as_posix(),
                    caption=alt or f"Model card figure {len(figures) + 1}",
                    label=f"model-card-{len(figures) + 1}",
                    figure_index=len(figures) + 1,
                    owner_section=current_section,
                    owner_evidence="model-card-heading",
                ))
        return figures

    def _get(self, url: str) -> bytes:
        self._validate_url(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": "MaxRead/0.1 (+https://github.com/xiaolong-li1/maxread)",
            "Accept": "application/json,text/markdown,text/plain,*/*",
        })
        with self.opener.open(request, timeout=self.timeout) as response:
            self._validate_url(response.geturl())
            return _read_bounded(response, self.max_download_bytes)

    def _download(self, url: str, path: Path, *, require_pdf: bool = True) -> None:
        self._validate_url(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": "MaxRead/0.1 (+https://github.com/xiaolong-li1/maxread)",
            "Accept": "application/pdf,image/*,*/*",
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                self._validate_url(response.geturl())
                data = _read_bounded(response, self.max_download_bytes)
            if require_pdf and not data.startswith(b"%PDF-"):
                raise RuntimeError("resolved resource is not a PDF")
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        if parsed.scheme != "https" or host not in self.allowed_hosts:
            raise RuntimeError(f"document host is not approved: {host or '[missing]'}")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise RuntimeError(f"document host DNS failed: {host}: {exc}") from exc
        for value in addresses:
            address = ipaddress.ip_address(value)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                raise RuntimeError(f"document host resolved to a non-public address: {host}")


def _pdf_bundle(ref: PaperRef, url: str, pdf_path: Path, warnings: list[str]) -> PaperBundle:
    import fitz

    document = fitz.open(pdf_path)
    try:
        source_dir = pdf_path.parent / "source"
        figure_dir = source_dir / "pdf_figures"
        figure_dir.mkdir(parents=True, exist_ok=True)
        source_text, headings, title = _structured_pdf_text(document)
        figures = _pdf_figures(document, headings, source_dir, figure_dir, warnings)
        tables = _pdf_tables(document, warnings)
        metadata_title = str(document.metadata.get("title") or "").strip()
        if _usable_pdf_title(metadata_title):
            title = metadata_title
    finally:
        document.close()
    title = title or Path(urllib.parse.urlparse(url).path).stem or "Technical report"
    source_path = pdf_path.parent / "document-layout.md"
    source_path.write_text(source_text, encoding="utf-8")
    metadata = ArxivMetadata(
        paper_id=ref.paper_id,
        title=title,
        authors=[],
        summary=_abstract_from_text(source_text),
        published="",
        updated="",
        categories=["PDF technical report"],
        pdf_url=url,
        abs_url=url,
        source_kind="document",
        source_label="PDF technical report",
    )
    return PaperBundle(
        metadata=metadata,
        pdf_path=pdf_path,
        source_path=source_path,
        source_dir=source_dir,
        source_text=source_text,
        pdf_text="",
        source_tree="document.pdf\ndocument-layout.md\npdf_figures/",
        source_assets=[figure.asset for figure in figures],
        source_captions=[figure.caption for figure in figures],
        source_figures=figures,
        source_tables=tables,
        parse_warnings=warnings + [
            "PDF-only layout evidence used; equations are verified after publishing",
            f"Recovered {len(figures)} figures and {len(tables)} tables from the PDF layout",
        ],
    )


def _structured_pdf_text(document) -> tuple[str, list[tuple[int, float, str]], str]:
    spans = []
    page_dicts = []
    for page in document:
        payload = page.get_text("dict", sort=True)
        page_dicts.append(payload)
        spans.extend(
            float(span.get("size") or 0)
            for block in payload.get("blocks", [])
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text") or "").strip()
        )
    # Body text dominates normal papers, but sparse title pages can make the
    # median too large. A lower quantile keeps numbered method headings visible
    # without promoting ordinary body lines.
    ordered_sizes = sorted(spans)
    base_size = ordered_sizes[round((len(ordered_sizes) - 1) * 0.30)] if ordered_sizes else 10.0
    headings: list[tuple[int, float, str]] = []
    output: list[str] = []
    title_candidates: list[tuple[float, float, str]] = []
    for page_index, payload in enumerate(page_dicts):
        output.append(f"\n[PDF page {page_index + 1}]\n")
        for block in payload.get("blocks", []):
            if int(block.get("type", 0)) != 0:
                continue
            text = _block_text(block)
            if not text:
                continue
            sizes = [
                float(span.get("size") or 0)
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if str(span.get("text") or "").strip()
            ]
            size = max(sizes or [base_size])
            bbox = block.get("bbox") or (0, 0, 0, 0)
            is_heading = len(text) <= 180 and size >= max(base_size * 1.22, base_size + 1.5)
            if page_index == 0 and len(text) <= 300:
                title_candidates.append((float(bbox[1]), size, text))
            if is_heading and not PDF_CAPTION_RE.match(text) and not TABLE_CAPTION_RE.match(text):
                headings.append((page_index, float(bbox[1]), text))
                output.append(f"\n## {text}\n")
            else:
                output.append(text)
    if title_candidates:
        max_size = max(item[1] for item in title_candidates)
        title_lines = [item for item in title_candidates if item[0] < 300 and item[1] >= max_size * 0.94]
        title = " ".join(item[2] for item in sorted(title_lines)).strip()
    else:
        title = ""
    text, _count = sanitize_unicode_text("\n\n".join(output))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return text[:600_000], headings, title


def _pdf_figures(document, headings, source_dir: Path, figure_dir: Path, warnings: list[str]) -> list[PaperFigure]:
    import fitz

    figures: list[PaperFigure] = []
    index = 0
    for page_index, page in enumerate(document):
        blocks = [block for block in page.get_text("blocks", sort=True) if len(block) >= 5]
        captions = []
        for block in blocks:
            text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
            match = PDF_CAPTION_RE.match(text)
            if match:
                captions.append((block, text, match.group("number")))
        previous_bottom = page.rect.y0 + 24
        for caption_index, (block, caption, number) in enumerate(captions):
            bbox = fitz.Rect(block[:4])
            owner = _owner_for_position(headings, page_index, bbox.y0)
            next_top = (
                fitz.Rect(captions[caption_index + 1][0][:4]).y0 - 8
                if caption_index + 1 < len(captions)
                else page.rect.y1 - 24
            )
            clip = _figure_clip(page, bbox, previous_bottom, next_top)
            previous_bottom = max(previous_bottom, bbox.y1 + 8)
            if clip.height < 60 or clip.width < 80:
                warnings.append(f"Figure {number} crop was too small and was skipped")
                continue
            index += 1
            path = figure_dir / f"figure-{index:03d}.png"
            page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=clip, alpha=False).save(path)
            constrain_rendered_image(path)
            figures.append(PaperFigure(
                asset=path.relative_to(source_dir).as_posix(),
                caption=caption[:1200],
                label=f"fig:{number}",
                figure_index=index,
                is_appendix=bool(re.search(r"appendix|supplement", owner, re.I)),
                owner_section=owner,
                owner_evidence=f"pdf-caption-page-{page_index + 1}",
            ))
    return figures


def _figure_clip(page, caption_bbox, lower_bound: float, next_caption_top: float):
    import fitz

    page_rect = page.rect
    candidates = []
    candidates_below = []
    column = None
    if caption_bbox.width / max(1.0, page_rect.width) < 0.62:
        column = fitz.Rect(max(page_rect.x0, caption_bbox.x0 - 24), page_rect.y0, min(page_rect.x1, caption_bbox.x1 + 24), page_rect.y1)
    for info in page.get_image_info(xrefs=True):
        rect = fitz.Rect(info.get("bbox") or (0, 0, 0, 0))
        if rect.y1 <= caption_bbox.y0 + 2 and rect.y0 >= lower_bound and rect.get_area() > 800:
            if column is None or rect.intersects(column):
                candidates.append(rect)
        elif rect.y0 >= caption_bbox.y1 - 2 and rect.y1 <= next_caption_top and rect.get_area() > 800:
            if column is None or rect.intersects(column):
                candidates_below.append(rect)
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing.get("rect") or (0, 0, 0, 0))
        if rect.y1 <= caption_bbox.y0 + 2 and rect.y0 >= lower_bound and rect.get_area() > 100:
            if column is None or rect.intersects(column):
                candidates.append(rect)
        elif rect.y0 >= caption_bbox.y1 - 2 and rect.y1 <= next_caption_top and rect.get_area() > 100:
            if column is None or rect.intersects(column):
                candidates_below.append(rect)
    if candidates:
        union = candidates[0]
        for rect in candidates[1:]:
            union |= rect
        clip = fitz.Rect(union.x0 - 12, union.y0 - 12, union.x1 + 12, caption_bbox.y0 - 4)
    elif candidates_below:
        union = candidates_below[0]
        for rect in candidates_below[1:]:
            union |= rect
        clip = fitz.Rect(union.x0 - 12, caption_bbox.y1 + 4, union.x1 + 12, union.y1 + 12)
    else:
        clip = fitz.Rect(column.x0 if column else page_rect.x0 + 24, lower_bound, column.x1 if column else page_rect.x1 - 24, caption_bbox.y0 - 4)
    return clip & page_rect


def _pdf_tables(document, warnings: list[str]) -> list[str]:
    tables: list[str] = []
    for page_index, page in enumerate(document):
        try:
            found = page.find_tables()
        except Exception as exc:
            warnings.append(f"PDF table detection unavailable on page {page_index + 1}: {exc}")
            continue
        for table_index, table in enumerate(getattr(found, "tables", []), start=1):
            try:
                rows = table.extract()
            except Exception:
                continue
            cleaned = [[re.sub(r"\s+", " ", str(cell or "")).strip() for cell in row] for row in rows]
            cleaned = [row for row in cleaned if any(row)]
            if len(cleaned) < 2:
                continue
            width = max(len(row) for row in cleaned)
            cleaned = [row + [""] * (width - len(row)) for row in cleaned]
            lines = [f"PDF page {page_index + 1}, table {table_index}"]
            lines.append("| " + " | ".join(cleaned[0]) + " |")
            lines.append("| " + " | ".join(["---"] * width) + " |")
            lines.extend("| " + " | ".join(row) + " |" for row in cleaned[1:120])
            tables.append("\n".join(lines))
    return tables


def _pdf_plain_text(path: Path, max_pages: int = 0) -> str:
    import fitz

    document = fitz.open(path)
    try:
        limit = len(document) if max_pages <= 0 else min(max_pages, len(document))
        return "\n".join(document[index].get_text("text", sort=True) for index in range(limit))
    finally:
        document.close()


def _block_text(block: dict) -> str:
    lines = []
    for line in block.get("lines", []):
        text = "".join(str(span.get("text") or "") for span in line.get("spans", [])).strip()
        if text:
            lines.append(text)
    return " ".join(lines).strip()


def _owner_for_position(headings: Iterable[tuple[int, float, str]], page_index: int, y: float) -> str:
    owner = ""
    for heading_page, heading_y, title in headings:
        if heading_page > page_index or (heading_page == page_index and heading_y > y):
            break
        owner = title
    return owner


def _markdown_tables(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    tables: list[str] = []
    index = 0
    while index < len(lines) - 1:
        if "|" not in lines[index] or not re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1]):
            index += 1
            continue
        block = [lines[index], lines[index + 1]]
        index += 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            block.append(lines[index])
            index += 1
        tables.append("\n".join(block))
    return tables


def _markdown_title(markdown: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.M)
    return match.group(1).strip() if match else ""


def _markdown_summary(markdown: str) -> str:
    cleaned = re.sub(r"```.*?```", "", markdown, flags=re.S)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    for paragraph in re.split(r"\n\s*\n", cleaned):
        text = re.sub(r"[#*_`>\[\]()]", "", paragraph).strip()
        if len(text) >= 80 and not text.startswith(("|", "http")):
            return re.sub(r"\s+", " ", text)[:2000]
    return "Technical source provided by its official repository."


def _abstract_from_text(text: str) -> str:
    match = re.search(r"(?is)(?:^|\n)#{0,3}\s*abstract\s*\n+(.*?)(?=\n#{1,3}\s|\n\[PDF page|\Z)", text)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:4000] if match else re.sub(r"\s+", " ", text).strip()[:2000]


def _preferred_pdf(siblings: list[str], markdown: str) -> str:
    candidates = [name for name in siblings if name.lower().endswith(".pdf")]
    candidates.extend(
        match.group(0).rstrip(")]>,.;")
        for match in re.finditer(r"https?://[^\s<>'\"]+\.pdf(?:\?[^\s<>'\"]*)?", markdown, re.I)
    )
    if not candidates:
        return ""
    def score(value: str) -> tuple[int, int]:
        lower = value.lower()
        priority = 3 if "tech" in lower and "report" in lower else 2 if "paper" in lower else 1 if "report" in lower else 0
        return priority, -len(value)
    return max(dict.fromkeys(candidates), key=score)


def _arxiv_id(text: str) -> str:
    match = ARXIV_LINK_RE.search(str(text or "")[:12_000])
    return match.group("id") if match else ""


def _huggingface_model_repo(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname == "huggingface.co" and len([part for part in parsed.path.split("/") if part]) == 2


def _huggingface_repo_parts(url: str) -> tuple[str, str]:
    parts = [part for part in urllib.parse.urlparse(url).path.split("/") if part]
    if len(parts) != 2:
        raise RuntimeError("invalid Hugging Face model repository URL")
    return parts[0], parts[1]


def _resolve_markdown_asset(readme_url: str, raw_url: str) -> str:
    value = str(raw_url or "").strip().strip("<>")
    if value.startswith(("http://", "https://")):
        return canonical_document_url(value)
    return urllib.parse.urljoin(readme_url.rsplit("/README.md", 1)[0] + "/", value)


def _looks_like_image_url(url: str) -> bool:
    return Path(urllib.parse.urlparse(url).path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}


def _usable_pdf_title(value: str) -> bool:
    text = str(value or "").strip()
    return len(text) >= 8 and text.lower() not in {"untitled", "microsoft word", "document"}


def _read_bounded(response, byte_limit: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > byte_limit:
        raise RuntimeError(f"document exceeds download limit ({byte_limit} bytes)")
    chunks = []
    total = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > byte_limit:
            raise RuntimeError(f"document exceeds download limit ({byte_limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)
