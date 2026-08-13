from __future__ import annotations

import json
import hashlib
import html
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import ArticleBundle, ArticleImage, ArticleSection


class WebArticleClient:
    def __init__(self, workdir: Path, timeout: int = 45):
        self.workdir = workdir
        self.timeout = timeout

    def fetch(self, url: str) -> ArticleBundle:
        warnings: List[str] = []
        article_id = article_id_for_url(url)
        article_dir = self.workdir / "articles" / article_id
        article_dir.mkdir(parents=True, exist_ok=True)
        try:
            html_text = self._get_text(url)
        except UnsupportedWebArticleError:
            raise
        except Exception as exc:
            return self._fetch_rendered_bundle(
                url,
                article_id,
                article_dir,
                [f"raw-fetch-failed:{_clip_inline(str(exc), 180)}"],
            )
        parser = ArticleHTMLParser(url)
        parser.feed(html_text)
        parser.close()
        images = self._download_images(parser.images, article_dir)
        rendered_snapshots: List[RenderedSnapshot] = []
        if _should_capture_rendered_snapshots(url, html_text):
            rendered_snapshots, snapshot_warnings = _capture_rendered_snapshots(
                url,
                article_dir,
                start_index=max([image.source_index for image in images] or [0]) + 1,
                max_items=int(os.environ.get("MAXREAD_WEB_RENDERED_SNAPSHOT_LIMIT", "24")),
                timeout=self.timeout,
            )
            warnings.extend(snapshot_warnings)
        if rendered_snapshots:
            images = [snapshot.image for snapshot in rendered_snapshots]
            section_blocks = _inject_rendered_snapshot_blocks(parser.section_blocks(images), rendered_snapshots)
        else:
            section_blocks = parser.section_blocks(images)
        text = parser.main_text(images)
        if _article_material_too_thin(text, parser.sections, images, parser.tables, parser.code_blocks, parser.math_blocks):
            return self._fetch_rendered_bundle(url, article_id, article_dir, warnings, fallback_title=parser.title)
        return ArticleBundle(
            article_id=article_id,
            url=url,
            title=parser.title or url,
            author=parser.meta.get("author", ""),
            published=parser.meta.get("article:published_time", "") or parser.meta.get("date", ""),
            site_name=parser.meta.get("og:site_name", ""),
            text=text,
            sections=parser.sections,
            section_blocks=section_blocks,
            images=images,
            tables=parser.tables,
            code_blocks=parser.code_blocks,
            math_blocks=_dedupe(parser.math_blocks + _extract_math(html_text + "\n" + text)),
            warnings=warnings,
        )

    def _fetch_rendered_bundle(
        self,
        url: str,
        article_id: str,
        article_dir: Path,
        warnings: List[str],
        fallback_title: str = "",
    ) -> ArticleBundle:
        rendered, rendered_warnings = _extract_rendered_page_text(url, article_dir, timeout=self.timeout)
        warnings = list(warnings) + rendered_warnings
        if not rendered or _rendered_page_too_thin(rendered):
            raise UnsupportedWebArticleError("网页正文抓取不足：原始 HTML 与浏览器渲染后都没有拿到有效正文，可能需要登录、被反爬，或页面结构暂不支持。")
        rendered_snapshots, snapshot_warnings = _capture_rendered_snapshots(
            url,
            article_dir,
            start_index=1,
            max_items=int(os.environ.get("MAXREAD_WEB_RENDERED_SNAPSHOT_LIMIT", "24")),
            timeout=self.timeout,
        )
        warnings.extend(snapshot_warnings)
        images = [snapshot.image for snapshot in rendered_snapshots]
        section_blocks = _inject_rendered_snapshot_blocks(rendered.sections, rendered_snapshots) if rendered_snapshots else rendered.sections
        text = _section_blocks_text(section_blocks)
        return ArticleBundle(
            article_id=article_id,
            url=url,
            title=rendered.title or fallback_title or url,
            author="",
            published=rendered.published,
            site_name=rendered.site_name or urllib.parse.urlparse(url).netloc,
            text=text,
            sections=[section.title for section in section_blocks if section.title],
            section_blocks=section_blocks,
            images=images,
            tables=rendered.tables,
            code_blocks=rendered.code_blocks,
            math_blocks=rendered.math_blocks,
            warnings=warnings,
        )

    def _get_text(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "MaxRead/0.1 (local research assistant)",
                "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            content_type = response.headers.get_content_type()
            final_url = response.geturl()
            if content_type == "application/pdf" or urllib.parse.urlparse(final_url).path.lower().endswith(".pdf"):
                raise UnsupportedWebArticleError("direct PDF URLs are not supported yet; send an arXiv/HuggingFace paper link instead")
            data = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")

    def _download_images(self, images: List[ArticleImage], article_dir: Path, max_images: int = 16) -> List[ArticleImage]:
        out_dir = article_dir / "images"
        out_dir.mkdir(parents=True, exist_ok=True)
        downloaded: List[ArticleImage] = []
        for image in images[:max_images]:
            try:
                suffix = Path(urllib.parse.urlparse(image.url).path).suffix.lower() or ".png"
                if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    suffix = ".png"
                name = hashlib.sha256(image.url.encode("utf-8")).hexdigest()[:16] + suffix
                path = out_dir / name
                if not path.exists() or path.stat().st_size == 0:
                    req = urllib.request.Request(image.url, headers={"User-Agent": "MaxRead/0.1 (local research assistant)"})
                    with urllib.request.urlopen(req, timeout=self.timeout) as response:
                        data = response.read()
                    if not _is_raster_image(data):
                        downloaded.append(ArticleImage(url=image.url, local_path=None, caption=image.caption, alt=image.alt, source_index=image.source_index))
                        continue
                    path.write_bytes(data)
                if _is_supported_image_file(path):
                    downloaded.append(ArticleImage(url=image.url, local_path=path, caption=image.caption, alt=image.alt, source_index=image.source_index))
                else:
                    downloaded.append(ArticleImage(url=image.url, local_path=None, caption=image.caption, alt=image.alt, source_index=image.source_index))
            except Exception:
                downloaded.append(image)
        return downloaded


class UnsupportedWebArticleError(RuntimeError):
    pass


StructuredBlock = Tuple[str, int, str]


@dataclass(frozen=True)
class RenderedSnapshot:
    image: ArticleImage
    section_title: str = ""
    anchor_text: str = ""
    y: float = 0.0
    kind: str = ""


@dataclass
class RenderedPageText:
    title: str = ""
    published: str = ""
    site_name: str = ""
    sections: List[ArticleSection] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    code_blocks: List[str] = field(default_factory=list)
    math_blocks: List[str] = field(default_factory=list)


class ArticleHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.meta: Dict[str, str] = {}
        self._stack: List[str] = []
        self._skip_depth = 0
        self._capture_title = False
        self._capture_code = False
        self._capture_table = False
        self._capture_figcaption = False
        self._capture_math = False
        self._capture_tochead = False
        self._current_text: List[str] = []
        self._current_code: List[str] = []
        self._current_table: List[str] = []
        self._current_caption: List[str] = []
        self._current_math: List[str] = []
        self._current_tochead: List[str] = []
        self._last_figure_image: Optional[ArticleImage] = None
        self._last_visual_toc_image: Optional[ArticleImage] = None
        self._in_visual_toc = False
        self._toc_images_by_title: Dict[str, int] = {}
        self.blocks: List[str] = []
        self._structured_blocks: List[StructuredBlock] = []
        self.sections: List[str] = []
        self.images: List[ArticleImage] = []
        self.tables: List[str] = []
        self.code_blocks: List[str] = []
        self.math_blocks: List[str] = []

    def handle_starttag(self, tag: str, attrs_list):
        attrs = dict(attrs_list)
        self._stack.append(tag)
        if tag == "nav" and "visual-toc" in (attrs.get("class") or ""):
            self._in_visual_toc = True
        if tag in {"script", "style", "nav", "footer", "header", "aside", "noscript"}:
            self._skip_depth += 1
        if tag == "title":
            self._capture_title = True
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            value = attrs.get("content")
            if key and value:
                self.meta[key] = html.unescape(value).strip()
                if key in {"og:title", "twitter:title"} and not self.title:
                    self.title = html.unescape(value).strip()
        if self._skip_depth:
            if self._in_visual_toc:
                if tag == "img":
                    self._last_visual_toc_image = self._capture_image(attrs, add_block=False)
                elif tag == "d-tochead":
                    self._capture_tochead = True
                    self._current_tochead = []
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}:
            self._current_text = []
        elif tag == "d-math":
            self._capture_math = True
            self._current_math = []
        elif tag == "pre" or tag == "code":
            self._capture_code = True
            self._current_code = []
        elif tag == "table":
            self._capture_table = True
            self._current_table = []
        elif tag == "figcaption":
            self._capture_figcaption = True
            self._current_caption = []
        elif tag == "img":
            image = self._capture_image(attrs, add_block=True)
            if image and "figure" in self._stack:
                self._last_figure_image = image

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "nav", "footer", "header", "aside", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "d-tochead" and self._capture_tochead:
            title = _clean(" ".join(self._current_tochead))
            if title and self._last_visual_toc_image:
                self._last_visual_toc_image.caption = title
                self._toc_images_by_title[_norm_heading(title)] = self._last_visual_toc_image.source_index
            self._capture_tochead = False
            self._current_tochead = []
        if tag == "nav" and self._in_visual_toc:
            self._in_visual_toc = False
        if tag == "title":
            self._capture_title = False
            if not self.title:
                self.title = _clean(" ".join(self._current_text))
        if self._skip_depth:
            self._pop(tag)
            return
        if tag == "d-math":
            math = _clean_math(" ".join(self._current_math))
            if math:
                self.math_blocks.append(math[:1200])
                if self._in_text_block(exclude={tag}):
                    self._current_text.append(f"<latex>{math}</latex>")
                else:
                    self.blocks.append(f"<latex>{math}</latex>")
                    self._structured_blocks.append(("text", 0, f"<latex>{math}</latex>"))
            self._capture_math = False
            self._current_math = []
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}:
            text = _clean(" ".join(self._current_text))
            if _is_useful_text_block(text):
                if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    level = int(tag[1])
                    if not self._is_boilerplate_heading(text):
                        self.sections.append(text)
                        self.blocks.append(f"## {text}")
                        self._structured_blocks.append(("heading", level, text))
                        toc_image_index = self._toc_images_by_title.get(_norm_heading(text))
                        if toc_image_index:
                            marker_text = f"[ArticleImage:{toc_image_index}] {text}".strip()
                            self.blocks.append(marker_text)
                            self._structured_blocks.append(("image", toc_image_index, text))
                else:
                    self.blocks.append(text)
                    self._structured_blocks.append(("text", 0, text))
            self._current_text = []
        elif tag == "pre":
            code = "\n".join(self._current_code).strip()
            if code:
                self.code_blocks.append(code[:4000])
                self._structured_blocks.append(("text", 0, f"[Code] {code[:1200]}"))
            self._capture_code = False
            self._current_code = []
        elif tag == "table":
            table = _clean(" ".join(self._current_table))
            if table:
                self.tables.append(table[:4000])
                self._structured_blocks.append(("text", 0, f"[Table] {table[:1200]}"))
            self._capture_table = False
            self._current_table = []
        elif tag == "figcaption":
            caption = _clean(" ".join(self._current_caption))
            if caption and self._last_figure_image:
                self._last_figure_image.caption = caption
                self._update_image_caption(self._last_figure_image.source_index, caption)
            self._capture_figcaption = False
            self._current_caption = []
        self._pop(tag)

    def handle_data(self, data: str):
        if self._capture_tochead:
            self._current_tochead.append(data)
            return
        if self._skip_depth:
            return
        if self._capture_math:
            self._current_math.append(data)
            return
        if self._capture_title:
            self._current_text.append(data)
        if self._capture_code:
            self._current_code.append(data)
        if self._capture_table:
            self._current_table.append(data)
        if self._capture_figcaption:
            self._current_caption.append(data)
        if self._in_text_block():
            self._current_text.append(data)

    def main_text(self, images: List[ArticleImage] | None = None) -> str:
        if not images:
            return _clip_text("\n\n".join(self.blocks), 100_000)
        available = {image.source_index for image in images if image.local_path}
        blocks = []
        for block in self.blocks:
            match = re.match(r"\[ArticleImage:(\d+)\]\s*(.*)", block)
            if match and int(match.group(1)) not in available:
                continue
            blocks.append(block)
        return _clip_text("\n\n".join(blocks), 100_000)

    def section_blocks(self, images: List[ArticleImage] | None = None) -> List[ArticleSection]:
        available = {image.source_index for image in images if image.local_path} if images is not None else None
        image_by_index = {image.source_index: image for image in images or self.images}
        sections: List[ArticleSection] = []
        current = ArticleSection(title="正文", level=0)

        for kind, value, text in self._structured_blocks:
            if kind == "heading":
                if current.blocks:
                    sections.append(current)
                current = ArticleSection(title=text, level=value)
                continue
            if kind == "image":
                source_index = value
                if available is not None and source_index not in available:
                    continue
                image = image_by_index.get(source_index)
                caption = (image.caption if image else "") or (image.alt if image else "") or text
                current.blocks.append(f"[ArticleImage:{source_index}] {caption}".strip())
                continue
            if text:
                current.blocks.append(text)

        if current.blocks:
            sections.append(current)
        return sections

    def visual_toc_image_indexes(self) -> set[int]:
        return set(self._toc_images_by_title.values())

    def _update_image_caption(self, source_index: int, caption: str) -> None:
        updated: List[StructuredBlock] = []
        for kind, value, text in self._structured_blocks:
            if kind == "image" and value == source_index:
                updated.append((kind, value, caption))
            else:
                updated.append((kind, value, text))
        self._structured_blocks = updated

    def _capture_image(self, attrs: Dict[str, str], add_block: bool) -> Optional[ArticleImage]:
        src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-original")
        if not src:
            return None
        image = ArticleImage(url=urllib.parse.urljoin(self.base_url, src), local_path=None, alt=attrs.get("alt", ""), source_index=len(self.images) + 1)
        self.images.append(image)
        if add_block:
            marker_text = f"[ArticleImage:{image.source_index}] {image.alt}".strip()
            self.blocks.append(marker_text)
            self._structured_blocks.append(("image", image.source_index, image.alt))
        return image

    def _pop(self, tag: str) -> None:
        if tag in self._stack[::-1]:
            idx = len(self._stack) - 1 - self._stack[::-1].index(tag)
            self._stack = self._stack[:idx]

    def _in_text_block(self, exclude: set[str] | None = None) -> bool:
        exclude = exclude or set()
        text_tags = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
        return any(tag in text_tags for tag in self._stack if tag not in exclude)

    def _is_boilerplate_heading(self, text: str) -> bool:
        normalized = _clean(text).lower()
        if normalized in {"authors", "affiliations", "published", "references", "citation"}:
            return True
        return bool(self.title and normalized == self.title.strip().lower())


