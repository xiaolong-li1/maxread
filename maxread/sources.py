from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import List

from .arxiv import extract_arxiv_refs
from .models import PaperRef


URL_RE = re.compile(r"https?://[^\s<>'\"\]\)]+", re.IGNORECASE)
HF_PAPER_RE = re.compile(r"https?://huggingface\.co/papers/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
FEISHU_DOC_RE = re.compile(r"https?://[^/]*(?:feishu|larksuite)\.[^/]+/(?:docx|docs|wiki|sheets|base|mindnotes|minutes)/", re.IGNORECASE)


@dataclass(frozen=True)
class WebRef:
    url: str


def extract_supported_inputs(text: str) -> tuple[List[PaperRef], List[WebRef]]:
    refs = extract_arxiv_refs(text)
    seen_papers = {ref.paper_id for ref in refs}
    for match in HF_PAPER_RE.finditer(text):
        paper_id = match.group("id")
        if paper_id not in seen_papers:
            refs.append(PaperRef(paper_id=paper_id, url=f"https://arxiv.org/abs/{paper_id}"))
            seen_papers.add(paper_id)
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


def is_supported_web_article_url(url: str) -> bool:
    lower = url.lower()
    if FEISHU_DOC_RE.search(lower):
        return False
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
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
    )
