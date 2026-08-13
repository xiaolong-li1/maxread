from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Iterable, List, Tuple

from .models import PaperBundle


CODE_ANCHOR_WORDS = (
    "code",
    "github",
    "gitlab",
    "repository",
    "repo",
    "source",
    "implementation",
    "代码",
    "仓库",
    "源码",
)

PROJECT_CONTEXT_WORDS = (
    "project page",
    "project website",
    "official project",
    "code is available",
    "code available",
    "repository is available",
    "项目主页",
    "项目网站",
    "代码仓库",
)


def find_repository_url(bundle: PaperBundle) -> str:
    text = _bundle_link_text(bundle)
    urls = _extract_urls(text)
    direct = _best_direct_repo_in_text(text)
    if direct:
        return direct
    project_pages = [
        url for url in urls
        if _is_project_page_url(url) or _has_project_page_context(text, url)
    ]
    resolved = resolve_repository_from_pages(project_pages)
    if resolved:
        return resolved
    return ""


def resolve_repository_from_pages(urls: Iterable[str]) -> str:
    seen = set()
    limit = _page_resolve_limit()
    checked = 0
    for url in urls:
        normalized = _clean_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        checked += 1
        if checked > limit:
            break
        html = _fetch_html(normalized)
        if not html:
            continue
        resolved = _best_repo_from_html(html, normalized)
        if resolved:
            return resolved
    return ""


def _bundle_link_text(bundle: PaperBundle) -> str:
    source_text = _strip_tex_comments(_strip_reference_file_chunks(bundle.source_text or ""))
    primary_text = source_text if source_text.strip() else _strip_pdf_reference_tail(bundle.pdf_text or "")
    parts = [
        primary_text,
        "\n".join(_strip_tex_comments(caption) for caption in bundle.source_captions or []),
        bundle.source_tree or "",
    ]
    return "\n".join(parts)


def _strip_reference_file_chunks(text: str) -> str:
    """Exclude bibliography files from repository discovery.

    Bibliographies contain repositories for cited datasets and models, not
    necessarily for the paper being summarized.
    """
    lines = []
    skip = False
    for line in (text or "").splitlines():
        match = re.match(r"\s*%\s*FILE:\s*(.+?)\s*$", line, flags=re.I)
        if match:
            suffix = os.path.splitext(match.group(1).strip())[1].lower()
            skip = suffix in {".bib", ".bbl"}
        if not skip:
            lines.append(line)
    return "\n".join(lines)


def _strip_tex_comments(text: str) -> str:
    """Remove TeX comments while preserving escaped literal percent signs."""
    lines = []
    for line in (text or "").splitlines():
        for index, char in enumerate(line):
            if char != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                line = line[:index]
                break
        lines.append(line)
    return "\n".join(lines)


def _strip_pdf_reference_tail(text: str) -> str:
    """Exclude cited-project links from repository discovery in PDF fallback text."""
    value = str(text or "")
    if not value:
        return ""
    heading = re.compile(
        r"(?im)^\s*(?:r\s*e\s*f\s*e\s*r\s*e\s*n\s*c\s*e\s*s|bibliography)\s*$"
    )
    cutoff_floor = int(len(value) * 0.35)
    for match in heading.finditer(value):
        if match.start() >= cutoff_floor:
            return value[: match.start()]
    return value


def _extract_urls(text: str) -> List[str]:
    urls: List[str] = []
    seen = set()
    for match in re.finditer(r"https?://[^\s<>{}\\\"'`]+", text or ""):
        url = _clean_url(match.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _clean_url(url: str) -> str:
    url = str(url or "").strip()
    url = url.rstrip(").,;:!?，。；：）]")
    url = url.replace("&amp;", "&")
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _best_direct_repo_url(urls: Iterable[str]) -> str:
    candidates = [_canonical_repo_url(url) for url in urls]
    candidates = [url for url in candidates if url]
    if not candidates:
        return ""
    for url in candidates:
        if "github.com/" in url.lower() or "gitlab.com/" in url.lower():
            return url
    return candidates[0]


def _best_direct_repo_in_text(text: str) -> str:
    """Return only a repository URL explicitly identified as project code.

    TeX sources often retain template, author-profile, and cited-project URLs.
    A bare GitHub URL is therefore insufficient evidence that it is the paper's
    implementation.
    """
    candidates: List[Tuple[int, int, str]] = []
    for index, match in enumerate(re.finditer(r"https?://[^\s<>{}\\\"'`]+", text or "")):
        repo = _canonical_repo_url(match.group(0))
        if not repo:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        context = line[:match.start() - line_start] + " " + line[match.end() - line_start:]
        lower = context.lower()
        repo_lower = repo.lower()
        score = 0
        if re.search(r"\b(?:our\s+)?code\s+(?:is\s+)?(?:available|released|open[- ]sourced)\b", lower):
            score += 200
        if re.search(r"\b(?:source\s+code|official\s+implementation|our\s+implementation|codebase|repository|repo)\b", lower):
            score += 100
        if line.lstrip().startswith(("%", "//", "#")):
            score -= 80
        if "template" in lower or "template" in repo_lower:
            score -= 300
        if score > 0:
            candidates.append((score, -index, repo))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][2]