def article_id_for_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _article_material_too_thin(
    text: str,
    sections: List[str],
    images: List[ArticleImage],
    tables: List[str],
    code_blocks: List[str],
    math_blocks: List[str],
) -> bool:
    chars = len(_material_chars(text))
    has_structured_material = bool(sections or images or tables or code_blocks or math_blocks)
    return chars < 600 and not has_structured_material


def _rendered_page_too_thin(rendered: RenderedPageText) -> bool:
    return len(_material_chars(_section_blocks_text(rendered.sections))) < 600


def _material_chars(text: str) -> str:
    text = re.sub(r"\[ArticleImage:[^\]]+\].*", " ", text or "")
    text = re.sub(r"\s+", "", text)
    return text


def _section_blocks_text(sections: List[ArticleSection]) -> str:
    parts: List[str] = []
    for section in sections:
        if section.title:
            parts.append(f"## {section.title}")
        parts.extend(block for block in section.blocks if block)
    return _clip_text("\n\n".join(parts), 100_000)


def _extract_rendered_page_text(url: str, article_dir: Path, timeout: int) -> tuple[Optional[RenderedPageText], List[str]]:
    node = _node_executable()
    if not node:
        return None, ["rendered-text-skipped:no-node"]
    env = _node_subprocess_env()
    payload = {"url": url}
    try:
        proc = subprocess.run(
            [node, "-e", _RENDERED_TEXT_SCRIPT],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(os.environ.get("MAXREAD_WEB_RENDER_TIMEOUT", "60")),
            env=env,
            check=False,
        )
    except Exception as exc:
        return None, [f"rendered-text-failed:{_clip_inline(str(exc), 180)}"]
    if proc.returncode != 0:
        detail = _clip_inline((proc.stderr or proc.stdout).strip(), 220)
        return None, [f"rendered-text-failed:{detail}"]
    try:
        row = json.loads(proc.stdout or "{}")
    except Exception as exc:
        return None, [f"rendered-text-invalid-json:{_clip_inline(str(exc), 120)}"]
    sections: List[ArticleSection] = []
    for item in row.get("sections") or []:
        blocks = [_clean(str(block)) for block in item.get("blocks") or []]
        blocks = [block for block in blocks if block]
        title = _clean(str(item.get("title") or "正文"))
        if title or blocks:
            sections.append(ArticleSection(title=title or "正文", level=int(item.get("level") or 0), blocks=blocks))
    links = []
    for item in row.get("links") or []:
        label = _clean(str(item.get("text") or item.get("href") or "链接"))
        href = _clean(str(item.get("href") or ""))
        if href:
            links.append((label, href))
    if links and sections:
        link_lines = ["相关链接："] + [f"- {label}: {href}" for label, href in links[:12]]
        sections[0].blocks.insert(0, "\n".join(link_lines))
    page = RenderedPageText(
        title=_clean(str(row.get("title") or "")),
        published=_clean(str(row.get("published") or "")),
        site_name=_clean(str(row.get("siteName") or urllib.parse.urlparse(url).netloc)),
        sections=sections,
        tables=[_clip_text(_clean(str(item)), 4000) for item in row.get("tables") or [] if _clean(str(item))],
        code_blocks=[_clip_text(str(item).strip(), 4000) for item in row.get("codeBlocks") or [] if str(item).strip()],
        math_blocks=_dedupe([_clean_math(str(item)) for item in row.get("mathBlocks") or [] if _clean_math(str(item))]),
    )
    return page, [f"rendered-text-fallback:{len(_material_chars(_section_blocks_text(sections)))}"]


