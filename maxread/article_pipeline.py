from __future__ import annotations

import traceback
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Optional

from .article_prompts import ARTICLE_SYSTEM_PROMPT, build_article_user_prompt
from .db import Store
from .feishu import FeishuClient
from .models import ArticleBundle, FeishuEvent
from .openai_client import OpenAIClient
from .publishing import publish_marker_image
from .render import markdown_to_docx_xml, polish_markdown
from .review import review_markdown_with_report
from .sources import WebRef
from .web_article import WebArticleClient


@dataclass
class ArticleProcessResult:
    article_id: str
    doc_url: str
    cached: bool
    error: str = ""


class ArticlePipeline:
    def __init__(self, store: Store, web: WebArticleClient, feishu: FeishuClient, llm: Optional[OpenAIClient]):
        self.store = store
        self.web = web
        self.feishu = feishu
        self.llm = llm

    def process_ref(self, ref: WebRef, event: Optional[FeishuEvent] = None, send_progress: bool = True) -> ArticleProcessResult:
        article_id = sha256(ref.url.encode("utf-8")).hexdigest()[:16]
        record = self.store.get_document(article_id)
        if record and record.status == "done" and record.doc_url:
            if event and send_progress:
                self._reply(event, f"哥，之前的文档在这里 {record.doc_url}", "cached", article_id)
            return ArticleProcessResult(article_id, record.doc_url, cached=True)
        try:
            if event and send_progress:
                self._reply(event, "[了解] 收到了：网页文章", "start", article_id)
                self._reply(event, f"[下载中] 正在抓取网页：{_host(ref.url)}", "downloading", article_id)
            self.store.upsert_document(article_id, "fetching", kind="article", source_url=ref.url)
            bundle = self.web.fetch(ref.url)
            self.store.upsert_document(article_id, "summarizing", kind="article", source_url=ref.url, title=bundle.title)
            if event and send_progress:
                self._reply(event, f"[在做了] 正在读文章：{_clip(bundle.title, 40)}", "reading", article_id)

            try:
                if not self.llm:
                    raise RuntimeError("OPENAI_API_KEY not configured or --no-openai was used")
                image_inserts = _image_placeholders(bundle)
                bundle.text = _replace_article_image_markers(bundle.text, image_inserts)
                markdown = self.llm.responses_text(ARTICLE_SYSTEM_PROMPT, build_article_user_prompt(bundle, image_inserts))
                markdown = polish_markdown(markdown)
                markers = [marker for marker, _path, _caption, _source_index in image_inserts]
                review_warnings = []
                if event and send_progress:
                    self._reply(event, f"[审阅中] 正在审阅/修订：{_clip(bundle.title or '网页文章', 40)}", "reviewing", article_id)
                try:
                    review = review_markdown_with_report(self.llm, markdown, markers, kind="article")
                    markdown = review.markdown
                    self.store.add_review_issues("article", article_id, review.issues)
                    for issue in review.issues:
                        review_warnings.append(f"review:{issue.category}:{issue.severity}:{issue.detail}")
                except Exception as review_exc:
                    review_warnings.append(f"Review pass failed: {review_exc}")
                markdown = polish_markdown(markdown)
                missing_markers = [marker for marker in markers if marker not in markdown]
                publish_warnings = review_warnings + [f"missing-marker:{marker}" for marker in missing_markers]
                xml = markdown_to_docx_xml(markdown)
            except Exception as exc:
                message = f"文章总结模型调用失败，未发布文档：{exc}"
                self.store.upsert_document(article_id, "summary_failed", error=message)
                if event and send_progress:
                    self._reply(event, f"这篇我没读成：{ref.url}\n原因：{message}", "summary-fail", article_id)
                return ArticleProcessResult(article_id, "", cached=False, error=message)

            self.store.upsert_document(article_id, "writing_doc")
            if event and send_progress:
                self._reply(event, "[敲键盘] 在写飞书文档", "writing", article_id)
            doc = self.feishu.create_docx(bundle.title or ref.url)
            self.feishu.overwrite_docx_xml(doc["url"], xml)
            warnings = list(publish_warnings)
            for marker, image_path, caption, _source_index in image_inserts:
                if marker not in markdown:
                    continue
                publish_result = publish_marker_image(self.feishu, doc["url"], image_path, caption, marker)
                warnings.extend(publish_result.warnings)
            self.feishu.publish_docx(doc["token"])
            self.store.upsert_document(article_id, "done", doc_url=doc["url"], doc_token=doc["token"], error="; ".join(warnings))
            if event and send_progress:
                self._reply(event, f"哥，读完了：{doc['url']}", "done", article_id)
            return ArticleProcessResult(article_id, doc["url"], cached=False)
        except Exception as exc:
            error = f"{exc}\n{traceback.format_exc()}"
            self.store.upsert_document(article_id, "failed", error=error)
            if event and send_progress:
                self._reply(event, f"这篇我没读成：{ref.url}\n原因：{_clip(str(exc), 500)}", "fail", article_id)
            return ArticleProcessResult(article_id, "", cached=False, error=str(exc))

    def _reply(self, event: FeishuEvent, text: str, prefix: str, article_id: str) -> None:
        stage = _progress_stage(prefix)
        if stage:
            try:
                self.feishu.react_progress(event.message_id, stage)
            except Exception:
                pass
            return
        key = sha256(f"article:{prefix}:{event.event_id}:{article_id}".encode("utf-8")).hexdigest()[:32]
        try:
            self.feishu.reply_text(event.message_id, _clip(text, 900), idempotency_key=key)
        except Exception:
            pass


def _progress_stage(prefix: str) -> str:
    return prefix if prefix in {"start", "downloading", "reading", "reviewing", "writing"} else ""


def _image_placeholders(bundle: ArticleBundle):
    inserts = []
    index = 0
    for image in bundle.images:
        if image.local_path and image.local_path.exists():
            index += 1
            marker = f"[MaxReadFigure:{index}:{image.local_path.stem}]"
            inserts.append((marker, image.local_path, image.caption or image.alt or image.url, image.source_index))
    return inserts[:16]


def _replace_article_image_markers(text: str, image_inserts) -> str:
    markers_by_source = {str(source_index): (marker, caption) for marker, _path, caption, source_index in image_inserts}

    def repl(match):
        source_index = match.group(1)
        fallback_caption = match.group(2).strip()
        item = markers_by_source.get(source_index)
        if not item:
            return fallback_caption
        marker, real_caption = item
        return f"{marker}\n**图：{real_caption or fallback_caption}**"

    return __import__("re").sub(r"\[ArticleImage:(\d+)\]\s*(.*)", repl, text)


def _host(url: str) -> str:
    return url.split("/", 3)[2] if "://" in url else url


def _clip(text: str, max_chars: int) -> str:
    text = str(text)
    return text if len(text) <= max_chars else text[:max_chars] + "..."
