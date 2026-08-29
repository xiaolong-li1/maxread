from maxread.quality import (
    blocking_quality_warnings,
    paper_markdown_completeness_errors,
    pre_publish_quality_warnings,
    quality_warnings,
    validate_fetched_docx_content,
    verify_published_docx,
)
from maxread.render import markdown_to_docx_xml, polish_markdown


def test_blocking_quality_warnings_selects_high_severity_only():
    warnings = [
        "quality:xml:xml:high:latex-downgraded-to-code",
        "post-publish:quality:formula:xml:high:joined-spacing-command",
        "quality:text:markdown:medium:source-truncated-marker",
        "image-caption-short:figure.png",
    ]

    assert blocking_quality_warnings(warnings) == warnings[:2]


def test_blocking_quality_warnings_includes_visual_high_severity():
    warnings = [
        "visual-qa:high:invalid-formula:bad formula",
        "visual-qa:recheck:high:image-overflow:still outside editor",
        "visual-qa:medium:image-too-wide:review",
        "visual-qa:remote-error:ssh unavailable",
    ]

    assert blocking_quality_warnings(warnings) == warnings[:2] + [warnings[3]]


def test_table_geometry_warnings_are_nonblocking_but_render_failure_blocks():
    warnings = [
        "visual-qa:high:table-overflow:表格超出正文区域 388px",
        "post-publish:visual-qa:recheck:high:table-overflow:legacy warning",
        "visual-qa:high:table-clipped:表格被裁切且无法横向滚动",
        "visual-qa:high:table-render-failed:表格没有有效行列",
    ]

    assert blocking_quality_warnings(warnings) == [warnings[3]]


def test_quality_flags_uncompiled_uncertainty_only_inside_table_cells():
    markdown = r"""正文可解释符号 x^{2}。

| ID | M1 |
| --- | --- |
| 1 | 1.28^{+0.11}_{-0.10} |
"""

    warnings = quality_warnings(markdown, "")

    assert "quality:format:markdown:high:raw-table-math" in warnings


def test_blocking_quality_warnings_includes_failed_image_publication():
    warnings = [
        "image-anchor-missing:framework.png:[MaxReadFigure:1:framework]",
        "post-publish:marker-left-after-publish",
        "image-anchor-missing:plot.png:[MaxReadFigure:2:plot]",
    ]

    assert blocking_quality_warnings(warnings) == warnings


def test_quality_flags_markdown_structure_swallowed_inside_xml_paragraph():
    xml = (
        "<p><latex>\\Omega</latex> text<br/>### 3.4 Method<br/>"
        "| Metric | Value |<br/>| --- | --- |<br/>"
        "[MaxReadFigure:1:framework]</p>"
    )

    warnings = quality_warnings("", xml)

    assert "quality:format:xml:high:markdown-heading-inside-paragraph" in warnings
    assert "quality:format:xml:high:markdown-table-inside-paragraph" in warnings
    assert "quality:format:xml:high:figure-marker-inside-paragraph" in warnings


def test_paper_completeness_requires_all_sections_and_three_selected_figures():
    markers = [f"[MaxReadFigure:{i}:f{i}]" for i in range(1, 6)]
    markdown = "# T\n\n**TL;DR**：摘要。\n\n" + "\n\n".join(
        f"## {number}. S\n\n" + ("正文。" * 80) for number in range(1, 8)
    )
    markdown += "\n\n" + "\n".join(markers[:3])

    assert paper_markdown_completeness_errors(markdown, markers) == []
    assert "missing-section-7" in paper_markdown_completeness_errors(markdown.replace("## 7. S", "### 7. S"), markers)
    assert "too-few-figures:2/3" in paper_markdown_completeness_errors(markdown.replace(markers[2], ""), markers)


def test_paper_completeness_requires_h1_on_first_nonempty_line():
    markdown = "前置说明\n\n# T\n\n**TL;DR**：摘要。\n\n" + "\n\n".join(
        f"## {number}. S\n\n" + ("正文。" * 80) for number in range(1, 8)
    )
    errors = paper_markdown_completeness_errors(markdown)
    assert "missing-h1" in errors


def test_paper_completeness_flags_outer_code_fence():
    markdown = "```markdown\n# T\n```"
    errors = paper_markdown_completeness_errors(markdown)
    assert "missing-h1" in errors
    assert "leading-code-fence" in errors