def _should_capture_rendered_snapshots(url: str, html_text: str) -> bool:
    mode = os.environ.get("MAXREAD_WEB_RENDERED_SNAPSHOTS", "auto").lower()
    if mode in {"0", "false", "no", "off"}:
        return False
    if mode in {"1", "true", "yes", "on"}:
        return True
    host = urllib.parse.urlparse(url).netloc.lower()
    visual_count = sum(html_text.lower().count(token) for token in ("<figure", "<d-figure", "<svg", "<canvas", "<table"))
    return "transformer-circuits.pub" in host or "<d-article" in html_text.lower() or visual_count >= 20


def _capture_rendered_snapshots(
    url: str,
    article_dir: Path,
    start_index: int,
    max_items: int,
    timeout: int,
) -> tuple[List[RenderedSnapshot], List[str]]:
    node = _node_executable()
    if not node:
        return [], ["rendered-snapshots-skipped:no-node"]
    env = _node_subprocess_env()
    out_dir = article_dir / "rendered_snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"url": url, "outDir": str(out_dir), "maxItems": max(0, int(max_items))}
    try:
        proc = subprocess.run(
            [node, "-e", _RENDERED_SNAPSHOT_SCRIPT],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(os.environ.get("MAXREAD_WEB_RENDER_TIMEOUT", "60")),
            env=env,
            check=False,
        )
    except Exception as exc:
        return [], [f"rendered-snapshots-failed:{_clip_inline(str(exc), 160)}"]
    if proc.returncode != 0:
        detail = _clip_inline((proc.stderr or proc.stdout).strip(), 220)
        return [], [f"rendered-snapshots-failed:{detail}"]
    try:
        rows = json.loads(proc.stdout or "[]")
    except Exception as exc:
        return [], [f"rendered-snapshots-invalid-json:{_clip_inline(str(exc), 120)}"]
    snapshots: List[RenderedSnapshot] = []
    for offset, row in enumerate(rows[:max_items], start=0):
        path = Path(str(row.get("path") or ""))
        if not path.exists() or path.stat().st_size == 0:
            continue
        caption = _clean(str(row.get("caption") or row.get("kind") or "原网页可视内容"))
        source_index = start_index + offset
        image = ArticleImage(
            url=f"rendered:{source_index}",
            local_path=path,
            caption=caption,
            alt=caption,
            source_index=source_index,
        )
        snapshots.append(
            RenderedSnapshot(
                image=image,
                section_title=_clean(str(row.get("sectionTitle") or "")),
                anchor_text=_clean(str(row.get("anchorText") or "")),
                y=float(row.get("y") or 0.0),
                kind=str(row.get("kind") or ""),
            )
        )
    warnings = [f"rendered-snapshots:{len(snapshots)}"] if snapshots else ["rendered-snapshots:0"]
    return snapshots, warnings


