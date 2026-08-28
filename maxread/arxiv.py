from __future__ import annotations

import gzip
import html
import io
import os
import re
import tarfile
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import shutil
from typing import Dict, Iterable, List, Optional, Tuple

from .models import ArxivMetadata, PaperBundle, PaperFigure, PaperRef
from .text_safety import sanitize_unicode_text


ARXIV_URL_RE = re.compile(
    r"https?://(?:www\.)?arxiv\.org/(?P<kind>abs|pdf|html)/(?P<id>\d{4}\.\d{4,5})(?P<version>v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)
ARXIV_ID_RE = re.compile(r"(?<![\w./-])(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?![\w.-])", re.IGNORECASE)
FIGURE_CAPTION_RE = re.compile(r"\\(?:caption\*?(?:\[[^\]]*\])?|captionof\{figure\}(?:\[[^\]]*\])?)\{")
GRAPHICS_COMMAND_RE = re.compile(
    r"\\(?:includegraphics|begin\{overpic\})\s*(?:\[[^\]]*\]\s*)?\{([^}]+)\}",
    flags=re.S,
)

_ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
_ARXIV_PACE_LOCK = threading.Lock()
_ARXIV_LAST_REQUEST_AT = 0.0


def extract_arxiv_refs(text: str) -> List[PaperRef]:
    seen = set()
    refs: List[PaperRef] = []
    for match in ARXIV_URL_RE.finditer(text):
        paper_id = match.group("id")
        if paper_id in seen:
            continue
        seen.add(paper_id)
        versioned_id = paper_id + (match.group("version") or "")
        kind = match.group("kind").lower()
        if kind == "html":
            url = f"https://arxiv.org/pdf/{versioned_id}"
        elif kind == "pdf":
            url = f"https://arxiv.org/pdf/{versioned_id}"
        else:
            url = f"https://arxiv.org/abs/{versioned_id}"
        refs.append(PaperRef(paper_id=paper_id, url=url))
    for match in ARXIV_ID_RE.finditer(text):
        paper_id = match.group("id")
        if paper_id in seen:
            continue
        seen.add(paper_id)
        refs.append(PaperRef(paper_id=paper_id, url=f"https://arxiv.org/abs/{paper_id}"))
    return refs


class ArxivClient:
    def __init__(self, workdir: Path, timeout: int = 45, parallel_streams: int = 4, parallel_min_bytes: int = 1_048_576):
        self.workdir = workdir
        self.timeout = timeout
        self.parallel_streams = max(1, int(parallel_streams or 1))
        self.parallel_min_bytes = max(0, int(parallel_min_bytes or 0))
        self._last_request_at = 0.0
        self.arxiv_relay_url = os.environ.get("MAXREAD_ARXIV_RELAY_URL", "").strip().rstrip("/")

    def fetch(self, paper_id: str) -> PaperBundle:
        paper_dir = self.workdir / "papers" / paper_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        metadata_warnings: List[str] = []
        try:
            metadata = self.fetch_metadata(paper_id)
        except Exception as exc:
            # Metadata is useful for naming, but it must not prevent the
            # independent source/PDF paths from running when arXiv resets one
            # request. The source is the actual completeness prerequisite.
            metadata = self._fallback_metadata(paper_id)
            metadata_warnings.append(f"arXiv metadata fetch failed; continued with source/PDF fallback: {exc}")
        # arXiv is happier when a single client does not chain metadata/pdf/source
        # requests back-to-back. This also matches the intended usage: a few
        # papers per hour, not a bulk mirror.
        source_path, source_dir, source_text, source_tree, source_assets, source_captions, source_figures, source_tables, source_macros, source_latex_macros, source_latex_arg_macros, source_warnings = self.fetch_source_text(paper_id, paper_dir)
        if source_text.strip():
            existing_pdf = paper_dir / f"{paper_id}.pdf"
            pdf_path = existing_pdf if existing_pdf.exists() and existing_pdf.stat().st_size > 0 else None
            pdf_text = ""
            pdf_warnings = ["PDF text extraction skipped because TeX source is available"]
        else:
            time.sleep(1.5)
            pdf_path, pdf_text, pdf_warnings = self.fetch_pdf_text(paper_id, paper_dir)
        return PaperBundle(
            metadata=metadata,
            pdf_path=pdf_path,
            source_path=source_path,
            source_dir=source_dir,
            source_text=source_text,
            pdf_text=pdf_text,
            source_tree=source_tree,
            source_assets=source_assets,
            source_captions=source_captions,
            source_figures=source_figures,
            source_tables=source_tables,
            source_macros=source_macros,
            source_latex_macros=source_latex_macros,
            source_latex_arg_macros=source_latex_arg_macros,
            parse_warnings=metadata_warnings + source_warnings + pdf_warnings,
        )

    @staticmethod
    def _fallback_metadata(paper_id: str) -> ArxivMetadata:
        return ArxivMetadata(
            paper_id=paper_id,
            title=f"arXiv {paper_id}",
            authors=[],
            summary="arXiv metadata temporarily unavailable; use the fetched source/PDF as the evidence package.",
            published="",
            updated="",
            categories=[],
            pdf_url=f"https://arxiv.org/pdf/{paper_id}.pdf",
            abs_url=f"https://arxiv.org/abs/{paper_id}",
        )

    def import_source(self, paper_id: str, input_path: Path) -> Path:
        paper_dir = self.workdir / "papers" / paper_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        source_path = paper_dir / f"{paper_id}.source"
        shutil.copyfile(input_path, source_path)
        # Validate that the imported file is readable before accepting it.
        source_dir = _extract_source_to_dir(source_path, paper_dir / "source")
        texts = list(_source_texts_from_dir(source_dir))
        if not texts:
            source_path.unlink(missing_ok=True)
            shutil.rmtree(source_dir, ignore_errors=True)
            raise RuntimeError("Imported source did not contain readable .tex/.bib/.bbl content")
        return source_path

    def fetch_metadata(self, paper_id: str) -> ArxivMetadata:
        api_warning = ""
        query = urllib.parse.urlencode({"id_list": paper_id})
        url = f"https://export.arxiv.org/api/query?{query}"
        try:
            data = self._get(url)
        except RuntimeError as exc:
            api_warning = str(exc)
        except Exception as exc:
            api_warning = str(exc)
        if api_warning:
            try:
                return self.fetch_metadata_from_abs_page(paper_id, warning=f"API fallback: {api_warning}")
            except Exception as abs_exc:
                raise RuntimeError(f"arXiv metadata unavailable; API failed: {api_warning}; abs failed: {abs_exc}") from abs_exc
        root = ET.fromstring(data)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            raise RuntimeError(f"arXiv returned no entry for {paper_id}")
        title = _norm(entry.findtext("atom:title", default="", namespaces=ns))
        summary = _norm(entry.findtext("atom:summary", default="", namespaces=ns))
        authors = [
            _norm(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        categories = [cat.attrib.get("term", "") for cat in entry.findall("atom:category", ns)]
        pdf_url = f"https://arxiv.org/pdf/{paper_id}.pdf"
        abs_url = f"https://arxiv.org/abs/{paper_id}"
        return ArxivMetadata(
            paper_id=paper_id,
            title=title,
            authors=[a for a in authors if a],
            summary=summary,
            published=entry.findtext("atom:published", default="", namespaces=ns),
            updated=entry.findtext("atom:updated", default="", namespaces=ns),
            categories=[c for c in categories if c],
            pdf_url=pdf_url,
            abs_url=abs_url,
        )

    def fetch_metadata_from_abs_page(self, paper_id: str, warning: str = "") -> ArxivMetadata:
        abs_url = f"https://arxiv.org/abs/{paper_id}"
        try:
            page = self._get(abs_url).decode("utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"arXiv metadata unavailable; API fallback also failed: {exc}") from exc
        title = _meta(page, "citation_title") or _between(page, "<h1 class=\"title mathjax\">", "</h1>")
        title = re.sub(r"^\s*Title:\s*", "", _strip_tags(title)).strip()
        authors = [_strip_tags(a).strip() for a in re.findall(r'<meta name="citation_author" content="([^"]+)"', page)]
        summary = _between(page, "<blockquote class=\"abstract mathjax\">", "</blockquote>")
        summary = re.sub(r"^\s*Abstract:\s*", "", _strip_tags(summary)).strip()
        published = _meta(page, "citation_date")
        categories = []
        primary = _between(page, "<span class=\"primary-subject\">", "</span>")
        if primary:
            categories.append(_strip_tags(primary).strip())
        if warning:
            summary = summary + f"\n\n[metadata fallback note: {warning}]"
        return ArxivMetadata(
            paper_id=paper_id,
            title=title or paper_id,
            authors=[a for a in authors if a],
            summary=summary or "arXiv abstract unavailable due to rate limiting.",
            published=published,
            updated="",
            categories=categories,
            pdf_url=f"https://arxiv.org/pdf/{paper_id}.pdf",
            abs_url=abs_url,
        )

    def fetch_pdf_text(self, paper_id: str, paper_dir: Path) -> Tuple[Optional[Path], str, List[str]]:
        warnings: List[str] = []
        pdf_path = paper_dir / f"{paper_id}.pdf"
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            try:
                self._download_to_path(f"https://arxiv.org/pdf/{paper_id}.pdf", pdf_path)
            except Exception as exc:
                return None, "", [f"PDF download failed: {exc}"]
        text = ""
        extractor_issue = ""
        try:
            import subprocess

            result = subprocess.run(
                ["pdftotext", str(pdf_path), "-"],
                check=False,
                text=True,
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                text = result.stdout
            else:
                extractor_issue = "pdftotext failed"
        except FileNotFoundError:
            extractor_issue = "pdftotext is not installed"
        except Exception as exc:
            extractor_issue = f"PDF text extraction failed: {exc}"
        if not text.strip():
            fallback_text, fallback_issue = _extract_pdf_text_with_python(pdf_path)
            if fallback_text.strip():
                text = fallback_text
                warnings.append(f"{extractor_issue or 'pdftotext returned no text'}; used pypdf fallback")
            else:
                warnings.append(extractor_issue or "PDF text unavailable")
                if fallback_issue:
                    warnings.append(fallback_issue)
        text, invalid_unicode_count = sanitize_unicode_text(text)
        if invalid_unicode_count:
            warnings.append(f"PDF text contained {invalid_unicode_count} isolated Unicode surrogate(s); replaced with U+FFFD")
        return pdf_path, _clip(text, 120_000), warnings


    def fetch_source_text(self, paper_id: str, paper_dir: Path) -> Tuple[Optional[Path], Optional[Path], str, str, List[str], List[str], List[PaperFigure], List[str], Dict[str, str], Dict[str, str], Dict[str, str], List[str]]:
        source_path = paper_dir / f"{paper_id}.source"
        if not source_path.exists() or source_path.stat().st_size == 0:
            errors = []
            downloaded = False
            source_urls = (
                f"https://export.arxiv.org/e-print/{paper_id}",
                f"https://export.arxiv.org/src/{paper_id}",
                f"https://arxiv.org/e-print/{paper_id}",
                f"https://arxiv.org/src/{paper_id}",
            )
            for source_url in source_urls:
                try:
                    self._download_to_path(source_url, source_path)
                    downloaded = True
                    break
                except Exception as exc:
                    errors.append(f"{source_url}: {exc}")
            if not downloaded:
                detail = "；".join(errors[-4:])
                return None, None, "", "", [], [], [], [], {}, {}, {}, [f"TeX source download failed: {detail}"]
        try:
            source_dir = _extract_source_to_dir(source_path, paper_dir / "source")
            texts = list(_source_texts_from_dir(source_dir))
            if not texts:
                return source_path, source_dir, "", _source_tree(source_dir), _source_assets(source_dir), [], [], [], {}, {}, {}, ["TeX source archive contained no readable .tex/.bib/.bbl files"]
            combined = "\n\n".join(texts)
            macros = _extract_simple_macros(combined)
            latex_macros, latex_arg_macros = _extract_latex_macro_definitions(combined)
            combined = _expand_simple_macros(_strip_simple_macro_definitions(combined), macros)
            tree = _source_tree(source_dir)
            assets = _source_assets(source_dir)
            captions = _extract_captions(combined)
            figures = _extract_figures_from_dir(source_dir, macros=macros)
            tables = _extract_tables(combined)
            return source_path, source_dir, _clip_source_with_appendix(combined, 90_000), tree, assets, captions, figures, tables, macros, latex_macros, latex_arg_macros, []
        except Exception as exc:
            return source_path, None, "", "", [], [], [], [], {}, {}, {}, [f"TeX source extraction failed: {exc}"]

    def _get(self, url: str) -> bytes:
        self._pace_requests()
        return self._get_once(url)

    def _get_once(self, url: str, range_header: str = "") -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "MaxRead/0.1 (local research assistant; contact: local-user)",
                "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            },
        )
        if range_header:
            req.add_header("Range", range_header)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    self._last_request_at = time.monotonic()
                    return response.read()
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_exc = exc
                if exc.code == 429 and attempt < 1:
                    retry_after = exc.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else 20
                    time.sleep(min(delay, 60))
                    continue
                if exc.code == 429:
                    raise RuntimeError("arXiv rate limited this client (HTTP 429); wait a few minutes and retry") from exc
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt + 1 >= 3:
                    if self.arxiv_relay_url:
                        try:
                            return self._get_via_relay(url)
                        except Exception as relay_exc:
                            last_exc = relay_exc
                    raise
                time.sleep(min(2 ** (attempt + 1), 8))
        assert last_exc is not None
        raise last_exc

    def _get_via_relay(self, url: str) -> bytes:
        relay_url = f"{self.arxiv_relay_url}/fetch?{urllib.parse.urlencode({'url': url})}"
        request = urllib.request.Request(
            relay_url,
            headers={"User-Agent": "MaxRead/0.1", "Accept": "*/*"},
        )
        with urllib.request.urlopen(request, timeout=max(self.timeout, 300)) as response:
            return response.read()

    def _download_to_path(self, url: str, output_path: Path) -> None:
        data = self._get_parallel(url)
        output_path.write_bytes(data)

    def _get_parallel(self, url: str) -> bytes:
        streams = self.parallel_streams
        if streams <= 1:
            return self._get(url)
        self._pace_requests()
        probe_req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "MaxRead/0.1 (local research assistant; contact: local-user)",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Range": "bytes=0-0",
            },
        )
        try:
            with urllib.request.urlopen(probe_req, timeout=self.timeout) as response:
                status = getattr(response, "status", response.getcode())
                if status == 200:
                    self._last_request_at = time.monotonic()
                    return response.read()
                if status != 206:
                    self._last_request_at = time.monotonic()
                    return response.read()
                total_size = _parse_content_range_total(response.headers.get("Content-Range", ""))
                response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 416:
                raise
            total_size = 0
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            # Range probing is an optimization. A reset probe should fall
            # back to the ordinary retried download instead of aborting source
            # acquisition before the archive is attempted.
            return self._get_once(url)
        if total_size <= 0 or total_size < self.parallel_min_bytes:
            return self._get_once(url)
        chunks = _split_ranges(total_size, streams)
        parts: list[tuple[int, bytes]] = []
        try:
            with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
                futures = {
                    executor.submit(self._get_once, url, f"bytes={start}-{end}"): (index, start, end)
                    for index, (start, end) in enumerate(chunks)
                }
                for future in as_completed(futures):
                    index, start, end = futures[future]
                    data = future.result()
                    expected = end - start + 1
                    if len(data) != expected:
                        raise RuntimeError(f"Range download returned {len(data)} bytes, expected {expected}")
                    parts.append((index, data))
        except Exception:
            return self._get_once(url)
        self._last_request_at = time.monotonic()
        return b"".join(data for _index, data in sorted(parts))

    def _pace_requests(self) -> None:
        global _ARXIV_LAST_REQUEST_AT
        # This lock is process-wide, so multiple queue workers cannot turn a
        # per-client delay into a burst against arXiv's public endpoints.
        with _ARXIV_PACE_LOCK:
            elapsed = time.monotonic() - _ARXIV_LAST_REQUEST_AT
            if elapsed < _ARXIV_REQUEST_INTERVAL_SECONDS:
                time.sleep(_ARXIV_REQUEST_INTERVAL_SECONDS - elapsed)
            _ARXIV_LAST_REQUEST_AT = time.monotonic()


def _extract_pdf_text_with_python(pdf_path: Path) -> Tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "pypdf fallback is not installed"
    try:
        reader = PdfReader(str(pdf_path))
        chunks = []
        failed_pages = 0
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                failed_pages += 1
        text = "\n\n".join(chunk for chunk in chunks if chunk.strip())
        if not text.strip():
            return "", "pypdf extracted no text"
        issue = f"pypdf skipped {failed_pages} page(s)" if failed_pages else ""
        return text, issue
    except Exception as exc:
        return "", f"pypdf fallback failed: {exc}"


def _parse_content_range_total(value: str) -> int:
    match = re.search(r"/([0-9]+)\s*$", value or "")
    if not match:
        return 0
    return int(match.group(1))


def _split_ranges(total_size: int, streams: int) -> List[Tuple[int, int]]:
    streams = max(1, min(int(streams or 1), total_size))
    chunk_size = (total_size + streams - 1) // streams
    ranges: List[Tuple[int, int]] = []
    start = 0
    while start < total_size:
        end = min(total_size - 1, start + chunk_size - 1)
        ranges.append((start, end))
        start = end + 1
    return ranges


def _extract_source_texts(blob: bytes) -> Iterable[str]:
    suffixes = (".tex", ".bib", ".bbl")
    if tarfile.is_tarfile(fileobj := io.BytesIO(blob)):
        fileobj.seek(0)
        with tarfile.open(fileobj=fileobj, mode="r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile() and m.name.lower().endswith(suffixes)]
            members.sort(key=lambda m: (0 if m.name.lower().endswith(".tex") else 1, m.name))
            for member in members[:80]:
                extracted = tf.extractfile(member)
                if extracted:
                    yield f"% FILE: {member.name}\n" + _decode_text(extracted.read())
        return
    try:
        decompressed = gzip.decompress(blob)
        yield _decode_text(decompressed)
    except OSError:
        yield _decode_text(blob)


def _extract_source_to_dir(source_path: Path, source_dir: Path) -> Path:
    shutil.rmtree(source_dir, ignore_errors=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    blob = source_path.read_bytes()
    fileobj = io.BytesIO(blob)
    if tarfile.is_tarfile(fileobj):
        fileobj.seek(0)
        with tarfile.open(fileobj=fileobj, mode="r:*") as tf:
            for member in tf.getmembers():
                target = (source_dir / member.name).resolve()
                if not str(target).startswith(str(source_dir.resolve())):
                    continue
                tf.extract(member, source_dir)
        return source_dir
    try:
        decompressed = gzip.decompress(blob)
        name = source_path.stem
        if not name.endswith(".tex"):
            name = f"{name}.tex"
        (source_dir / name).write_bytes(decompressed)
    except OSError:
        (source_dir / source_path.name).write_bytes(blob)
    return source_dir


def _source_texts_from_dir(source_dir: Path) -> Iterable[str]:
    suffixes = (".tex", ".bib", ".bbl")
    files = [p for p in source_dir.rglob("*") if p.is_file() and p.name.lower().endswith(suffixes)]
    files.sort(key=_source_file_rank)
    for path in files[:80]:
        rel = path.relative_to(source_dir)
        yield f"% FILE: {rel}\n" + _decode_text(path.read_bytes())


def _source_file_rank(path: Path) -> Tuple[int, str]:
    name = str(path).lower()
    priority = [
        "abstract",
        "intro",
        "prelim",
        "background",
        "observation",
        "method",
        "approach",
        "experiment",
        "evaluation",
        "result",
        "ablation",
        "analysis",
        "conclusion",
        "appendix",
    ]
    if path.suffix.lower() != ".tex":
        return (100, name)
    for idx, key in enumerate(priority):
        if key in name:
            return (idx, name)
    if path.name.lower() == "main.tex":
        return (50, name)
    return (60, name)


def _source_tree(source_dir: Path, max_items: int = 200) -> str:
    items = []
    for path in sorted(source_dir.rglob("*")):
        if len(items) >= max_items:
            items.append("[TRUNCATED]")
            break
        rel = path.relative_to(source_dir)
        if path.is_dir():
            continue
        items.append(str(rel))
    return "\n".join(items)


def _source_assets(source_dir: Path, max_items: int = 240) -> List[str]:
    suffixes = {".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg"}
    assets = [str(p.relative_to(source_dir)) for p in sorted(source_dir.rglob("*")) if p.is_file() and p.suffix.lower() in suffixes]
    return assets[:max_items]


def _extract_captions(tex_text: str, max_items: int = 80) -> List[str]:
    tex_text = _strip_latex_comments(tex_text)
    captions = []
    for match in FIGURE_CAPTION_RE.finditer(tex_text):
        body = _balanced_brace_content(tex_text, match.end() - 1)
        if body:
            captions.append(_clip(_clean_latex_text(_norm(body)), 1200))
        if len(captions) >= max_items:
            break
    return captions


def _extract_tables(tex_text: str, max_items: int = 24) -> List[str]:
    tex_text = _strip_latex_comments(tex_text)
    tables: List[str] = []
    pattern = re.compile(r"\\begin\{table\*?\}.*?\\end\{table\*?\}", re.S)
    for match in pattern.finditer(tex_text):
        block = match.group(0)
        if "tabular" not in block:
            continue
        tables.append(_clip(block, 5000))
        if len(tables) >= max_items:
            break
    return tables


def _extract_figures_from_dir(source_dir: Path, max_items: int = 220, macros: Optional[dict[str, str]] = None) -> List[PaperFigure]:
    figures: List[PaperFigure] = []
    macros = macros or {}
    figure_index = 0
    for tex_path in sorted(source_dir.rglob("*.tex"), key=_source_file_rank):
        rel_tex = str(tex_path.relative_to(source_dir))
        text = _expand_simple_macros(_strip_latex_comments(_decode_text(tex_path.read_bytes())), macros)
        appendix_match = re.search(
            r"\\appendix\b|\\section\*?\s*\{\s*(?:appendix|supplementary\s+material)",
            text,
            flags=re.I,
        )
        appendix_at = appendix_match.start() if appendix_match else -1
        search_cursor = 0
        for block in _figure_blocks(text):
            block_at = text.find(block, search_cursor)
            if block_at < 0:
                block_at = text.find(block)
            if block_at >= 0:
                search_cursor = block_at + len(block)
            is_appendix = appendix_at >= 0 and block_at >= appendix_at
            if _is_subfigure_block(block):
                caption = _last_caption(block)
                label = _last_label(block) or _first_label(block)
                panel_captions = _subfigure_panel_captions(block)
                segment_figures = 0
                for asset_index, (asset, row, col) in enumerate(_includegraphics_assets_with_layout(block, tex_path.parent, source_dir)):
                    figures.append(
                        PaperFigure(
                            asset=asset,
                            caption=caption,
                            panel_caption=panel_captions[asset_index] if asset_index < len(panel_captions) else "",
                            tex_file=rel_tex,
                            label=label,
                            figure_index=figure_index,
                            asset_index=asset_index,
                            row=row,
                            col=col,
                            is_appendix=is_appendix,
                        )
                    )
                    segment_figures += 1
                    if len(figures) >= max_items:
                        return figures
                if segment_figures:
                    figure_index += 1
                continue
            for segment, caption, label in _figure_segments(block):
                segment_figures = 0
                for asset_index, (asset, row, col) in enumerate(_includegraphics_assets_with_layout(segment, tex_path.parent, source_dir)):
                    figures.append(
                        PaperFigure(
                            asset=asset,
                            caption=caption,
                            tex_file=rel_tex,
                            label=label,
                            figure_index=figure_index,
                            asset_index=asset_index,
                            row=row,
                            col=col,
                            is_appendix=is_appendix,
                        )
                    )
                    segment_figures += 1
                    if len(figures) >= max_items:
                        return figures
                if segment_figures:
                    figure_index += 1
    return figures


def _is_subfigure_block(block: str) -> bool:
    return "\\begin{subfigure" in block and len(GRAPHICS_COMMAND_RE.findall(block)) > 1


def _subfigure_panel_captions(block: str) -> List[str]:
    """Return one subcaption per graphics command, in source order.

    A composed figure's parent caption explains the whole grid, while each
    ``subfigure`` caption carries the panel identity.  The latter is drawn by
    LaTeX rather than embedded in the image asset, so dropping it makes a
    reconstructed figure semantically incomplete.
    """
    panels: List[Tuple[int, int, str]] = []
    pattern = re.compile(r"\\begin\{subfigure\*?\}.*?\\end\{subfigure\*?\}", re.S)
    for match in pattern.finditer(block):
        panels.append((match.start(), match.end(), _last_caption(match.group(0))))
    captions: List[str] = []
    for graphic in GRAPHICS_COMMAND_RE.finditer(block):
        caption = next(
            (value for start, end, value in panels if start <= graphic.start() < end),
            "",
        )
        captions.append(caption)
    return captions

def _extract_simple_macros(tex_text: str) -> dict[str, str]:
    stripped = _strip_latex_comments(tex_text)
    macros: dict[str, str] = {}
    patterns = [
        r"\\def\\(?P<name>[A-Za-z]+)\s*\{",
        r"\\(?:re)?newcommand\*?\s*\{\\(?P<name>[A-Za-z]+)\}\s*\{",
        r"\\(?:re)?newcommand\*?\s*\\(?P<name>[A-Za-z]+)\s*\{",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, stripped):
            name = match.group("name")
            body = _balanced_brace_content(stripped, match.end() - 1)
            if not body or "#" in body:
                continue
            value = _clean_latex_text(body)
            if value and len(value) <= 80:
                macros[name] = value
    return macros


def _extract_latex_macro_definitions(tex_text: str) -> tuple[dict[str, str], dict[str, str]]:
    stripped = _strip_latex_comments(tex_text)
    zero_arg: dict[str, str] = {}
    one_arg: dict[str, str] = {}
    patterns = [
        r"\\(?:re)?newcommand\*?\s*(?:\{\\(?P<name1>[A-Za-z]+)\}|\\(?P<name2>[A-Za-z]+))\s*(?:\[(?P<argc>\d+)\])?\s*\{",
        r"\\def\\(?P<name3>[A-Za-z]+)(?P<defargs>(?:#\d+)*)\s*\{",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, stripped):
            name = match.groupdict().get("name1") or match.groupdict().get("name2") or match.groupdict().get("name3") or ""
            if not name:
                continue
            argc = 0
            if match.groupdict().get("argc"):
                try:
                    argc = int(match.group("argc"))
                except ValueError:
                    argc = 0
            elif match.groupdict().get("defargs"):
                argc = match.group("defargs").count("#")
            if argc > 1:
                continue
            body = _balanced_brace_content(stripped, match.end() - 1).strip()
            if not _is_safe_latex_macro_body(body, expects_arg=argc == 1):
                continue
            if argc == 1:
                one_arg[name] = body
            else:
                zero_arg[name] = body
    return zero_arg, one_arg


def _is_safe_latex_macro_body(body: str, expects_arg: bool = False) -> bool:
    if not body or len(body) > 240:
        return False
    if not expects_arg and "#" in body:
        return False
    if expects_arg and re.search(r"#(?!1)", body):
        return False
    if re.search(r"\\(?:begin|end|def|newcommand|renewcommand|usepackage|RequirePackage|input|include|includegraphics|citep?|ref|label|url|href)\b", body):
        return False
    return _balanced_braces_fragment(body)


def _balanced_braces_fragment(text: str) -> bool:
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


def _expand_simple_macros(text: str, macros: dict[str, str]) -> str:
    for name, value in sorted(macros.items(), key=lambda item: -len(item[0])):
        text = re.sub(rf"\\{re.escape(name)}(?![A-Za-z])\s*", lambda _match, v=value: v + " ", text)
    return text


def _strip_simple_macro_definitions(text: str) -> str:
    text = re.sub(r"\\def\\[A-Za-z]+\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", text)
    text = re.sub(r"\\(?:re)?newcommand\*?\s*(?:\{\\[A-Za-z]+\}|\\[A-Za-z]+)\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", text)
    return text


def _clean_latex_text(text: str) -> str:
    text = re.sub(r"\\(?:textsc|textbf|textit|emph|mathrm|mathbf|mathtt)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:xspace|NB|DX|lpk|wx|qz)(?![A-Za-z])(?:\s*\{([^{}]*)\})?", lambda m: m.group(1) or "", text)
    text = re.sub(r"\\(?:citep?|ref|label|url|href)\s*\{[^{}]*\}", "", text)
    text = re.sub(r"[{}]", "", text)
    text = _norm(text)
    text = re.sub(r"\s+([,.;:!?。；：！？])", r"\1", text)
    return text


def _figure_blocks(tex_text: str) -> Iterable[str]:
    pattern = re.compile(r"\\begin\{figure\*?\}.*?\\end\{figure\*?\}", re.S)
    yield from (match.group(0) for match in pattern.finditer(tex_text))


def _figure_segments(block: str) -> Iterable[Tuple[str, str, str]]:
    captions = []
    for match in FIGURE_CAPTION_RE.finditer(block):
        open_index = match.end() - 1
        close_index = _balanced_brace_end_index(block, open_index)
        if close_index < 0:
            continue
        caption = _clip(_clean_latex_text(_norm(block[open_index + 1:close_index])), 1200)
        captions.append((match.start(), close_index + 1, caption))
    if not captions:
        yield block, "", _first_label(block)
        return
    previous_end = 0
    for index, (caption_start, caption_end, caption) in enumerate(captions):
        next_caption_start = captions[index + 1][0] if index + 1 < len(captions) else len(block)
        segment = block[previous_end:caption_start]
        label_region = block[caption_end:next_caption_start]
        label = _first_label(label_region) or _first_label(block[previous_end:next_caption_start])
        yield segment, caption, label
        previous_end = caption_end


def _includegraphics_assets(block: str, tex_dir: Path, source_dir: Path) -> List[str]:
    return [asset for asset, _row, _col in _includegraphics_assets_with_layout(block, tex_dir, source_dir)]


def _includegraphics_assets_with_layout(block: str, tex_dir: Path, source_dir: Path) -> List[Tuple[str, int, int]]:
    assets: List[Tuple[str, int, int]] = []
    row = 0
    col = 0
    last_end = 0
    for match in GRAPHICS_COMMAND_RE.finditer(block):
        between = block[last_end:match.start()]
        if assets:
            if _contains_graphics_row_break(between):
                row += 1
                col = 0
            else:
                col += 1
        raw = match.group(1).strip()
        resolved = _resolve_graphic_path(raw, tex_dir, source_dir)
        assets.append((resolved or raw, row, col))
        last_end = match.end()
    return _compact_layout_rows(assets)


def _contains_graphics_row_break(text: str) -> bool:
    return bool(
        re.search(r"\\\\(?:\s*\[[^\]]*\])?", text)
        or re.search(r"\\(?:vspace|smallskip|medskip|bigskip|par)(?![A-Za-z])", text)
    )


def _compact_layout_rows(assets: List[Tuple[str, int, int]]) -> List[Tuple[str, int, int]]:
    if not assets:
        return []
    row_map = {row: index for index, row in enumerate(sorted({row for _asset, row, _col in assets}))}
    grouped_cols: dict[int, List[int]] = {}
    for _asset, row, col in assets:
        grouped_cols.setdefault(row, []).append(col)
    col_maps = {
        row: {col: index for index, col in enumerate(sorted(set(cols)))}
        for row, cols in grouped_cols.items()
    }
    return [(asset, row_map[row], col_maps[row][col]) for asset, row, col in assets]

def _resolve_graphic_path(raw: str, tex_dir: Path, source_dir: Path) -> str:
    candidate = Path(raw)
    suffixes = [""] if candidate.suffix else [".pdf", ".png", ".jpg", ".jpeg", ".eps"]
    roots = [tex_dir, source_dir]
    source_root = source_dir.resolve()
    for root in roots:
        for suffix in suffixes:
            path = (root / f"{raw}{suffix}").resolve()
            if path.exists() and str(path).startswith(str(source_root)):
                return str(path.relative_to(source_root))
    stem = Path(raw).stem.lower()
    for path in source_dir.rglob("*"):
        if path.is_file() and path.stem.lower() == stem:
            return str(path.resolve().relative_to(source_root))
    return ""


def _first_caption(block: str) -> str:
    match = FIGURE_CAPTION_RE.search(block)
    if not match:
        return ""
    return _clip(_clean_latex_text(_norm(_balanced_brace_content(block, match.end() - 1))), 1200)


def _last_caption(block: str) -> str:
    caption = ""
    for match in FIGURE_CAPTION_RE.finditer(block):
        value = _balanced_brace_content(block, match.end() - 1)
        if value:
            caption = _clip(_clean_latex_text(_norm(value)), 1200)
    return caption


def _first_label(block: str) -> str:
    match = re.search(r"\\label\{([^}]+)\}", block)
    return match.group(1).strip() if match else ""


def _last_label(block: str) -> str:
    matches = re.findall(r"\\label\{([^}]+)\}", block)
    return matches[-1].strip() if matches else ""


def _strip_latex_comments(text: str) -> str:
    cleaned = []
    for line in text.splitlines():
        cleaned.append(re.sub(r"(?<!\\)%.*$", "", line))
    return "\n".join(cleaned)


def _balanced_brace_content(text: str, open_brace_index: int) -> str:
    depth = 0
    start = open_brace_index + 1
    i = open_brace_index
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    return ""


def _balanced_brace_end_index(text: str, open_brace_index: int) -> int:
    depth = 0
    i = open_brace_index
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding, errors="replace")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clip(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[TRUNCATED]"


def _clip_source_with_appendix(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    appendix_start = _find_appendix_start(text)
    if appendix_start < 0 or appendix_start < max_chars // 3:
        return _clip(text, max_chars)
    appendix_budget = min(24_000, max_chars // 3)
    main_budget = max_chars - appendix_budget - 80
    main = text[:main_budget].rstrip()
    appendix = text[appendix_start:appendix_start + appendix_budget].strip()
    return f"{main}\n\n[TRUNCATED: skipped middle source; preserved appendix excerpt]\n\n{appendix}\n\n[TRUNCATED]"


def _find_appendix_start(text: str) -> int:
    patterns = [
        r"\\appendix\b",
        r"\\section\*?\{Appendix",
        r"\\section\*?\{Supplement",
        r"% FILE: [^\n]*(?:appendix|supplement)[^\n]*",
    ]
    starts = [match.start() for pattern in patterns for match in re.finditer(pattern, text, re.I)]
    return min(starts) if starts else -1


def _meta(page: str, name: str) -> str:
    match = re.search(rf'<meta name="{re.escape(name)}" content="([^"]*)"', page)
    return html.unescape(match.group(1)).strip() if match else ""


def _between(text: str, start: str, end: str) -> str:
    idx = text.find(start)
    if idx < 0:
        return ""
    idx += len(start)
    end_idx = text.find(end, idx)
    if end_idx < 0:
        return ""
    return text[idx:end_idx]


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return _norm(html.unescape(text))