def test_paper_completeness_blocks_prompt_leak_and_duplicate_h1():
    markdown = "# [2212.02500] Title\n\nThe user wants me to generate a document.\n\n# [2212.02500] Title\n"

    errors = paper_markdown_completeness_errors(markdown)

    assert "prompt-leak" in errors
    assert "duplicate-h1" in errors


def test_quality_formula_agent_flags_unsupported_macros():
    warnings = quality_warnings(r"<latex>\bmX+\rvx+\tens{K}+\matrix{A}</latex>", r"<p><latex>\bmX+\rvx+\tens{K}+\matrix{A}</latex></p>")
    assert "quality:formula:markdown:high:unsupported-bm-macro" in warnings
    assert "quality:formula:markdown:high:unsupported-paper-macro" in warnings
    assert "quality:formula:markdown:high:unsupported-tensor-macro" in warnings
    assert "quality:formula:markdown:high:unsupported-position-macro" in warnings


def test_quality_formula_agent_flags_joined_spacing_commands():
    warnings = quality_warnings(r"<latex>a=1,\qquadb=2</latex>")

    assert "quality:formula:markdown:high:joined-spacing-command" in warnings


def test_quality_formula_agent_flags_internal_display_delimiter():
    warnings = quality_warnings(r"<latex>\begin{cases}a \[4pt] b\end{cases}</latex>")

    assert "quality:formula:markdown:high:internal-display-delimiter" in warnings


def test_quality_formula_agent_allows_cases_row_break_spacing():
    warnings = quality_warnings(r"<latex>\begin{cases}a\\[4pt]b\end{cases}</latex>")

    assert "quality:formula:markdown:medium:raw-dollar-display-math" not in warnings


def test_quality_agents_flag_raw_formatting_and_fused_formula_commands():
    warnings = quality_warnings(
        "",
        r"<p>\textbfLeaked</p><p><latex>\overlineQ<br/></latex></p>",
    )

    assert "quality:format:xml:high:raw-tex-formatting-command" in warnings
    assert "quality:formula:xml:high:fused-accent-command" in warnings
    assert "quality:formula:xml:high:html-tag-in-formula" in warnings


def test_quality_formula_agent_distinguishes_math_less_than_from_html_tags():
    warnings = quality_warnings(r"<latex>q<p,\quad a_1,\dots,a_n,\quad q\to p</latex>")

    assert "quality:formula:markdown:high:html-tag-in-formula" not in warnings
    assert "quality:formula:markdown:high:fused-accent-command" not in warnings
    assert "quality:formula:markdown:high:split-latex-command" not in warnings

    warnings = quality_warnings(r"<latex>x<br/>y</latex>")
    assert "quality:formula:markdown:high:html-tag-in-formula" in warnings


def test_quality_formula_agent_does_not_treat_currency_as_raw_math():
    markdown = "实验成本为 $19,627.77，另一阶段成本为 $1,656.25。"
    xml = "<p>实验成本为 $19,627.77</p><p>另一阶段成本为 $1,656.25</p>"

    warnings = quality_warnings(markdown, xml)

    assert "quality:formula:markdown:high:cjk-inside-formula" not in warnings
    assert "quality:formula:xml:high:cjk-inside-formula" not in warnings
    assert "quality:formula:xml:high:html-tag-in-formula" not in warnings


def test_quality_formula_agent_still_inspects_raw_dollar_math():
    warnings = quality_warnings(r"$中文 + x$")

    assert "quality:formula:markdown:high:cjk-inside-formula" in warnings


def test_quality_formula_agent_blocks_nested_latex_tags():
    warnings = quality_warnings(r"<latex><latex>x+y</latex></latex>")

    assert "quality:formula:markdown:high:nested-latex-tag" in warnings


def test_quality_formula_agent_blocks_unknown_html_from_compiler_frontend():
    warnings = quality_warnings(r"<latex>x<span>y</span></latex>")

    assert "quality:formula:markdown:high:unknown-html-in-formula" in warnings


def test_quality_text_agent_flags_unresolved_placeholders_and_truncated_tails():
    warnings = quality_warnings("这与 ?? 的发现一致。模型会 rep")

    assert "quality:text:markdown:medium:unresolved-question-placeholder" in warnings
    assert "quality:text:markdown:medium:possible-truncated-english-tail" in warnings