def _inject_rendered_snapshot_blocks(sections: List[ArticleSection], snapshots: List[RenderedSnapshot]) -> List[ArticleSection]:
    if not snapshots:
        return sections
    output = [ArticleSection(section.title, section.level, list(section.blocks)) for section in sections]
    by_title = {_norm_heading(section.title): section for section in output}
    overview = [item for item in snapshots if item.kind == "overview"]
    regular = [item for item in snapshots if item not in overview]
    if overview:
        output.insert(
            0,
            ArticleSection(
                title="原网页标题区和可视目录",
                level=1,
                blocks=[_snapshot_block(item) for item in sorted(overview, key=lambda item: item.y)],
            ),
        )
    first_content = next((section for section in output if section.title != "原网页标题区和可视目录"), None)
    grouped: Dict[int, List[RenderedSnapshot]] = {}
    for snapshot in sorted(regular, key=lambda item: item.y):
        target = by_title.get(_norm_heading(snapshot.section_title))
        if target is None:
            target = _nearest_section_by_heading(output, snapshot.section_title)
        if target is None and not snapshot.section_title:
            target = first_content
        if target is None:
            if not output:
                output.append(ArticleSection(title="正文", level=0))
            target = output[-1]
        grouped.setdefault(id(target), []).append(snapshot)
    by_id = {id(section): section for section in output}
    for section_id, items in grouped.items():
        section = by_id[section_id]
        _spread_snapshots_in_section(section, items)
    return output


