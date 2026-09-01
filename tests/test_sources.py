from maxread.batch import _queue_message
from maxread.db import Store
from maxread.job_queue import QueueItem, _cached_doc
from maxread.sources import canonical_document_url, extract_supported_inputs, is_supported_web_article_url


def test_huggingface_papers_maps_to_arxiv():
    refs, web_refs = extract_supported_inputs("读 https://huggingface.co/papers/2605.18739")
    assert [ref.paper_id for ref in refs] == ["2605.18739"]
    assert web_refs == []


def test_papers_cool_arxiv_url_maps_to_canonical_arxiv_pipeline():
    refs, web_refs = extract_supported_inputs("读 https://papers.cool/arxiv/2608.25479")

    assert [(ref.paper_id, ref.url) for ref in refs] == [
        ("2608.25479", "https://arxiv.org/abs/2608.25479")
    ]
    assert web_refs == []
    assert is_supported_web_article_url("https://papers.cool/arxiv/2608.25479") is False


def test_papers_cool_version_and_query_deduplicate_plain_arxiv_id():
    refs, web_refs = extract_supported_inputs(
        "2608.25479 https://papers.cool/arxiv/2608.25479v2?from=feed"
    )

    assert [ref.paper_id for ref in refs] == ["2608.25479"]
    assert web_refs == []


def test_arxiv_html_link_maps_to_paper_not_web_article():
    refs, web_refs = extract_supported_inputs("读 https://arxiv.org/html/2503.08067v1")
    assert [(ref.paper_id, ref.url) for ref in refs] == [("2503.08067", "https://arxiv.org/pdf/2503.08067v1")]
    assert web_refs == []


def test_regular_url_becomes_web_ref():
    refs, web_refs = extract_supported_inputs("看 https://nrehiew.github.io/blog/sft_rl_opd/")
    assert refs == []
    assert [ref.url for ref in web_refs] == ["https://nrehiew.github.io/blog/sft_rl_opd/"]


def test_markdown_wrapped_url_is_cleaned_and_deduped():
    text = (
        "这篇我没读成：[https://transformer-circuits.pub/2026/workspace/index.html]"
        "(https://transformer-circuits.pub/2026/workspace/index.html)]([https://transformer-circuits.pub/2026/workspace/index.html]"
        "(https://transformer-circuits.pub/2026/workspace/index.html)"
    )

    refs, web_refs = extract_supported_inputs(text)

    assert refs == []
    assert [ref.url for ref in web_refs] == ["https://transformer-circuits.pub/2026/workspace/index.html"]


def test_direct_pdf_and_feishu_docs_are_not_web_articles():
    refs, web_refs = extract_supported_inputs(
        "看看 https://openreview.net/pdf?id=nM5tDHrQsx 和 https://tenant.feishu.cn/docx/AbCdEf"
    )
    assert refs == []
    assert web_refs == []


def test_github_blob_pdf_becomes_document_paper_ref():
    refs, web_refs = extract_supported_inputs(
        "读 https://github.com/QwenLM/Qwen3.8-Flash-Next/blob/main/tech_report.pdf?spm=tracking&file=tech_report.pdf"
    )
    assert len(refs) == 1
    assert refs[0].paper_id.startswith("gh-QwenLM-Qwen3.8-Flash-Next-tech_report-")
    assert refs[0].url == "https://raw.githubusercontent.com/QwenLM/Qwen3.8-Flash-Next/main/tech_report.pdf"
    assert web_refs == []


def test_huggingface_model_repo_becomes_document_paper_ref():
    refs, web_refs = extract_supported_inputs(
        "读 https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
    )
    assert len(refs) == 1
    assert refs[0].paper_id.startswith("hf-deepseek-ai-DeepSeek-V4-Flash-Vision-Exp-")
    assert web_refs == []


def test_document_canonicalization_drops_tracking_query():
    assert canonical_document_url(
        "https://github.com/a/b/blob/main/report.pdf?spm=x&file=report.pdf"
    ) == "https://raw.githubusercontent.com/a/b/main/report.pdf"


def test_queue_message_includes_order_and_wait():
    refs, web_refs = extract_supported_inputs("2605.15980 https://nrehiew.github.io/blog/sft_rl_opd/")
    msg = _queue_message([("paper", refs[0]), ("article", web_refs[0])], workers=1)
    assert "排队顺序" in msg
    assert "2605.15980" in msg
    assert "第 2 批" in msg
    assert "预计等待" in msg


def test_queue_cached_doc_for_paper(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.upsert_paper("2604.12946", "done", title="T", doc_url="https://doc")
    cached = _cached_doc(store, QueueItem("paper", "2604.12946", "https://arxiv.org/abs/2604.12946", "2604.12946"))
    assert cached == ("https://doc", "T")
    store.close()


def test_feishu_login_trap_is_never_treated_as_web_article():
    url = (
        "https://login.feishu.cn/accounts/trap?app_id=2&query_scope=all&"
        "redirect_uri=https%3A%2F%2Ftenant.feishu.cn%2Fdocx%2Fdoc"
    )

    papers, articles = extract_supported_inputs(f"重试 2410.06205\n失败原因：{url}")

    assert [paper.paper_id for paper in papers] == ["2410.06205"]
    assert articles == []
    assert is_supported_web_article_url(url) is False