def _best_repo_from_html(html: str, base_url: str) -> str:
    parser = _AnchorParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    scored: List[Tuple[int, str]] = []
    for href, text in parser.links:
        url = urllib.parse.urljoin(base_url, href)
        repo = _canonical_repo_url(url)
        if not repo:
            continue
        label = f"{text} {href}".lower()
        score = 0
        if any(word in label for word in CODE_ANCHOR_WORDS):
            score += 100
        if "github.com/" in repo.lower() or "gitlab.com/" in repo.lower():
            score += 20
        scored.append((score, repo))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored[0][0] > 0:
            return scored[0][1]

    # Some project pages put links in scripts or badges instead of anchors.
    return _best_direct_repo_url(_extract_urls(html))


def _canonical_repo_url(url: str) -> str:
    url = _clean_url(url)
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}:
        if len(path_parts) < 2:
            return ""
        owner, repo = path_parts[0], path_parts[1]
        if owner.lower() in {"topics", "features", "collections", "marketplace", "explore"}:
            return ""
        if repo.lower() in {"issues", "pulls", "projects", "stars", "followers", "following"}:
            return ""
        return f"{parsed.scheme or 'https'}://{host}/{owner}/{repo}"
    if host == "huggingface.co":
        if len(path_parts) >= 2 and path_parts[0] not in {"papers", "docs", "spaces"}:
            return f"{parsed.scheme or 'https'}://{host}/{path_parts[0]}/{path_parts[1]}"
        return ""
    if host == "sourceforge.net" and len(path_parts) >= 2:
        return urllib.parse.urlunparse((parsed.scheme or "https", host, parsed.path.rstrip("/"), "", "", ""))
    return ""


def _is_repository_or_project_url(url: str) -> bool:
    if _canonical_repo_url(url):
        return True
    return _is_project_page_url(url)


def _is_project_page_url(url: str) -> bool:
    lower = url.lower()
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if any(blocked in host for blocked in (
        "arxiv.org",
        "openreview.net",
        "doi.org",
        "aclanthology.org",
        "paperswithcode.com",
        "fonts.googleapis.com",
        "cdn.jsdelivr.net",
    )):
        return False
    return any(word in lower for word in (
        "github.io",
        "/project",
        "project.",
    ))


def _has_project_page_context(text: str, url: str) -> bool:
    if not text or not url:
        return False
    lower_text = text.lower()
    candidates = {url.lower(), _clean_url(url).lower()}
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme == "http":
        candidates.add(urllib.parse.urlunparse(("https", parsed.netloc, parsed.path, "", parsed.query, "")).lower())
    elif parsed.scheme == "https":
        candidates.add(urllib.parse.urlunparse(("http", parsed.netloc, parsed.path, "", parsed.query, "")).lower())
    for candidate in candidates:
        index = lower_text.find(candidate)
        if index < 0:
            continue
        start = max(0, index - 160)
        end = min(len(lower_text), index + len(candidate) + 80)
        context = lower_text[start:end]
        if any(word in context for word in PROJECT_CONTEXT_WORDS):
            return True
    return False


def _fetch_html(url: str) -> str:
    timeout = _page_resolve_timeout()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MaxRead/0.1 repository resolver",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if content_type and "html" not in content_type.lower():
                return ""
            data = response.read(1_000_000)
    except Exception:
        return ""
    return data.decode("utf-8", errors="replace")


def _page_resolve_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("MAXREAD_REPO_RESOLVE_TIMEOUT", "6")))
    except ValueError:
        return 6.0


def _page_resolve_limit() -> int:
    try:
        return max(0, int(os.environ.get("MAXREAD_REPO_RESOLVE_PAGES", "4")))
    except ValueError:
        return 4


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: List[Tuple[str, str]] = []
        self._href = ""
        self._text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href", "")
        if href:
            self._href = href
            self._text_parts = []

    def handle_data(self, data: str):
        if self._href:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() != "a" or not self._href:
            return
        text = " ".join(" ".join(self._text_parts).split())
        self.links.append((self._href, text))
        self._href = ""
        self._text_parts = []