def _snapshot_block(snapshot: RenderedSnapshot) -> str:
    return f"[ArticleImage:{snapshot.image.source_index}] {snapshot.image.caption}".strip()


def _nearest_section_by_heading(sections: List[ArticleSection], title: str) -> Optional[ArticleSection]:
    needle = _norm_heading(title)
    if not needle:
        return None
    best: Optional[ArticleSection] = None
    best_score = 0
    for section in sections:
        current = _norm_heading(section.title)
        if not current:
            continue
        if current == needle:
            return section
        if current in needle or needle in current:
            score = len(current)
            if score > best_score:
                best = section
                best_score = score
    return best


def _spread_snapshots_in_section(section: ArticleSection, snapshots: List[RenderedSnapshot]) -> None:
    original = list(section.blocks)
    if not original:
        section.blocks = [_snapshot_block(item) for item in sorted(snapshots, key=lambda item: item.y)]
        return
    insertions: Dict[int, List[RenderedSnapshot]] = {}
    unanchored: List[RenderedSnapshot] = []
    used_keys: set[tuple[int, int]] = set()
    for order, snapshot in enumerate(sorted(snapshots, key=lambda item: item.y)):
        anchor_index = _find_anchor_block(original, snapshot.anchor_text)
        if anchor_index < 0:
            unanchored.append(snapshot)
            continue
        key = min(len(original), anchor_index + 1)
        insertions.setdefault(key, []).append(snapshot)
        used_keys.add((key, order))

    text_insert_points = [index + 1 for index, block in enumerate(original) if not block.startswith("[ArticleImage:")]
    if not text_insert_points:
        text_insert_points = [len(original)]
    unanchored = sorted(unanchored, key=lambda item: item.y)
    for index, snapshot in enumerate(unanchored):
        if len(unanchored) == 1:
            target_pos = 0
        else:
            target_pos = round(index * (len(text_insert_points) - 1) / max(1, len(unanchored) - 1))
        key = text_insert_points[min(max(target_pos, 0), len(text_insert_points) - 1)]
        while insertions.get(key) and key < len(original):
            key += 1
        insertions.setdefault(key, []).append(snapshot)

    rebuilt: List[str] = []
    for index in range(len(original) + 1):
        for snapshot in insertions.get(index, []):
            rebuilt.append(_snapshot_block(snapshot))
        if index < len(original):
            rebuilt.append(original[index])
    section.blocks = rebuilt


