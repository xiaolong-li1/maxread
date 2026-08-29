import json
import re
from pathlib import Path

from maxread.db import Store
from maxread.models import ArxivMetadata, PaperBundle, PaperFigure, PaperRef
from maxread.pipeline import IncompleteGenerationError, MaxReadPipeline, _describe_figures_for_prompt, _deterministic_editorial_validation, _duplicate_markdown_table_sections, _extract_project_summary, _extract_section_output, _generate_complete_paper_markdown, _generate_sectional_paper_markdown, _global_sectional_uniqueness_errors, _load_retry_context, _paper_method_markdown, _paper_method_source_context, _paper_review_source_context, _post_publish_failure_message, _require_renderable_source_figures, _sanitize_repository_markdown, _section_output_errors, _write_paper_artifact
from maxread.quality import PrePublishQualityError
from maxread.visual_qa import VisualQAController
from maxread.workflow import WorkflowEvent, WorkflowState


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


def test_project_summary_prefers_generated_one_sentence_positioning():
    markdown = """# [2608.00001] 中文标题：通过稀疏路由减少长视频生成成本

**TL;DR**：这篇论文提出一个更长的说明。第二句话继续解释实验。
"""

    assert _extract_project_summary(markdown, "abstract fallback") == "通过稀疏路由减少长视频生成成本"


def test_post_publish_message_distinguishes_export_infrastructure_from_visual_failure():
    infrastructure = _post_publish_failure_message([
        "visual-qa:infrastructure:export-pending:ticket=123"
    ])
    quality = _post_publish_failure_message([
        "visual-qa:high:invalid-formula:page 2"
    ])

    assert "飞书 PDF 导出仍在处理中" in infrastructure
    assert "可恢复的基础设施状态" in infrastructure
    assert "质检发现明确问题" in quality


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
        if "方法一致性验收员" in system:
            return json.dumps({"passed": True, "findings": []}, ensure_ascii=False)
        if "方法推导一致性审计员" in system:
            match = re.search(r"待审计 Markdown：\n```markdown\n(.*?)\n```", user, flags=re.S)
            return json.dumps({"markdown": match.group(1) if match else body, "issues": []}, ensure_ascii=False)
        return body


class FakeVisionLLM(FakeLLM):
    def responses_image_text(self, system, user, image_path):
        assert "caption:" in user
        assert str(image_path).endswith("figure.png")
        return "图中显示两个相连模块和一条从输入到输出的箭头。"


class BadQualityLLM(FakeLLM):
    def responses_text(self, system, user, **kwargs):
        body = super().responses_text(system, user, **kwargs)
        if "方法一致性验收员" in system or "方法推导一致性审计员" in system:
            return body
        return body + r"\n\n公式：<latex>\newcommand{\RR}{\mathbb{R}} x\in\RR</latex>"


class RepairingQualityLLM(BadQualityLLM):
    def __init__(self):
        self.repair_calls = 0

    def responses_text(self, system, user, **kwargs):
        if "本轮确定性质检错误" in user:
            self.repair_calls += 1
            if "待修复块" in user:
                return json.dumps({"markdown": "公式已修复。", "issues": []}, ensure_ascii=False)
            return json.dumps(
                {"markdown": FakeLLM().responses_text(system, user, **kwargs), "issues": []},
                ensure_ascii=False,
            )
        return super().responses_text(system, user, **kwargs)


class MissingH1LLM(FakeLLM):
    def responses_text(self, system, user, **kwargs):
        return super().responses_text(system, user, **kwargs).replace("# Fake Paper\n", "前置说明\n")


class ConcatenatedDocumentLLM(FakeLLM):
    def __init__(self):
        self.calls = 0

    def responses_text(self, system, user, **kwargs):
        self.calls += 1
        complete = FakeLLM().responses_text(system, user, **kwargs).replace(
            "# Fake Paper", "# [2604.12946] Complete Paper", 1
        )
        return (
            "The user wants me to generate a Feishu document.\n"
            "# [2604.12946] Partial Draft\n\nThis fragment is incomplete."
            + complete
        )


