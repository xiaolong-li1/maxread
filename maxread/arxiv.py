from __future__ import annotations

import gzip
import html
import io
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
import shutil
from typing import Iterable, List, Optional, Tuple

from .models import ArxivMetadata, PaperBundle, PaperFigure, PaperRef


ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/)?(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)


def extract_arxiv_refs(text: str) -> List[PaperRef]:
    seen = set()
    refs: List[PaperRef] = []
    for match in ARXIV_ID_RE.finditer(text):
        paper_id = match.group("id")
        if paper_id in seen:
            continue
        seen.add(paper_id)
        refs.append(PaperRef(paper_id=paper_id, url=f"https://arxiv.org/abs/{paper_id}"))
    return refs


class ArxivClient:
    def __init__(self, workdir: Path, timeout: int = 45):
        self.workdir = workdir
        self.timeout = timeout
        self._last_request_at = 0.0

    def fetch(self, paper_id: str) -> PaperBundle:
        paper_dir = self.workdir / "papers" / paper_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        metadata = self.fetch_metadata(paper_id)
        # arXiv is happier when a single client does not chain metadata/pdf/source
        # requests back-to-back. This also matches the intended usage: a few
        # papers per hour, not a bulk mirror.
        source_path, source_dir, source_text, source_tree, source_assets, source_captions, source_figures, source_tables, source_warnings = self.fetch_source_text(paper_id, paper_dir)
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
            parse_warnings=source_warnings + pdf_warnings,
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
        try:
            return self.fetch_metadata_from_abs_page(paper_id)
        except Exception as abs_exc:
            abs_warning = str(abs_exc)
        query = urllib.parse.urlencode({"id_list": paper_id})
        url = f"https://export.arxiv.org/api/query?{query}"
        try:
            data = self._get(url)
        except RuntimeError as exc:
            if "HTTP 429" in str(exc):
                raise RuntimeError(f"arXiv metadata unavailable; abs page failed: {abs_warning}; API failed: {exc}") from exc
            raise
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
                pdf_path.write_bytes(self._get(f"https://arxiv.org/pdf/{paper_id}.pdf"))
            except Exception as exc:
                return None, "", [f"PDF download failed: {exc}"]
        text = ""
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
                warnings.append("pdftotext failed; PDF text unavailable")
        except FileNotFoundError:
            warnings.append("pdftotext is not installed; PDF text unavailable")
        except Exception as exc:
            warnings.append(f"PDF text extraction failed: {exc}")
        return pdf_path, _clip(text, 120_000), warnings

    def fetch_source_text(self, paper_id: str, paper_dir: Path) -> Tuple[Optional[Path], Optional[Path], str, str, List[str], List[str], List[PaperFigure], List[str], List[str]]:
        source_path = paper_dir / f"{paper_id}.source"
        if not source_path.exists() or source_path.stat().st_size == 0:
            try:
                source_path.write_bytes(self._get(f"https://arxiv.org/e-print/{paper_id}"))
            except urllib.error.HTTPError as exc:
                return None, None, "", "", [], [], [], [], [f"TeX source unavailable: HTTP {exc.code}"]
            except Exception as exc:
                return None, None, "", "", [], [], [], [], [f"TeX source download failed: {exc}"]
        try:
            source_dir = _extract_source_to_dir(source_path, paper_dir / "source")
            texts = list(_source_texts_from_dir(source_dir))
            if not texts:
                return source_path, source_dir, "", _source_tree(source_dir), _source_assets(source_dir), [], [], [], ["TeX source archive contained no readable .tex/.bib/.bbl files"]
            combined = "\n\n".join(texts)
            macros = _extract_simple_macros(combined)
            combined = _expand_simple_macros(_strip_simple_macro_definitions(combined), macros)
            tree = _source_tree(source_dir)
            assets = _source_assets(source_dir)
            captions = _extract_captions(combined)
            figures = _extract_figures_from_dir(source_dir, macros=macros)
            tables = _extract_tables(combined)
            return source_path, source_dir, _clip(combined, 90_000), tree, assets, captions, figures, tables, []
        except Exception as exc:
            return source_path, None, "", "", [], [], [], [], [f"TeX source extraction failed: {exc}"]

    def _get(self, url: str) -> bytes:
        self._pace_requests()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "MaxRead/0.1 (local research assistant; contact: local-user)",
                "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            },
        )
        last_exc: Exception | None = None
        for attempt in range(2):
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
        assert last_exc is not None
        raise last_exc

    def _pace_requests(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < 3.0:
            time.sleep(3.0 - elapsed)


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
    for match in re.finditer(r"\\caption(?:\[[^\]]*\])?\{", tex_text):
        body = _balanced_brace_content(tex_text, match.end() - 1)
        if body:
            captions.append(_clip(_clean_latex_text(_norm(body)), 1200))
        if len(captions) >= max_items:
            break
    return captions


def _extract_tables(tex_text: str, max_items: int = 8) -> List[str]:
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
        for block in _figure_blocks(text):
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
                        )
                    )
                    segment_figures += 1
                    if len(figures) >= max_items:
                        return figures
                if segment_figures:
                    figure_index += 1
    return figures

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
    for match in re.finditer(r"\\caption(?:\[[^\]]*\])?\{", block):
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
    for match in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", block):
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
    match = re.search(r"\\caption(?:\[[^\]]*\])?\{", block)
    if not match:
        return ""
    return _clip(_clean_latex_text(_norm(_balanced_brace_content(block, match.end() - 1))), 1200)


def _first_label(block: str) -> str:
    match = re.search(r"\\label\{([^}]+)\}", block)
    return match.group(1).strip() if match else ""


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