def _find_anchor_block(blocks: List[str], anchor_text: str) -> int:
    needle = _norm_text(anchor_text)
    if len(needle) < 24:
        return -1
    best_index = -1
    best_score = 0
    for index, block in enumerate(blocks):
        if block.startswith("[ArticleImage:"):
            continue
        current = _norm_text(block)
        if not current:
            continue
        if needle in current or current in needle:
            return index
        score = _overlap_score(needle, current)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index if best_score >= 0.72 else -1


def _overlap_score(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", left))
    right_tokens = set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))


def _norm_text(text: str) -> str:
    text = re.sub(r"<latex>.*?</latex>", " ", text or "", flags=re.S)
    text = re.sub(r"\[ArticleImage:[^\]]+\]", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip().lower()


def _playwright_node_path() -> str:
    configured = os.environ.get("MAXREAD_PLAYWRIGHT_NODE_MODULES", "")
    candidates = [Path(item).expanduser() for item in configured.split(os.pathsep) if item]
    candidates.extend(
        [
            Path.cwd() / "node_modules",
            Path.cwd() / "var" / "maxread" / "playwright-deps" / "node_modules",
            Path.home() / ".hermes" / "hermes-agent" / "node_modules",
            Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "node_modules",
        ]
    )
    candidates.extend(Path.home().glob(".npm/_npx/*/node_modules"))
    for candidate in candidates:
        if (candidate / "playwright").exists() or (candidate / "playwright-core").exists():
            return str(candidate)
    return ""


def _node_executable() -> str:
    configured = os.environ.get("MAXREAD_NODE", "")
    candidates = [Path(configured).expanduser()] if configured else []
    found = shutil.which("node")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        [
            Path.home() / ".local" / "node" / "bin" / "node",
            Path.home() / ".local" / "bin" / "node",
            Path("/usr/local/bin/node"),
            Path("/usr/bin/node"),
        ]
    )
    candidates.extend(Path.home().glob(".vscode-server/bin/*/node"))
    for candidate in candidates:
        try:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return ""


def _node_subprocess_env() -> Dict[str, str]:
    env = os.environ.copy()
    node_path = _playwright_node_path()
    if node_path:
        existing = env.get("NODE_PATH", "")
        env["NODE_PATH"] = os.pathsep.join([node_path, existing]) if existing else node_path
    local_lib = Path.home() / ".local" / "playwright-libs" / "usr" / "lib" / "x86_64-linux-gnu"
    if local_lib.exists():
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join([str(local_lib), existing]) if existing else str(local_lib)
    return env


