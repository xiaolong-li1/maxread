from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import ArticleBundle, ArticleImage, ArticleSection


class WebArticleClient:
    def __init__(self, workdir: Path, timeout: int = 45):
        self.workdir = workdir
        self.timeout = timeout

    def fetch(self, url: str) -> ArticleBundle:
        article_id = article_id_for_url(url)
        article_dir = self.workdir / "articles" / article_id
        article_dir.mkdir(parents=True, exist_ok=True)
        html_text = self._get_text(url)
        parser = ArticleHTMLParser(url)
        parser.feed(html_text)
        parser.close()
        images = self._download_images(parser.images, article_dir)
        text = parser.main_text(images)
        return ArticleBundle(
            article_id=article_id,
            url=url,
            title=parser.title or url,
            author=parser.meta.get("author", ""),
            published=parser.meta.get("article:published_time", "") or parser.meta.get("date", ""),
            site_name=parser.meta.get("og:site_name", ""),
            text=text,
            sections=parser.sections,
            section_blocks=parser.section_blocks(images),
            images=images,
            tables=parser.tables,
            code_blocks=parser.code_blocks,
            math_blocks=_extract_math(html_text + "\n" + text),
            warnings=[],
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


StructuredBlock = Tuple[str, int, str]


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
        self._current_text: List[str] = []
        self._current_code: List[str] = []
        self._current_table: List[str] = []
        self._current_caption: List[str] = []
        self._last_figure_image: Optional[ArticleImage] = None
        self.blocks: List[str] = []
        self._structured_blocks: List[StructuredBlock] = []
        self.sections: List[str] = []
        self.images: List[ArticleImage] = []
        self.tables: List[str] = []
        self.code_blocks: List[str] = []

    def handle_starttag(self, tag: str, attrs_list):
        attrs = dict(attrs_list)
        self._stack.append(tag)
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
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "blockquote"}:
            self._current_text = []
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
            src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-original")
            if src:
                image = ArticleImage(url=urllib.parse.urljoin(self.base_url, src), local_path=None, alt=attrs.get("alt", ""), source_index=len(self.images) + 1)
                self.images.append(image)
                marker_text = f"[ArticleImage:{image.source_index}] {image.alt}".strip()
                self.blocks.append(marker_text)
                self._structured_blocks.append(("image", image.source_index, image.alt))
                if "figure" in self._stack:
                    self._last_figure_image = image

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "nav", "footer", "header", "aside", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._capture_title = False
            if not self.title:
                self.title = _clean(" ".join(self._current_text))
        if self._skip_depth:
            self._pop(tag)
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "blockquote"}:
            text = _clean(" ".join(self._current_text))
            if len(text) >= 2:
                if tag in {"h1", "h2", "h3", "h4"}:
                    level = int(tag[1])
                    self.sections.append(text)
                    self.blocks.append(f"## {text}")
                    self._structured_blocks.append(("heading", level, text))
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
        if self._skip_depth:
            return
        if self._capture_title:
            self._current_text.append(data)
        if self._capture_code:
            self._current_code.append(data)
        if self._capture_table:
            self._current_table.append(data)
        if self._capture_figcaption:
            self._current_caption.append(data)
        if self._stack and self._stack[-1] in {"p", "li", "h1", "h2", "h3", "h4", "blockquote"}:
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

    def _update_image_caption(self, source_index: int, caption: str) -> None:
        updated: List[StructuredBlock] = []
        for kind, value, text in self._structured_blocks:
            if kind == "image" and value == source_index:
                updated.append((kind, value, caption))
            else:
                updated.append((kind, value, text))
        self._structured_blocks = updated

    def _pop(self, tag: str) -> None:
        if tag in self._stack[::-1]:
            idx = len(self._stack) - 1 - self._stack[::-1].index(tag)
            self._stack = self._stack[:idx]


def article_id_for_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


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


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _clip_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[TRUNCATED]"



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
