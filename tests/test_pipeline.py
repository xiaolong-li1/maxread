from pathlib import Path

from maxread.db import Store
from maxread.models import ArxivMetadata, PaperBundle, PaperRef
from maxread.pipeline import MaxReadPipeline


class FakeArxiv:
    def fetch(self, paper_id):
        return PaperBundle(
            metadata=ArxivMetadata(
                paper_id=paper_id,
                title="Fake Paper",
                authors=["A", "B"],
                summary="A fake abstract.",
                published="2026-01-01T00:00:00Z",
                updated="2026-01-02T00:00:00Z",
                categories=["cs.CL"],
                pdf_url=f"https://arxiv.org/pdf/{paper_id}.pdf",
                abs_url=f"https://arxiv.org/abs/{paper_id}",
            ),
            pdf_path=Path("fake.pdf"),
            source_path=Path("fake.source"),
            source_dir=Path("source"),
            source_text="\\section{Method} fake method",
            pdf_text="fake pdf",
            source_tree="main.tex\nfigures/overview.png",
            source_assets=["figures/overview.png"],
            source_captions=["An overview figure."],
            parse_warnings=[],
        )


class FakeFeishu:
    def __init__(self):
        self.published = []

    def create_docx(self, title):
        return {"url": "https://tenant.feishu.cn/docx/doc123", "token": "doc123"}

    def overwrite_docx(self, doc_url, markdown):
        assert "Fake Paper" in markdown or "A fake abstract" in markdown
        return {"ok": True}

    def overwrite_docx_xml(self, doc_url, xml):
        assert "Fake Paper" in xml or "A fake abstract" in xml
        return {"ok": True}

    def insert_image(self, doc_url, image_path, caption="", selection="", width=720):
        return {"ok": True}

    def remove_text(self, doc_url, text):
        return {"ok": True}

    def publish_docx(self, token):
        self.published.append(token)
        return {"ok": True}


class FakeLLM:
    def responses_text(self, system, user):
        return "# Fake Paper\n\n一句话总结：A fake abstract."


class FakeArxivNoSource(FakeArxiv):
    def fetch(self, paper_id):
        bundle = super().fetch(paper_id)
        bundle.source_text = ""
        bundle.source_path = None
        bundle.parse_warnings = ["TeX source download failed: HTTP 429"]
        return bundle


def test_pipeline_process_and_cache(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = FakeFeishu()
    pipeline = MaxReadPipeline(store, FakeArxiv(), feishu, FakeLLM(), require_source=True)
    ref = PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946")

    first = pipeline.process_ref(ref)
    assert first.doc_url == "https://tenant.feishu.cn/docx/doc123"
    assert first.cached is False
    assert feishu.published == ["doc123"]

    second = pipeline.process_ref(ref)
    assert second.doc_url == first.doc_url
    assert second.cached is True
    assert feishu.published == ["doc123"]
    store.close()


def test_pipeline_requires_source(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = FakeFeishu()
    pipeline = MaxReadPipeline(store, FakeArxivNoSource(), feishu, FakeLLM(), require_source=True)
    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))
    assert result.doc_url == ""
    assert "需要 TeX source" in result.error
    assert feishu.published == []
    record = store.get_paper("2604.12946")
    assert record.status == "needs_source"
    store.close()