_RENDERED_TEXT_SCRIPT = r"""
const fs = require('fs');
const { chromium } = require('playwright');

const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const url = payload.url;

function clean(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1200 },
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    extraHTTPHeaders: { 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8' },
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 90000 });
  await page.waitForTimeout(2500);
  const data = await page.evaluate(() => {
    const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      const r = el.getBoundingClientRect();
      return r.width > 20 && r.height > 8;
    };
    const inChrome = (el) => Boolean(el.closest('script,style,noscript,nav,header,footer,aside'));
    const isHeading = (tag) => /^H[1-4]$/.test(tag);
    const bodyText = clean(document.body ? document.body.innerText : '');
    const dateMatch = bodyText.match(/\b20\d{2}-\d{2}-\d{2}\b/);
    const sections = [];
    let current = { title: '正文', level: 0, blocks: [] };
    const pushCurrent = () => {
      if (current.blocks.length || (current.title && current.title !== '正文')) {
        sections.push(current);
      }
      current = { title: '正文', level: 0, blocks: [] };
    };
    const selector = [
      'h1', 'h2', 'h3', 'h4',
      'p', 'li', 'blockquote', 'table', 'pre',
      'div.mt-markdown-body', 'div.prose', 'div.intro'
    ].join(',');
    const elements = [...document.querySelectorAll(selector)]
      .filter((el) => visible(el) && !inChrome(el))
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return (ar.y + scrollY) - (br.y + scrollY);
      });
    let lastBlock = '';
    for (const el of elements) {
      const tag = el.tagName;
      const text = clean(el.innerText || el.textContent || '');
      if (!text) continue;
      if (tag === 'DIV' && el.querySelector('h1,h2,h3,h4,p,li,blockquote,table,pre')) continue;
      if (isHeading(tag)) {
        const level = Number(tag.slice(1));
        pushCurrent();
        current = { title: text, level, blocks: [] };
        lastBlock = '';
        continue;
      }
      let block = text;
      if (tag === 'TABLE') block = `[Table] ${text}`;
      if (tag === 'PRE') block = `[Code] ${text}`;
      if (block === lastBlock) continue;
      current.blocks.push(block);
      lastBlock = block;
    }
    pushCurrent();
    if (!sections.length && bodyText) {
      sections.push({ title: clean(document.querySelector('h1')?.innerText || document.title || '正文'), level: 1, blocks: [bodyText] });
    }
    const importantLink = (a) => {
      const text = clean(a.innerText || a.getAttribute('aria-label') || a.title || '');
      const href = a.href || '';
      const haystack = `${text} ${href}`.toLowerCase();
      return href && /github|gitlab|huggingface|repository|repo|code|paper|arxiv|demo|api|modelscope|在线体验|仓库|代码/.test(haystack);
    };
    const links = [];
    const seenLinks = new Set();
    for (const a of [...document.querySelectorAll('a')]) {
      if (!importantLink(a)) continue;
      const href = a.href;
      if (seenLinks.has(href)) continue;
      seenLinks.add(href);
      links.push({ text: clean(a.innerText || a.getAttribute('aria-label') || a.title || href), href });
      if (links.length >= 12) break;
    }
    const tables = [...document.querySelectorAll('table')]
      .filter((el) => visible(el) && !inChrome(el))
      .map((el) => clean(el.innerText))
      .filter(Boolean)
      .slice(0, 8);
    const codeBlocks = [...document.querySelectorAll('pre')]
      .filter((el) => visible(el) && !inChrome(el))
      .map((el) => el.innerText || '')
      .filter((text) => clean(text))
      .slice(0, 6);
    const mathBlocks = [...document.querySelectorAll('math, .katex, .MathJax, d-math')]
      .filter((el) => visible(el) && !inChrome(el))
      .map((el) => clean(el.getAttribute('alttext') || el.textContent || ''))
      .filter(Boolean)
      .slice(0, 80);
    return {
      title: clean(document.querySelector('h1')?.innerText || document.title || ''),
      published: dateMatch ? dateMatch[0] : '',
      siteName: location.hostname,
      sections,
      links,
      tables,
      codeBlocks,
      mathBlocks,
    };
  });
  await browser.close();
  process.stdout.write(JSON.stringify(data));
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


_RENDERED_SNAPSHOT_SCRIPT = r"""
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const url = payload.url;
const outDir = payload.outDir;
const maxItems = Math.max(0, Number(payload.maxItems || 24));
fs.mkdirSync(outDir, { recursive: true });

function safeName(text) {
  return String(text || 'snapshot').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 64) || 'snapshot';
}

