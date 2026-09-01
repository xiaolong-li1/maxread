from __future__ import annotations

import re
from hashlib import sha256
import os
import urllib.parse
from dataclasses import dataclass
from typing import List

from .arxiv import extract_arxiv_refs
from .models import PaperRef


URL_RE = re.compile(r"https?://[^\s<>'\"\]\)]+", re.IGNORECASE)
HF_PAPER_RE = re.compile(r"https?://huggingface\.co/papers/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
PAPERS_COOL_RE = re.compile(
    r"https?://(?:www\.)?papers\.cool/arxiv/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?:[/?#][^\s<>'\"\]\)]*)?",
    re.IGNORECASE,
)
FEISHU_DOC_RE = re.compile(r"https?://[^/]*(?:feishu|larksuite)\.[^/]+/(?:docx|docs|wiki|sheets|base|mindnotes|minutes)/", re.IGNORECASE)
AUTH_REDIRECT_HOSTS = {"login.feishu.cn", "passport.feishu.cn", "accounts.larksuite.com"}
HF_RESERVED_ROOTS = {
    "api", "blog", "collections", "datasets", "docs", "join", "login", "models",
    "organizations", "papers", "pricing", "settings", "spaces", "tasks",
}
DOCUMENT_SOURCE_HOSTS = {
    "github.com", "www.github.com", "raw.githubusercontent.com", "media.githubusercontent.com",
    "user-images.githubusercontent.com", "huggingface.co", "cdn-lfs.huggingface.co",
    "cdn-avatars.huggingface.co",
}


@dataclass(frozen=True)
class WebRef:
    url: str


def extract_supported_inputs(text: str) -> tuple[List[PaperRef], List[WebRef]]:
    refs = extract_arxiv_refs(text)
    seen_papers = {ref.paper_id for ref in refs}
    for pattern in (HF_PAPER_RE, PAPERS_COOL_RE):
        for match in pattern.finditer(text):
            paper_id = match.group("id")
            if paper_id not in seen_papers:
                refs.append(PaperRef(paper_id=paper_id, url=f"https://arxiv.org/abs/{paper_id}"))
                seen_papers.add(paper_id)
    seen_document_urls = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;，。；）)]")
        if not is_document_source_url(url):
            continue
        canonical_url = canonical_document_url(url)
        if canonical_url in seen_document_urls:
            continue
        refs.append(PaperRef(paper_id=document_source_id(canonical_url), url=canonical_url))
        seen_document_urls.add(canonical_url)
    web_refs: List[WebRef] = []
    seen_urls = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;，。；）)]")
        if _is_paper_url(url):
            continue
        if not is_supported_web_article_url(url):
            continue
        if url not in seen_urls:
            web_refs.append(WebRef(url=url))
            seen_urls.add(url)
    return refs, web_refs


def is_document_source_url(url: str) -> bool:
    """Return whether a URL is a technical document source handled as a paper.

    This check is deliberately syntax-only. Network resolution happens in the
    document adapter on the worker, keeping message ingestion fast and durable.
    """
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    path = urllib.parse.unquote(parsed.path or "")
    lower_path = path.lower().rstrip("/")
    if host in {"github.com", "www.github.com"}:
        parts = [part for part in path.split("/") if part]
        return len(parts) >= 5 and parts[2] in {"blob", "raw"} and lower_path.endswith(".pdf")
    if host in {"raw.githubusercontent.com", "media.githubusercontent.com"}:
        return lower_path.endswith(".pdf")
    if host == "huggingface.co":
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[0].lower() not in HF_RESERVED_ROOTS:
            return True
        return lower_path.endswith(".pdf")
    configured_hosts = {
        item.strip().lower()
        for item in os.environ.get("MAXREAD_DOCUMENT_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    if host not in DOCUMENT_SOURCE_HOSTS | configured_hosts:
        return False
    if lower_path.endswith(".pdf") or lower_path.endswith("/pdf"):
        return True
    query = urllib.parse.parse_qs(parsed.query)
    return any(str(value).lower().endswith(".pdf") for values in query.values() for value in values)


def canonical_document_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    path = urllib.parse.unquote(parsed.path or "")
    if host in {"github.com", "www.github.com"}:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 5 and parts[2] in {"blob", "raw"}:
            owner, repo, _kind, revision = parts[:4]
            asset = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{revision}/{asset}"
    if host == "huggingface.co":
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 5 and parts[2] in {"blob", "resolve"}:
            owner, repo, _kind, revision = parts[:4]
            asset = "/".join(parts[4:])
            return f"https://huggingface.co/{owner}/{repo}/resolve/{revision}/{asset}"
        if len(parts) == 2:
            return f"https://huggingface.co/{parts[0]}/{parts[1]}"
    clean_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "spm" or key.lower().startswith("utm_"):
            continue
        clean_query.append((key, value))
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", urllib.parse.urlencode(clean_query), ""))


def document_source_id(url: str) -> str:
    canonical = canonical_document_url(url)
    parsed = urllib.parse.urlparse(canonical)
    parts = [part for part in urllib.parse.unquote(parsed.path).split("/") if part]
    if parsed.hostname == "huggingface.co" and len(parts) >= 2:
        prefix = f"hf-{parts[0]}-{parts[1]}"
    elif parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
        prefix = f"gh-{parts[0]}-{parts[1]}-{_path_stem(parts[-1])}"
    else:
        prefix = f"pdf-{_path_stem(parts[-1] if parts else 'document')}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", prefix).strip("-._")[:72] or "document"
    return f"{slug}-{sha256(canonical.encode('utf-8')).hexdigest()[:10]}"


def _path_stem(name: str) -> str:
    value = str(name or "document").rsplit("/", 1)[-1]
    return value[:-4] if value.lower().endswith(".pdf") else value


def is_supported_web_article_url(url: str) -> bool:
    lower = url.lower()
    if FEISHU_DOC_RE.search(lower):
        return False
    if PAPERS_COOL_RE.search(url):
        return False
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host in AUTH_REDIRECT_HOSTS or path.startswith("/accounts/trap"):
        return False
    if path.endswith(".pdf") or path.rstrip("/").endswith("/pdf"):
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_paper_url(url: str) -> bool:
    lower = url.lower()
    return (
        "arxiv.org/abs/" in lower
        or "arxiv.org/pdf/" in lower
        or "arxiv.org/html/" in lower
        or "huggingface.co/papers/" in lower
        or "papers.cool/arxiv/" in lower
        or is_document_source_url(url)
    )