class RepairingGenerationLLM(FakeLLM):
    def __init__(self):
        self.calls = 0
        self.prompts = []

    def responses_text(self, system, user, **kwargs):
        self.calls += 1
        self.prompts.append(user)
        if self.calls == 1:
            return "The user wants me to draft a document.\n" + ("incomplete " * 220)
        return super().responses_text(system, user, **kwargs)


class CapturingGenerationLLM(FakeLLM):
    def __init__(self):
        self.prompts = []

    def responses_text(self, system, user, **kwargs):
        self.prompts.append(user)
        return super().responses_text(system, user, **kwargs)


class AlwaysInvalidGenerationLLM:
    def __init__(self):
        self.calls = 0

    def responses_text(self, system, user, **kwargs):
        self.calls += 1
        return "The user wants me to draft a document.\n" + ("incomplete " * 220)


class MethodValidationFailLLM(FakeLLM):
    def responses_text(self, system, user, **kwargs):
        if "方法一致性验收员" in system:
            return json.dumps(
                {
                    "passed": False,
                    "findings": [
                        {
                            "category": "math",
                            "severity": "high",
                            "detail": "定义与派生式不一致。",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return super().responses_text(system, user, **kwargs)


class FakeArxivNoSource(FakeArxiv):
    def fetch(self, paper_id):
        bundle = super().fetch(paper_id)
        bundle.source_text = ""
        bundle.source_path = None
        bundle.parse_warnings = ["TeX source download failed: HTTP 429"]
        return bundle


class ExplodingArxiv:
    def fetch(self, paper_id):
        raise AssertionError("published-document resume unexpectedly fetched source")


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

    forced = pipeline.process_ref(ref, force=True)
    assert forced.doc_url == first.doc_url
    assert forced.cached is False
    assert feishu.published == ["doc123", "doc123"]
    store.close()


def test_pipeline_emits_durable_workflow_milestones(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    usage_id = store.add_usage_event("evt", "om", "oc", "p2p", "ou", "paper", "2604.12946", "url", status="queued")
    queued = store.enqueue_job("paper", "2604.12946", "url", "evt", "om", "oc", "p2p", "ou", usage_id)
    store.claim_next_queue_job(worker_id="worker-a")
    pipeline = MaxReadPipeline(
        store,
        FakeArxiv(),
        FakeFeishu(),
        FakeLLM(),
        require_source=True,
        on_workflow_event=lambda event, detail="": store.transition_queue_job(queued["job_id"], event, detail),
    )

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.error == ""
    job = store.list_queue_jobs()[0]
    assert job["status"] == "done"
    assert job["workflow_state"] == WorkflowState.COMPLETED.value
    transitions = [
        json.loads(item["detail"])
        for item in reversed(store.list_job_events(queued["job_id"], 30))
        if item["event_type"] == "transition"
    ]
    assert [item["to"] for item in transitions] == [
        "claimed",
        "fetching",
        "source_ready",
        "generating",
        "generation_checking",
        "reviewing",
        "quality_checking",
        "publishing",
        "post_publish_checking",
        "completed",
    ]
    store.close()


def test_pipeline_resumes_published_quality_failure_without_model_or_source_fetch(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.upsert_paper(
        "2604.12946",
        "quality_failed",
        title="Fake Paper",
        doc_url="https://tenant.feishu.cn/docx/existing",
        doc_token="existing",
        error="visual-qa:high:invalid-formula",
    )
    pipeline = MaxReadPipeline(store, ExplodingArxiv(), FakeFeishu(), None, require_source=True)

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.doc_url == "https://tenant.feishu.cn/docx/existing"
    assert result.error == ""
    assert store.get_paper("2604.12946").status == "done"
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


def test_generation_extracts_complete_document_appended_after_partial_draft():
    llm = ConcatenatedDocumentLLM()
    attempts = []

    result = _generate_complete_paper_markdown(
        llm,
        "生成文档",
        [],
        paper_id="2604.12946",
        title="Complete Paper",
        attempt_writer=lambda number, raw, errors: attempts.append((number, raw, errors)),
    )

    assert llm.calls == 1
    assert result.startswith("# [2604.12946] Complete Paper\n")
    assert "The user wants me" not in result
    assert attempts[-1][2] == ["deterministic-repair:complete-document-suffix"]


def test_generation_repairs_invalid_output_with_previous_draft_and_exact_errors():
    llm = RepairingGenerationLLM()
    events = []

    result = _generate_complete_paper_markdown(
        llm,
        "生成文档",
        [],
        attempts=3,
        paper_id="2604.12946",
        title="Fake Paper",
        on_workflow_event=lambda event, detail: events.append((event, detail)),
    )

    assert result.startswith("# Fake Paper\n")
    assert llm.calls == 2
    assert "精确错误：" in llm.prompts[1]
    assert "BEGIN PREVIOUS OUTPUT" in llm.prompts[1]
    assert "The user wants me to draft a document" in llm.prompts[1]
    assert [event for event, _detail in events] == [
        WorkflowEvent.GENERATION_CHECK_STARTED,
        WorkflowEvent.GENERATION_REPAIR_REQUIRED,
        WorkflowEvent.GENERATION_RECHECK,
    ]


def test_manual_retry_starts_from_previous_draft_and_failure_ledger():
    llm = CapturingGenerationLLM()
    previous = "# Previous\n\n**TL;DR**：旧稿。\n\n缺少后续章节。"

    result = _generate_complete_paper_markdown(
        llm,
        "生成文档",
        [],
        paper_id="2604.12946",
        title="Fake Paper",
        previous_markdown=previous,
        prior_feedback=["attempt 3: missing-section-7", "quality:formula:xml:high:html-tag-in-formula"],
    )

    assert result.startswith("# Fake Paper\n")
    assert len(llm.prompts) == 1
    assert "BEGIN PREVIOUS OUTPUT" in llm.prompts[0]
    assert previous in llm.prompts[0]
    assert "missing-section-7" in llm.prompts[0]
    assert "html-tag-in-formula" in llm.prompts[0]
    assert "不得再次引入" in llm.prompts[0]


def test_retry_context_loads_previous_draft_and_all_quality_layers(tmp_path):
    bundle = FakeArxiv().fetch("2604.12946")
    bundle.pdf_path = tmp_path / "paper.pdf"
    bundle.pdf_path.write_bytes(b"pdf")
    artifacts = tmp_path / "pipeline_artifacts"
    artifacts.mkdir()
    (artifacts / "01-20260819T010000Z-attempt-3.md").write_text("# Failed draft\n", encoding="utf-8")
    (artifacts / "01-20260819T010000Z-attempt-3.json").write_text(
        json.dumps({"attempt": 3, "errors": ["missing-section-7"]}),
        encoding="utf-8",
    )
    (artifacts / "05-final.md").write_text("# Quality failed draft\n", encoding="utf-8")
    (artifacts / "07-quality-round-3.json").write_text(
        json.dumps({"blocking_warnings": ["quality:formula:xml:high:html-tag-in-formula"]}),
        encoding="utf-8",
    )
    (artifacts / "09-visual-qa.json").write_text(
        json.dumps(
            {
                "rounds": [
                    {
                        "round": 2,
                        "findings": [
                            {"kind": "invalid-formula", "detail": "页面有无效公式", "section": "3.1 方法"}
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    context = _load_retry_context(bundle)

    assert context.previous_markdown == "# Quality failed draft\n"
    assert any("missing-section-7" in item for item in context.feedback)
    assert any("html-tag-in-formula" in item for item in context.feedback)
    assert any("页面有无效公式" in item for item in context.feedback)


def test_retry_context_prefers_complete_polished_draft_over_one_section(tmp_path):
    bundle = FakeArxiv().fetch("2604.12946")
    bundle.pdf_path = tmp_path / "paper.pdf"
    bundle.pdf_path.write_bytes(b"pdf")
    artifacts = tmp_path / "pipeline_artifacts"
    artifacts.mkdir()
    (artifacts / "01-20260829T010000Z-method-attempt-2.md").write_text(
        "## 3. Only one section\n",
        encoding="utf-8",
    )
    (artifacts / "01-20260829T010000Z-method-attempt-2.json").write_text(
        json.dumps({"section": "method", "attempt": 2, "errors": []}),
        encoding="utf-8",
    )
    (artifacts / "01-generated.md").write_text("# Complete generated draft\n", encoding="utf-8")
    (artifacts / "02-polished.md").write_text("# Complete polished draft\n", encoding="utf-8")

    context = _load_retry_context(bundle)

    assert context.previous_markdown == "# Complete polished draft\n"
    assert "Only one section" not in context.previous_markdown


def test_failed_whole_draft_retry_falls_back_to_sectional_generation(tmp_path):
    class DurableArxiv(FakeArxiv):
        def fetch(self, paper_id):
            bundle = super().fetch(paper_id)
            bundle.pdf_path = tmp_path / "paper.pdf"
            bundle.source_path = tmp_path / "paper.source"
            bundle.pdf_path.write_bytes(b"pdf")
            bundle.source_path.write_bytes(b"source")
            return bundle

    class FallbackLLM(FakeLLM):
        def __init__(self):
            self.whole_draft_calls = 0
            self.section_calls = 0

        def responses_text(self, system, user, **kwargs):
            if "BEGIN PREVIOUS OUTPUT" in user:
                self.whole_draft_calls += 1
                raise RuntimeError("upstream 502")
            if "分章生成任务：" not in user:
                return super().responses_text(system, user, **kwargs)
            self.section_calls += 1
            if "文档开头与第 1-2 章" in user:
                return "# [2604.12946] Fake Paper\n\n**TL;DR**：摘要。\n\n|维度|一句话|\n|---|---|\n|问题|问题|\n\n## 1. 这篇论文要解决什么问题\n\n" + ("背景。" * 180) + "\n\n## 2. 核心观察 / 关键直觉\n\n" + ("观察。" * 180)
            if "第 3 章方法框架" in user:
                return "## 3. 方法框架\n\n" + ("方法输入、计算、输出与边界。" * 150)
            if "第 4 章实验结果" in user:
                return "## 4. 实验结果\n\n" + ("实验设置与结果。" * 120)
            if "第 5 章消融" in user:
                return "## 5. 消融与补充分析\n\n" + ("控制变量与消融结果。" * 120)
            return "## 6. 局限性与开放问题\n\n" + ("局限。" * 100) + "\n\n## 7. 整体评价\n\n" + ("评价。" * 100)

    artifacts = tmp_path / "pipeline_artifacts"
    artifacts.mkdir()
    (artifacts / "02-polished.md").write_text("# Previous complete draft\n", encoding="utf-8")
    store = Store(tmp_path / "maxread.sqlite3")
    llm = FallbackLLM()
    pipeline = MaxReadPipeline(
        store,
        DurableArxiv(),
        FakeFeishu(),
        llm,
        sectional_generation_enabled=True,
        sectional_generation_workers=5,
        generation_repair_rounds=0,
    )

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.error == ""
    assert result.doc_url
    assert llm.whole_draft_calls == 1
    assert llm.section_calls == 5
    assert list(artifacts.glob("01-*-sectional-fallback.json"))
    store.close()


def test_generation_enters_incomplete_only_after_bounded_attempts_are_exhausted():
    llm = AlwaysInvalidGenerationLLM()
    events = []

    try:
        _generate_complete_paper_markdown(
            llm,
            "生成文档",
            [],
            attempts=3,
            paper_id="2604.12946",
            title="Fake Paper",
            on_workflow_event=lambda event, detail: events.append((event, detail)),
        )
        raise AssertionError("incomplete generation unexpectedly passed")
    except IncompleteGenerationError as exc:
        assert exc.attempts == 3

    assert llm.calls == 3
    assert [event for event, _detail in events] == [
        WorkflowEvent.GENERATION_CHECK_STARTED,
        WorkflowEvent.GENERATION_REPAIR_REQUIRED,
        WorkflowEvent.GENERATION_RECHECK,
        WorkflowEvent.GENERATION_REPAIR_REQUIRED,
        WorkflowEvent.GENERATION_RECHECK,
    ]


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
    assert "发布后质检发现明确问题" in result.error
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


def test_pipeline_repairs_blocking_quality_errors_before_publishing(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = FakeFeishu()
    llm = RepairingQualityLLM()
    pipeline = MaxReadPipeline(store, FakeArxiv(), feishu, llm, require_source=True)

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.error == ""
    assert result.doc_url == "https://tenant.feishu.cn/docx/doc123"
    assert llm.repair_calls == 1
    assert feishu.published == ["doc123"]
    assert store.get_paper("2604.12946").status == "done"
    store.close()


def test_pipeline_blocks_document_creation_when_method_revalidation_fails(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    feishu = FakeFeishu()
    workflow_state = WorkflowState.QUEUED

    def transition_event(event, _detail=""):
        nonlocal workflow_state
        from maxread.workflow import transition
        workflow_state = transition(workflow_state, event).to_state

    # process_ref begins after queue claim in production.
    workflow_state = WorkflowState.CLAIMED
    pipeline = MaxReadPipeline(
        store,
        FakeArxiv(),
        feishu,
        MethodValidationFailLLM(),
        require_source=True,
        on_workflow_event=transition_event,
    )

    result = pipeline.process_ref(PaperRef("2604.12946", "https://arxiv.org/abs/2604.12946"))

    assert result.doc_url == ""
    assert "method-audit:math:high" in result.error
    assert feishu.published == []
    assert workflow_state == WorkflowState.QUALITY_FAILED
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


def test_describe_figures_for_prompt_runs_independent_images_concurrently(tmp_path, monkeypatch):
    import threading
    import time

    class ConcurrentVisionLLM:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def responses_image_text(self, _system, _user, image_path):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return f"读图 {Path(image_path).stem}"

    inserts = []
    for index in range(4):
        image = tmp_path / f"figure-{index}.png"
        image.write_bytes(b"image")
        inserts.append((f"[MaxReadFigure:{index}:f]", image, f"Caption {index}"))
    monkeypatch.setenv("MAXREAD_FIGURE_VISION_WORKERS", "4")
    llm = ConcurrentVisionLLM()

    descriptions, warnings = _describe_figures_for_prompt(llm, inserts)

    assert warnings == []
    assert len(descriptions) == 4
    assert llm.max_active >= 2


def test_require_renderable_source_figures_blocks_silent_missing_images():
    bundle = FakeArxiv().fetch("2604.12946")
    bundle.source_figures = [PaperFigure(asset="figures/overview.pdf", caption="Method overview")]

    try:
        _require_renderable_source_figures(bundle, [])
    except PrePublishQualityError as exc:
        assert "no-renderable-source-figure" in str(exc)
        assert "formats=.pdf" in str(exc)
    else:
        raise AssertionError("missing rendered figures should block publication")

    _require_renderable_source_figures(bundle, [(Path("overview.png"), "Method overview")])

    bundle.source_figures = [PaperFigure(asset="figures/appendix.pdf", caption="Extra examples", is_appendix=True)]
    _require_renderable_source_figures(bundle, [])


def test_paper_review_source_context_includes_primary_method_evidence():
    bundle = FakeArxiv().fetch("2604.12946")
    bundle.source_text = r"\section{Method} m_i=(s_i+\tau_i,s_i+h_i,s_i+w_i)"
    bundle.source_captions = ["Position design comparison."]

    context = _paper_review_source_context(bundle)

    assert "TeX/source excerpt" in context
    assert r"s_i+\tau_i" in context
    assert "Position design comparison" in context


def test_method_validation_context_keeps_only_method_section_and_compact_source():
    bundle = FakeArxiv().fetch("2604.12946")
    bundle.source_text = (
        r"\section{Introduction} intro "
        r"\section{Method} method evidence x=y "
        r"\section{Experiments} experiment evidence"
    )
    markdown = "# T\n\n## 2. O\n\nobs\n\n## 3. 方法框架\n\nmethod\n\n## 4. 实验结果\n\nresult"

    assert _paper_method_markdown(markdown).startswith("## 3.")
    context = _paper_method_source_context(bundle)
    assert "method evidence" in context
    assert "experiment evidence" not in context


def test_deterministic_editorial_validation_rejects_duplicate_marker():
    marker = "[MaxReadFigure:1:a]"
    markdown = "# T\n\n**TL;DR**：x\n\n" + "\n\n".join(
        f"## {number}. S\n\n正文。" for number in range(1, 8)
    ) + f"\n\n{marker}\n\n{marker}"

    result = _deterministic_editorial_validation(markdown, [marker])

    assert result.passed is False
    assert any("global figure marker count" in issue.detail for issue in result.issues)


def test_sectional_generation_runs_all_sections_concurrently_and_merges_unique_materials():
    class SectionLLM:
        def __init__(self):
            self.prompts = []

        def responses_text(self, _system, user, **_kwargs):
            self.prompts.append(user)
            markers = re.findall(r"\[MaxReadFigure:[^\]]+\]", user.split("分章生成任务：", 1)[-1])
            tables = re.findall(r"\[MaxReadTable:\d+\]", user.split("分章生成任务：", 1)[-1])
            material = "\n".join(
                markers
                + [f"{marker}\n|配置|值|\n|---|---|\n|table-{marker.split(':', 1)[1].rstrip(']')}|1|" for marker in tables]
            )
            if "文档开头与第 1-2 章" in user:
                return "# [2604.12946] 标题\n\n**English**\n\n**TL;DR**：摘要。\n\n|维度|一句话|\n|---|---|\n|问题|问题|\n\n## 1. 这篇论文要解决什么问题\n\n" + ("背景。" * 180) + "\n\n## 2. 核心观察 / 关键直觉\n\n" + ("观察。" * 180)
            if "第 3 章方法框架" in user:
                return "## 3. 方法框架\n\n" + ("方法输入、计算、输出与边界。" * 150) + "\n" + material
            if "第 4 章实验结果" in user:
                return "## 4. 实验结果\n\n" + ("实验设置与结果。" * 120) + "\n" + material
            if "第 5 章消融" in user:
                return "## 5. 消融与补充分析\n\n" + ("控制变量与消融结果。" * 120) + "\n" + material
            return "## 6. 局限性与开放问题\n\n" + ("局限。" * 100) + "\n\n## 7. 整体评价\n\n" + ("评价。" * 100)

    llm = SectionLLM()
    markers = {"front": [], "method": ["[MaxReadFigure:1:m]"], "experiments": ["[MaxReadFigure:2:e]"], "ablation": ["[MaxReadFigure:3:a]"], "closing": []}
    tables = {"front": [], "method": [1], "experiments": [2], "ablation": [3], "closing": []}

    markdown = _generate_sectional_paper_markdown(llm, "COMMON PREFIX", "2604.12946", markers, tables, attempts=1, workers=5)

    assert len(llm.prompts) == 5
    assert any("文档开头与第 1-2 章" in prompt for prompt in llm.prompts)
    assert all(prompt.startswith("COMMON PREFIX") for prompt in llm.prompts)
    assert [int(value) for value in re.findall(r"(?m)^##\s+([1-7])", markdown)] == list(range(1, 8))
    for marker in [item for values in markers.values() for item in values]:
        assert markdown.count(marker) == 1
    assert "MaxReadTable" not in markdown


def test_sectional_generation_retries_only_the_section_with_transient_model_error():
    class FlakySectionLLM:
        def __init__(self):
            self.calls = {}

        def responses_text(self, _system, user, **_kwargs):
            if "文档开头与第 1-2 章" in user:
                section = "front"
                output = "# [2604.12946] 标题\n\n**TL;DR**：摘要。\n\n|维度|一句话|\n|---|---|\n|问题|问题|\n\n## 1. 这篇论文要解决什么问题\n\n" + ("背景。" * 180) + "\n\n## 2. 核心观察 / 关键直觉\n\n" + ("观察。" * 180)
            elif "第 3 章方法框架" in user:
                section = "method"
                output = "## 3. 方法框架\n\n" + ("方法输入、计算、输出与边界。" * 150)
            elif "第 4 章实验结果" in user:
                section = "experiments"
                output = "## 4. 实验结果\n\n" + ("实验设置与结果。" * 120)
            elif "第 5 章消融" in user:
                section = "ablation"
                output = "## 5. 消融与补充分析\n\n" + ("控制变量与消融结果。" * 120)
            else:
                section = "closing"
                output = "## 6. 局限性与开放问题\n\n" + ("局限。" * 100) + "\n\n## 7. 整体评价\n\n" + ("评价。" * 100)
            self.calls[section] = self.calls.get(section, 0) + 1
            if section == "method" and self.calls[section] == 1:
                raise RuntimeError("transient 502")
            return output

    attempts = []
    llm = FlakySectionLLM()
    markdown = _generate_sectional_paper_markdown(
        llm,
        "COMMON PREFIX",
        "2604.12946",
        {key: [] for key in ("front", "method", "experiments", "ablation", "closing")},
        {key: [] for key in ("front", "method", "experiments", "ablation", "closing")},
        attempts=2,
        workers=5,
        artifact_writer=lambda section, attempt, raw, errors: attempts.append((section, attempt, raw, errors)),
    )

    assert "## 3. 方法框架" in markdown
    assert llm.calls == {"front": 1, "method": 2, "experiments": 1, "ablation": 1, "closing": 1}
    assert any(section == "method" and errors[0].startswith("model-call:") for section, _attempt, _raw, errors in attempts)


def test_sectional_global_check_rejects_duplicate_table_content():
    markdown = """# T

## 1. A

[MaxReadTable:1]
|A|B|
|---|---|
|x|1|

## 2. B

[MaxReadTable:2]
|A|B|
|---|---|
|x|1|
"""

    errors = _global_sectional_uniqueness_errors(markdown, [], [1, 2])

    assert "duplicate markdown table content across sections" in errors


def test_sectional_duplicate_table_ownership_repairs_unmarked_copy():
    table = "|A|B|\n|---|---|\n|x|1|"
    outputs = {
        "front": f"# T\n\n{table}",
        "method": "## 3. M",
        "experiments": f"## 4. E\n\n[MaxReadTable:1]\n{table}",
        "ablation": "## 5. A",
        "closing": "## 6. L\n\n## 7. V",
    }

    offenders = _duplicate_markdown_table_sections(outputs, list(outputs))

    assert offenders == {"front"}


def test_sectional_generation_repairs_only_section_with_duplicate_table():
    class DuplicateRepairLLM:
        def __init__(self):
            self.front_calls = 0

        def responses_text(self, _system, user, **_kwargs):
            table = "|配置|值|\n|---|---|\n|same|1|"
            if "文档开头与第 1-2 章" in user:
                self.front_calls += 1
                duplicate = "" if "合并级返修要求" in user else f"\n\n{table}"
                return "# [2604.12946] 标题\n\n**TL;DR**：摘要。\n\n|维度|一句话|\n|---|---|\n|问题|问题|\n\n## 1. 这篇论文要解决什么问题\n\n" + ("背景。" * 180) + "\n\n## 2. 核心观察 / 关键直觉\n\n" + ("观察。" * 180) + duplicate
            if "第 3 章方法框架" in user:
                return "## 3. 方法框架\n\n" + ("方法输入、计算、输出与边界。" * 150)
            if "第 4 章实验结果" in user:
                return "## 4. 实验结果\n\n" + ("实验设置与结果。" * 120) + "\n\n[MaxReadTable:1]\n" + table
            if "第 5 章消融" in user:
                return "## 5. 消融与补充分析\n\n" + ("控制变量与消融结果。" * 120)
            return "## 6. 局限性与开放问题\n\n" + ("局限。" * 100) + "\n\n## 7. 整体评价\n\n" + ("评价。" * 100)

    llm = DuplicateRepairLLM()
    empty = {key: [] for key in ("front", "method", "experiments", "ablation", "closing")}
    tables = {**empty, "experiments": [1]}

    markdown = _generate_sectional_paper_markdown(llm, "COMMON PREFIX", "2604.12946", empty, tables, attempts=2, workers=4)

    assert llm.front_calls == 2
    assert markdown.count("|same|1|") == 1


def test_section_output_strips_inline_provider_preamble_and_uses_real_heading():
    raw = (
        'I will emit "## 4. 实验结果" only. Previous error...'
        "## 4. 实验结果\n\n### 4.1 设置\n\n正文。"
    )

    output = _extract_section_output(raw, "experiments")

    assert output.startswith("## 4. 实验结果")
    assert "I will emit" not in output
    assert output.count("## 4. 实验结果") == 1


def test_section_output_rejects_malformed_nested_heading():
    markdown = "## 3. 方法框架\n\n### 3.# 3. 方法框架\n\n" + ("方法流程。" * 300)

    errors = _section_output_errors(markdown, "method", [], [])

    assert "malformed nested heading" in errors


def test_section_length_budget_applies_to_all_prose_but_not_tables():
    oversized_closing = "## 6. 局限性与开放问题\n\n" + ("冗长说明。" * 700) + "\n\n## 7. 整体评价\n\n结论。"
    large_method = "## 3. 方法框架\n\n" + ("必要推导。" * 1800)
    compact_with_large_table = "## 4. 实验结果\n\n" + ("关键结论。" * 100) + "\n\n|配置|结果|\n|---|---|\n" + ("|A|1|\n" * 2000)

    closing_errors = _section_output_errors(oversized_closing, "closing", [], [])
    method_errors = _section_output_errors(large_method, "method", [], [])
    experiment_errors = _section_output_errors(compact_with_large_table, "experiments", [], [])

    assert any("narrative too long" in error for error in closing_errors)
    assert any("narrative too long" in error for error in method_errors)
    assert not any("narrative too long" in error for error in experiment_errors)


def test_experiment_with_visual_evidence_can_use_compact_narrative():
    marker = "[MaxReadFigure:1:result]"
    markdown = "## 4. 实验结果\n\n" + ("关键结果。" * 55) + f"\n\n{marker}\n\n**图：主结果对比。**"

    errors = _section_output_errors(markdown, "experiments", [marker], [])

    assert not any("narrative too short" in error for error in errors)


def test_sanitize_repository_markdown_removes_unverified_repository():
    markdown = "# T\n\n仓库：https://github.com/wrong/repo\n\n| 维度 | 一句话 |\n| --- | --- |\n| 仓库 | https://github.com/wrong/repo |\n"

    cleaned = _sanitize_repository_markdown(markdown, "")

    assert "github.com" not in cleaned
    assert "| 仓库 |" not in cleaned