function clean(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function uniqueRows(rows) {
  const seen = new Set();
  const out = [];
  for (const row of rows) {
    const key = `${Math.round(row.y / 12)}:${Math.round(row.x / 12)}:${Math.round(row.w / 12)}:${Math.round(row.h / 12)}:${row.kind}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(row);
  }
  return out;
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1200 },
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    extraHTTPHeaders: { 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8' },
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 90000 });
  await page.waitForTimeout(2500);
  const metrics = await page.evaluate((maxItems) => {
    const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim();
    const uniqueRows = (rows) => {
      const seen = new Set();
      const out = [];
      for (const row of rows) {
        const key = `${Math.round(row.y / 12)}:${Math.round(row.x / 12)}:${Math.round(row.w / 12)}:${Math.round(row.h / 12)}:${row.kind}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(row);
      }
      return out;
    };
    const headings = [...document.querySelectorAll('h2,h3,h4')]
      .map((el) => {
        const r = el.getBoundingClientRect();
        return { text: clean(el.innerText || ''), y: r.y + scrollY, w: r.width, h: r.height };
      })
      .filter((item) => item.text && item.w > 80 && item.h > 8)
      .sort((a, b) => a.y - b.y);
    const nearestHeading = (y) => {
      let best = '';
      for (const heading of headings) {
        if (heading.y < y - 12) best = heading.text;
        else break;
      }
      return best;
    };
    const textBlocks = [...document.querySelectorAll('h2,h3,h4,p,li,blockquote')]
      .filter((el) => !el.closest('nav.visual-toc, .visual-toc, figure, d-figure, .figure, table, d-code, pre'))
      .map((el) => {
        const r = el.getBoundingClientRect();
        return {
          text: clean(el.innerText || ''),
          sectionTitle: nearestHeading(r.y + scrollY),
          y: r.y + scrollY,
          w: r.width,
          h: r.height,
        };
      })
      .filter((item) => item.text && item.w > 80 && item.h > 8)
      .sort((a, b) => a.y - b.y);
    const nearestAnchor = (y, sectionTitle) => {
      let best = '';
      for (const block of textBlocks) {
        if (block.y >= y - 8) break;
        if (sectionTitle && block.sectionTitle !== sectionTitle) continue;
        best = block.text;
      }
      return best;
    };
    let id = 0;
    const rows = [];
    const visualToc = document.querySelector('nav.visual-toc, .visual-toc');
    if (visualToc) {
      const r = visualToc.getBoundingClientRect();
      if (r.width > 200 && r.height > 100) {
        visualToc.setAttribute('data-maxread-shot', `shot-${id++}`);
        rows.push({
          id: visualToc.getAttribute('data-maxread-shot'),
          kind: 'overview',
          caption: '原网页标题区和可视目录',
          sectionTitle: '',
          anchorText: '',
          x: r.x + scrollX,
          y: r.y + scrollY,
          w: r.width,
          h: r.height,
        });
      }
    }
    const selector = [
      'figure', 'd-figure', '.figure', 'table', 'd-code', 'pre', 'canvas', 'svg', 'img',
      '.chart-section', '.bench-board', '.charts-grid', '.demo-tabs', '.panel-video', '.overflow-x-auto'
    ].join(',');
    for (const el of [...document.querySelectorAll(selector)]) {
      if (el.closest('nav.visual-toc, .visual-toc')) continue;
      const ancestor = el.parentElement && el.parentElement.closest(selector);
      if (ancestor && ancestor !== el && !['CANVAS', 'SVG'].includes(ancestor.tagName)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 260 || r.height < 120) continue;
      if (r.x + scrollX < -200 || r.y + scrollY < 0) continue;
      const tag = el.tagName.toLowerCase();
      if (tag === 'img') {
        const src = String(el.currentSrc || el.src || '').toLowerCase();
        const alt = clean(el.getAttribute?.('alt') || '');
        if (r.width < 300 || r.height < 140) continue;
        if (/logo|icon|avatar|favicon/.test(src) || /logo|icon|avatar/.test(alt.toLowerCase())) continue;
      }
      const text = clean(el.querySelector?.('figcaption,d-title,caption')?.innerText || el.getAttribute?.('aria-label') || el.getAttribute?.('alt') || el.innerText || '');
      if (['svg', 'canvas'].includes(tag) && !text && r.width < 360 && r.height < 180) continue;
      el.setAttribute('data-maxread-shot', `shot-${id++}`);
      const y = r.y + scrollY;
      const sectionTitle = nearestHeading(y);
      rows.push({
        id: el.getAttribute('data-maxread-shot'),
        kind: tag,
        caption: text || `原网页 ${tag} 可视内容`,
        sectionTitle,
        anchorText: nearestAnchor(y, sectionTitle),
        x: r.x + scrollX,
        y,
        w: r.width,
        h: r.height,
      });
    }
    return uniqueRows(rows).sort((a, b) => a.y - b.y).slice(0, maxItems);
  }, maxItems);

  const output = [];
  for (const [index, row] of metrics.entries()) {
    const handle = await page.$(`[data-maxread-shot="${row.id}"]`);
    if (!handle) continue;
    const file = path.join(outDir, `${String(index + 1).padStart(2, '0')}-${safeName(row.kind + '-' + row.caption)}.png`);
    try {
      await handle.screenshot({ path: file, timeout: 30000 });
      output.push({ ...row, path: file });
    } catch (err) {
      // Ignore one bad interactive element; the HTML parser still has text.
    }
  }
  await browser.close();
  process.stdout.write(JSON.stringify(output));
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


def _extract_math(text: str, max_items: int = 80) -> List[str]:
    items = []
    for pattern in [r"\$\$(.*?)\$\$", r"\\\[(.*?)\\\]", r"(?<!\$)\$([^\n$]{2,240})\$(?!\$)"]:
        for match in re.finditer(pattern, text, flags=re.S):
            body = _clean(match.group(1))
            if body and body not in items:
                items.append(body)
                if len(items) >= max_items:
                    return items
    return items


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _clean_math(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip().strip(",")


def _norm_heading(text: str) -> str:
    text = re.sub(r"\s+", " ", html.unescape(text or "")).strip().lower()
    return re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", text)


def _is_useful_text_block(text: str) -> bool:
    if len(text) < 2:
        return False
    normalized = text.strip()
    low_info_patterns = (
        r"(?i)^(we\s+)?find\s+that\s*[\.:;,!?。！？；：]*$",
        r"(?i)^we\s+show\s+that\s*[\.:;,!?。！？；：]*$",
        r"(?i)^the\s+results\s+show\s+that\s*[\.:;,!?。！？；：]*$",
    )
    return not any(re.match(pattern, normalized) for pattern in low_info_patterns)


def _clip_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[TRUNCATED]"


def _clip_inline(text: str, max_chars: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."



def _is_svg_image(data: bytes) -> bool:
    head = data[:500].lstrip().lower()
    return head.startswith(b"<svg") or b"<svg" in head or head.startswith(b"<?xml") and b"<svg" in head


def _is_raster_image(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff") or data.startswith(b"GIF8") or data.startswith(b"RIFF")


def _is_supported_image_file(path: Path) -> bool:
    try:
        return _is_raster_image(path.read_bytes()[:16])
    except OSError:
        return False
