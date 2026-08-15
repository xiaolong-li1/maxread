from pathlib import Path

from maxread.db import Store
from maxread.models import ArxivMetadata, PaperBundle, PaperRef
from maxread.pipeline import MaxReadPipeline, _describe_figures_for_prompt, _generate_complete_paper_markdown, _sanitize_repository_markdown, _write_paper_artifact
from maxread.visual_qa import VisualQAController


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
        self.fetched = []

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

    def fetch_docx(self, doc_url, doc_format="xml", detail="simple"):
        self.fetched.append((doc_url, doc_format, detail))
        return {"data": {"document": {"content": "<title>Fake Paper</title><p>ok</p>"}}}


class FetchFailFeishu(FakeFeishu):
    def fetch_docx(self, *args, **kwargs):
        raise RuntimeError("fetch unavailable")


class InvalidPublishedFormulaFeishu(FakeFeishu):
    def fetch_docx(self, doc_url, doc_format="xml", detail="simple"):
        return {
            "data": {
                "document": {
                    "content": r"<title>Fake Paper</title><p><latex>a=1,\quadw=2</latex></p>"
                }
            }
        }


class RepairablePublishedFormulaFeishu(FakeFeishu):
    def __init__(self):
        super().__init__()
        self.repaired = False
        self.replacements = []

    def fetch_docx(self, doc_url, doc_format="xml", detail="simple"):
        formula = r"a=1,\quad{}w=2" if self.repaired else r"a=1,\quadw=2"
        return {
            "data": {
                "document": {
                    "content": f'<title>Fake Paper</title><p id="formula"><latex>{formula}</latex></p>'
                }
            }
        }

    def block_replace(self, doc_url, block_id, content):
        self.replacements.append((block_id, content))
        self.repaired = True
        return {"ok": True}


class FakeLLM:
    def responses_text(self, system, user, **kwargs):
        body = "# Fake Paper\n\n**TL;DR**：A fake abstract.\n\n"
        body += "\n\n".join(f"## {number}. Section {number}\n\n" + ("完整正文。" * 70) for number in range(1, 8))
        return body


class FakeVisionLLM(FakeLLM):
    def responses_image_text(self, system, user, image_path):
        assert "caption:" in user
        assert str(image_path).endswith("figure.png")
        return "图中显示两个相连模块和一条从输入到输出的箭头。"


class BadQualityLLM(FakeLLM):
    def responses_text(self, system, user, **kwargs):
        body = super().responses_text(system, user, **kwargs)
        return body + r"\n\n公式：<latex>\newcommand{\RR}{\mathbb{R}} x\in\RR</latex>"


class MissingH1LLM(FakeLLM):
    def responses_text(self, system, user, **kwargs):
        return super().responses_text(system, user, **kwargs).replace("# Fake Paper\n", "前置说明\n")


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
    assert feishu.fetched == [("https://tenant.feishu.cn/docx/doc123", "xml", "simple")]

    second = pipeline.process_ref(ref)
    assert second.doc_url == first.doc_url
    assert second.cached is True
    assert feishu.published == ["doc123"]
    store.close()


def test_generation_repairs_only_missing_h1_from_metadata():
    attempts = []
    result = _generate_complete_paper_markdown(
        MissingH1LLM(),
        "生成文档",
        [],
        paper_id="2604.12946",
        title="Fake Paper",
        attempt_writer=lambda number, raw, errors: attempts.append((number, raw, errors)),
    )

    assert result.startswith("# [2604.12946] Fake Paper\n")
    assert attempts[0][1].startswith("前置说明")
    assert attempts[-1][2] == ["deterministic-repair:missing-h1"]


def test_pipeline_post_publish_fetch_failure_is_warning(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = FetchFailFeishu()
    pipeline = MaxReadPipeline(store, FakeArxiv(), feishu, FakeLLM(), require_source=True)
    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))
    assert result.doc_url == "https://tenant.feishu.cn/docx/doc123"
    record = store.get_paper("2604.12946")
    assert "post-publish:fetch-failed" in record.error
    store.close()


def test_pipeline_blocks_delivery_when_published_formula_is_invalid(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    pipeline = MaxReadPipeline(store, FakeArxiv(), InvalidPublishedFormulaFeishu(), FakeLLM(), require_source=True)

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.doc_url == "https://tenant.feishu.cn/docx/doc123"
    assert "发布后质检失败" in result.error
    record = store.get_paper("2604.12946")
    assert record.status == "quality_failed"
    assert "post-publish:quality:formula:xml:high:joined-spacing-command" in record.error
    store.close()


def test_pipeline_discards_stale_warning_after_structural_repair(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = RepairablePublishedFormulaFeishu()
    pipeline = MaxReadPipeline(
        store,
        FakeArxiv(),
        feishu,
        FakeLLM(),
        require_source=True,
        visual_qa=VisualQAController(enabled=False, max_repairs=2),
    )

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.error == ""
    assert feishu.repaired is True
    assert feishu.replacements[0][0] == "formula"
    record = store.get_paper("2604.12946")
    assert record.status == "done"
    assert "joined-spacing-command" not in record.error
    store.close()


def test_pipeline_classifies_pre_publish_gate_as_quality_failure(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = FakeFeishu()
    pipeline = MaxReadPipeline(store, FakeArxiv(), feishu, BadQualityLLM(), require_source=True)

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.doc_url == ""
    assert "论文已读完" in result.error
    assert "总结模型调用失败" not in result.error
    assert store.get_paper("2604.12946").status == "quality_failed"
    assert feishu.published == []
    store.close()


def test_write_paper_artifact_uses_paper_directory(tmp_path):
    bundle = FakeArxiv().fetch("2604.12946")
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    bundle.pdf_path = paper_dir / "paper.pdf"

    _write_paper_artifact(bundle, "01-generated.md", "# draft")

    assert (paper_dir / "pipeline_artifacts" / "01-generated.md").read_text() == "# draft"


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


def test_describe_figures_for_prompt_uses_image_reader(tmp_path):
    image = tmp_path / "figure.png"
    image.write_bytes(b"not inspected by fake llm")
    marker = "[MaxReadFigure:1:ambiguous_name]"

    descriptions, warnings = _describe_figures_for_prompt(FakeVisionLLM(), [(marker, image, "Architecture caption")])

    assert warnings == []
    assert descriptions[marker] == "图中显示两个相连模块和一条从输入到输出的箭头。"


def test_sanitize_repository_markdown_removes_unverified_repository():
    markdown = "# T\n\n仓库：https://github.com/wrong/repo\n\n| 维度 | 一句话 |\n| --- | --- |\n| 仓库 | https://github.com/wrong/repo |\n"

    cleaned = _sanitize_repository_markdown(markdown, "")

    assert "github.com" not in cleaned
    assert "| 仓库 |" not in cleaned
