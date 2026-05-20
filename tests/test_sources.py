from maxread.batch import _queue_message
from maxread.db import Store
from maxread.job_queue import QueueItem, _cached_doc
from maxread.sources import extract_supported_inputs


def test_huggingface_papers_maps_to_arxiv():
    refs, web_refs = extract_supported_inputs("读 https://huggingface.co/papers/2605.18739")
    assert [ref.paper_id for ref in refs] == ["2605.18739"]
    assert web_refs == []


def test_regular_url_becomes_web_ref():
    refs, web_refs = extract_supported_inputs("看 https://nrehiew.github.io/blog/sft_rl_opd/")
    assert refs == []
    assert [ref.url for ref in web_refs] == ["https://nrehiew.github.io/blog/sft_rl_opd/"]


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