def test_validate_fetched_docx_content_checks_empty_title_and_markers():
    warnings = validate_fetched_docx_content("<title>T</title><p>[MaxReadFigure:1:a]</p>", expected_title="Missing")
    assert "missing-title" not in warnings
    assert "marker-left-after-publish" in warnings
    assert "missing-title" in validate_fetched_docx_content("<p>正文</p>", expected_title="T")
    assert "missing-title" in validate_fetched_docx_content("<title> </title><p>正文</p>", expected_title="T")
    assert validate_fetched_docx_content("", expected_title="T") == ["fetch-empty"]


def test_pre_publish_quality_warnings_ignores_unpublished_markers():
    warnings = pre_publish_quality_warnings(
        r"<latex>\bmX</latex>",
        r"<p><latex>\bmX</latex></p><p>[MaxReadFigure:1:a]</p>",
    )

    assert "quality:formula:xml:high:unsupported-bm-macro" in warnings


def test_quality_formula_agent_allows_standard_eta_command():
    warnings = quality_warnings("", r"<p><latex>p_\eta(z|x)+\eta_t+\eta^\star</latex></p>")

    assert "quality:formula:xml:high:unsupported-paper-macro" not in warnings
    assert all(":unpublished-marker:" not in warning for warning in warnings)


def test_quality_formula_agent_still_flags_et_prefixed_paper_macro():
    warnings = quality_warnings(r"<latex>\etLambda+\eta_t</latex>")

    assert "quality:formula:markdown:high:unsupported-paper-macro" in warnings


def test_quality_formula_agent_allows_standard_b_prefix_commands():
    warnings = quality_warnings(r"<latex>(i+p)\bmod n+\begin{bmatrix}x\\y\end{bmatrix}</latex>")

    assert "quality:formula:markdown:high:unsupported-bm-macro" not in warnings


def test_quality_formula_agent_still_flags_unexpanded_bm_macros():
    warnings = quality_warnings(r"<latex>\bmX+\bm{q}</latex>")

    assert "quality:formula:markdown:high:unsupported-bm-macro" in warnings


def test_validate_fetched_docx_content_does_not_count_omitted_media_blocks():
    warnings = validate_fetched_docx_content(
        "<title>T</title><p><latex>x+y</latex></p><img token=\"img1\"/>",
        expected_title="T",
        expected_image_min=2,
        expected_latex_min=3,
    )

    assert all(not warning.startswith("missing-images:") for warning in warnings)
    assert "missing-latex:1/3" in warnings


def test_verify_published_docx_returns_post_publish_quality_warnings():
    class Feishu:
        def fetch_docx(self, doc_url, doc_format="xml", detail="simple"):
            return {"data": {"document": {"content": "<title>T</title><p><latex>\\bmX</latex></p>"}}}

    warnings = verify_published_docx(Feishu(), "doc", expected_title="T", attempts=1)
    assert "post-publish:quality:formula:xml:high:unsupported-bm-macro" in warnings


def test_verify_published_docx_fetch_failure_is_soft_warning():
    class Feishu:
        def fetch_docx(self, *args, **kwargs):
            raise RuntimeError("temporary failure")

    warnings = verify_published_docx(Feishu(), "doc", expected_title="T", attempts=1)
    assert warnings[0].startswith("post-publish:fetch-failed:")


def test_verify_published_docx_retries_transient_roundtrip_formula_markup():
    class Feishu:
        calls = 0

        def fetch_docx(self, *args, **kwargs):
            self.calls += 1
            content = "<title>T</title><p><latex>x<br/>y</latex></p>" if self.calls == 1 else "<title>T</title><p><latex>x+y</latex></p>"
            return {"data": {"document": {"content": content}}}

    feishu = Feishu()
    warnings = verify_published_docx(feishu, "doc", expected_title="T", attempts=2, retry_delay=0)

    assert warnings == []
    assert feishu.calls == 2


def test_historical_formula_failure_corpus_compiles_without_blockers():
    corpus = [
        "调用 <latex>apply_p_rope</latex> 完成旋转。",
        r"<latex>\mathcal{V}=\Big\{x\;\middle|\;x>0\Big\}</latex>",
        "| 约束 | 值 |\n| --- | --- |\n| 集合 | <latex>|\\mathcal{T}_D|=|\\mathcal{T}_A|</latex> |",
        "| M |\n| --- |\n| 1.28^{+0.11}_{-0.10} |",
        r"<latex>a=1,\qquadb=2</latex>",
        r"<latex>\bm{q}+\bmX</latex>",
    ]

    for source in corpus:
        markdown = polish_markdown(source)
        xml = markdown_to_docx_xml(markdown)
        assert blocking_quality_warnings(pre_publish_quality_warnings(markdown, xml)) == [], source
