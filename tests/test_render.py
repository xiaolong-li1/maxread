import os
from pathlib import Path
from types import SimpleNamespace

from maxread.models import ArxivMetadata, PaperBundle, PaperFigure
from maxread.render import compiled_figure_captions, compose_related_figure_groups, constrain_rendered_image, display_caption, enforce_figure_owner_sections, ensure_figure_markers, ensure_priority_figure_markers, ensure_referenced_figure_markers, figure_placeholders, markdown_to_docx_xml, normalize_figure_captions, polish_markdown, prepare_key_figures, prepare_key_figures_with_owners, remove_false_material_warning, _figure_section_target, _pretty_grid_label, _render_asset


def test_polish_markdown_converts_math():
    text = "# $$\\na=b\\n$$\nInline $x+y$."
    out = polish_markdown(text)
    assert "# $$" not in out
    assert "<latex>\\na=b\\n</latex>" in out or "<latex>a=b</latex>" in out
    assert "<latex>x+y</latex>" in out


def test_figure_caption_compiler_emits_numbered_plain_paragraph():
    marker = "[MaxReadFigure:1:overview]"
    inserts = [(marker, Path("overview.png"), "Method overview")]

    markdown = normalize_figure_captions(f"正文引用。\n{marker}\n> **图：方法总览。**", inserts)
    xml = markdown_to_docx_xml(markdown)

    assert markdown == f"正文引用。\n{marker}\n\n图 1　方法总览。\n"
    assert compiled_figure_captions(markdown) == {marker: "图 1　方法总览。"}
    assert f"<p>正文引用。</p><p>{marker}</p>" == xml


def test_figure_caption_compiler_numbers_by_document_order():
    first = "[MaxReadFigure:2:result]"
    second = "[MaxReadFigure:1:method]"
    inserts = [
        (second, Path("method.png"), "Method overview."),
        (first, Path("result.png"), "Result comparison."),
    ]

    markdown = normalize_figure_captions(
        f"{first}\n图题：主要结果对比\n\n正文。\n\n{second}\n**图 8：方法框架**",
        inserts,
    )

    assert f"{first}\n\n图 1　主要结果对比。" in markdown
    assert f"{second}\n\n图 2　方法框架。" in markdown
    assert "**图" not in markdown


def test_figure_caption_compiler_never_hard_truncates_and_prefers_chinese_visual():
    marker = "[MaxReadFigure:1:related]"
    long_english = "A detailed data construction pipeline with annotations, camera poses, rendered trajectories, filtering, and the complete architecture of the world model and refiner."

    markdown = normalize_figure_captions(
        f"{marker}\n图题：并列图组：{long_english}",
        [(marker, Path("related.png"), long_english)],
        visual_descriptions={marker: "上半部分展示数据构建流程，下半部分展示世界模型与细化器的两阶段架构。"},
    )

    caption = compiled_figure_captions(markdown)[marker]
    assert caption == "图 1　上半部分展示数据构建流程，下半部分展示世界模型与细化器的两阶段架构。"
    assert "..." not in caption


def test_method_pipeline_group_is_not_reclassified_as_experiment_by_quality_word():
    target = _figure_section_target(
        Path("related-data_pipeline-pipeline_overview.png"),
        "Data construction pipeline and SANA-WM architecture followed by a refiner to improve visual quality.",
        "上半部分展示数据构建流程，下半部分展示模型架构与细化器。",
    )

    assert target == "method"


def test_result_comparison_stays_in_experiment_section():
    target = _figure_section_target(
        Path("main-result-comparison.png"),
        "Quantitative benchmark comparison across methods.",
        "不同方法的主结果对比。",
    )

    assert target == "experiments"


def test_polish_markdown_converts_tex_delimited_math():
    text = r"结果显示，\(k=4\) 有 \(1.6\times\) 加速。\[\hat{x}_{t+1}=F^{-1}(z)\]"
    out = polish_markdown(text)
    assert r"\(k=4\)" not in out
    assert r"\times" in out
    assert "<latex>k=4</latex>" in out
    assert "<latex>1.6\\times</latex>" in out
    assert "<latex>\\hat{x}_{t+1}=F^{-1}(z)</latex>" in out


def test_polish_markdown_repairs_common_latex_join_errors():
    text = r"$$\tau\sim N,\quadr\sim P, a\leL_{\mathrm{sm}}, b\geC, c\toY, d\inC, e\notinS$$ eta $5\mathrm{e}{-4}$ $block\_size$ $D_{JS}&lt;\tau$"
    out = polish_markdown(text)
    assert "\\quadr" not in out
    assert "\\quad{}r" in out
    assert "\\leL" not in out
    assert "\\le L_{\\mathrm{sm}}" in out
    assert "\\ge C" in out
    assert "\\to Y" in out
    assert "\\in C" in out
    assert "\\notin S" in out
    assert "5\\times10^{-4}" in out
    assert "block\\_size" not in out
    assert "block_size" in out
    assert "&lt;" not in out
    assert "D_{JS}<\\tau" in out


def test_polish_markdown_restores_snake_case_function_with_single_letter_segment():
    polished = polish_markdown("调用 <latex>apply_p_rope</latex> 完成旋转。")

    assert "`apply_p_rope`" in polished
    assert "<latex>apply_p_rope</latex>" not in polished


def test_polish_markdown_repairs_single_backslash_cases_spacing_break():
    out = polish_markdown(
        r"<latex>\begin{cases}a=1 \[4pt] b=2\end{cases}</latex>"
    )

    assert r"<latex>\begin{cases}a=1 \\[4pt] b=2\end{cases}</latex>" in out


def test_polish_markdown_keeps_array_formula_with_mm_row_break():
    out = polish_markdown(
        r"<latex>\begin{array}{l}Y_C=Attn(Q_C,K_C,V_C),\\[1mm]Y_X=Attn(Q_X,K_X,V_X)\end{array}</latex>"
    )

    assert r"<latex>\begin{array}{l}Y_C=Attn(Q_C,K_C,V_C),\\[1mm]Y_X=Attn(Q_X,K_X,V_X)\end{array}</latex>" in out
    assert "`\\begin{array}" not in out


def test_polish_markdown_keeps_r_stitch_cases_formula_across_repeated_normalization():
    formula = (
        r"<latex>\mathrm{Switch}(t)=\begin{cases}"
        r"\mathrm{SLM}\rightarrow\mathrm{LLM} & \mathrm{if}\mathcal{H}_t^{\mathrm{SLM}}>\tau,\\ "
        r"\mathrm{LLM}\rightarrow\mathrm{SLM} & \mathrm{if}\mathcal{H}_t^{\mathrm{LLM}}\le \tau."
        r"\end{cases}</latex>"
    )

    once = polish_markdown(formula)
    twice = polish_markdown(once)
    xml = markdown_to_docx_xml(twice)

    assert once == twice
    assert r"\begin{cases}" in twice
    assert "<latex>" in xml
    assert "<code>" not in xml


def test_polish_markdown_does_not_treat_rvert_as_rv_vector_macro():
    source = r"<latex>\lvert\mathrm{Compile}(C)\rvert\le B_G</latex>"

    polished = polish_markdown(source)

    assert r"\lvert\mathrm{Compile}(C)\rvert" in polished
    assert r"\mathbf{ert}" not in polished
    assert "<code>" not in markdown_to_docx_xml(polished)


def test_polish_markdown_recovers_historical_rvert_corruption_when_delimiters_prove_intent():
    source = r"<latex>\lvert\mathrm{Compile}(C)\mathbf{ert}\le B_G</latex>"

    polished = polish_markdown(source)

    assert r"\lvert\mathrm{Compile}(C)\rvert" in polished
    assert "<code>" not in markdown_to_docx_xml(polished)


def test_polish_markdown_keeps_escaped_currency_out_of_inline_math_and_table_columns():
    source = r"""
主指标 CumReg、\$Total、Perf/\$；成本约 \$0.054/M。

| Mode | AvgPerf% | CumReg↓ | \$Total | Perf/\$↑ |
| --- | --- | --- | --- | --- |
| Agent | 62.50 | 17.0 | 52.97 | 1.18 |
"""

    once = polish_markdown(source)
    twice = polish_markdown(once)
    xml = markdown_to_docx_xml(twice)

    assert once == twice
    assert "`$Total`" in twice
    assert "`Perf/$`↑" in twice
    assert "`$0.054/M`" in twice
    assert "<latex>Total" not in twice
    assert "<code>$Total</code>" in xml
    assert "<code>Perf/$</code>↑" in xml
    assert xml.count("<td>") == 10


def test_polish_markdown_expands_legacy_cal_declarations():
    source = r"公式：<latex>{\cal T}_\sigma + v_{\cal S}(x)</latex>"

    out = polish_markdown(source)

    assert r"\mathcal{T}_\sigma" in out
    assert r"v_{\mathcal{S}}(x)" in out
    assert r"\cal" not in out


def test_polish_markdown_preserves_valid_latex_commands():
    text = r"$\left(x^\top\right)$ $\infty + \int_0^1 f(x)dx$ $\left\{x: A^\top x\right\}$ $\simCLR$ $q\to p$"
    out = polish_markdown(text)
    assert "\\le ft" not in out
    assert "^\\to p" not in out
    assert "\\left(x^\\top\\right)" in out
    assert "\\infty + \\int_0^1 f(x)dx" in out
    assert "\\left\\{x: A^\\top x\\right\\}" in out
    assert "\\simCLR" in out
    assert "q\\to p" in out
    assert "q\\top" not in out


def test_polish_markdown_sanitizes_invalid_latex_blocks():
    out = polish_markdown(r"坏公式 <latex>\frac{x}{y</latex> 继续。")
    assert "<latex>\\frac{x}{y</latex>" not in out
    assert "`\\frac{x}{y`" in out


def test_polish_markdown_cleans_cjk_math_code_without_leaking_tex():
    out = polish_markdown(r"路由集合：`\mathcal S_i=\{j:|i-j|\le w\}\cup\{全局锚点\}`")

    assert r"\mathcal" not in out
    assert r"\le" not in out
    assert "全局锚点" in out
    assert "S_i" in out


def test_polish_markdown_normalizes_common_paper_macros():
    out = polish_markdown(r"$\mX \in \R^{N\times d}, \gI_i=\TopK(\mS), \Ls_{\rm KL}=\KL(P\|Q), \sg(P), \softmax(x)$")
    assert r"\mathbf{X}" in out
    assert r"\mathbb{R}" in out
    assert r"\mathcal{I}" in out
    assert r"\mathcal{L}_{\mathrm{KL}}" in out
    assert r"\mathrm{KL}" in out
    assert r"\mathrm{sg}" in out
    assert r"\mathrm{softmax}" in out


def test_polish_markdown_uses_feishu_friendly_latex_subset():
    text = r"$\mathbf X\in \mathbb R^{N\times d_{\rm model}}$ $$M=\max_{\substack{j\in \mathcal B_b\\j\le i}}S,\qquadM_2=\TopK(x)$$"
    out = polish_markdown(text)
    assert r"\rm" not in out
    assert r"\substack" not in out
    assert r"\qquadM" not in out
    assert r"d_{\mathrm{model}}" in out
    assert r"\max_{j\in \mathcal B_b, j\le i}" in out
    assert r"\qquad{}M_2=\mathrm{TopK}(x)" in out


def test_polish_markdown_keeps_spacing_command_boundary_after_feishu_round_trip():
    out = polish_markdown(r"<latex>a=1,\quad w=2</latex>")

    assert r"\quad{}w" in out
    assert r"\quad w" not in out


def test_polish_markdown_removes_tex_formatting_commands_from_prose():
    out = polish_markdown(r"**图：\textbf{The pipeline}**，\textit{plain}，\textbfThe rest")

    assert r"\textbf" not in out
    assert r"\textit" not in out
    assert "The pipeline" in out
    assert "The rest" in out


def test_polish_markdown_repairs_fused_accents_and_html_inside_formula():
    out = polish_markdown(r"图注：$\overlineQ,\hatV<br/>$")

    assert r"<latex>\overline{Q},\hat{V}</latex>" in out
    assert "<br" not in out


def test_polish_markdown_preserves_commands_that_begin_with_accent_names():
    out = polish_markdown(r"$a_1,\dots,a_n;\doteq;\vector;\checkmark;\hat x;\hatV$")

    assert r"\dots" in out
    assert r"\dot{s}" not in out
    assert r"\doteq" in out
    assert r"\vector" in out
    assert r"\checkmark" in out
    assert r"\hat{x}" in out
    assert r"\hat{V}" in out


def test_polish_markdown_removes_escaped_html_inside_formula_in_one_pass():
    out = polish_markdown(r"公式：<latex>x &amp;lt;br/&amp;gt; y &lt;/p&gt;</latex>")

    assert "&lt;br" not in out
    assert "<br" not in out
    assert "</p>" not in out
    assert r"<latex>x   y</latex>" in out


def test_polish_markdown_flattens_backticked_and_nested_latex_wrappers():
    out = polish_markdown(r"先看 `<latex>R_i</latex>`，再看 <latex><latex>x+y</latex></latex>。")

    assert "`<latex>" not in out
    assert "<latex><latex>" not in out
    assert "</latex></latex>" not in out
    assert r"<latex>R_i</latex>" in out
    assert r"<latex>x+y</latex>" in out


def test_polish_markdown_recovers_backticked_math_from_review_output():
    text = r"矩阵 `Q` 的奇异值为 `\sigma_1\ge\cdots\ge\sigma_d`，区间 `500\le\Delta\le5000`，阈值 `κ`，坐标 `(i,j)`。"

    out = polish_markdown(text)

    assert "`" not in out
    assert r"<latex>Q</latex>" in out
    assert r"<latex>\sigma_1\ge \cdots\ge \sigma_d</latex>" in out
    assert r"<latex>500\le \Delta\le5000</latex>" in out
    assert r"<latex>\kappa</latex>" in out
    assert r"<latex>(i,j)</latex>" in out


def test_polish_markdown_keeps_real_code_spans_as_code():
    text = '运行 `pip install torch`，然后调用 `print("ok")`。'

    out = polish_markdown(text)

    assert "`pip install torch`" in out
    assert '`print("ok")`' in out


def test_polish_markdown_restores_reviewed_api_identifiers_to_code():
    text = (
        r"调用 <latex>tensor_meta()</latex>、<latex>on_worker</latex> 和 "
        r"<latex>publish(req_id)</latex>；数学量仍包括 <latex>x_i</latex>、"
        r"<latex>\lambda_max</latex> 与 <latex>f(x)</latex>。"
    )

    out = polish_markdown(text)
    xml = markdown_to_docx_xml(out)

    assert "`tensor_meta()`" in out
    assert "`on_worker`" in out
    assert "`publish(req_id)`" in out
    assert r"<latex>x_i</latex>" in out
    assert r"<latex>\lambda_{\max}</latex>" in out or r"<latex>\lambda_max</latex>" in out
    assert r"<latex>f(x)</latex>" in out
    assert "<code>tensor_meta()</code>" in xml
    assert "<latex>tensor_meta()</latex>" not in xml


def test_polish_markdown_repairs_split_latex_commands_before_validation():
    text = r"<latex>\bar{a}=\mathrm{softmax}\le ft(\hat{Q}K^\to p/\sqrt d\right),\quad \text{s.t.}\ D_{JS}<\tau</latex>"
    out = polish_markdown(text)
    assert "<code>" not in markdown_to_docx_xml(out)
    assert r"\le ft" not in out
    assert r"\to p" not in out
    assert r"\left(\hat{Q}K^\top/\sqrt d\right)" in out
    assert r"\mathrm{s.t.}" in out


def test_polish_markdown_normalizes_minimax_bm_macros():
    out = polish_markdown(r"$\bmo_t^{(h)}+\bmq_t+\bmk_i+\bmX+\bmO_i+\bmQ^{idx}$")
    assert r"\bmo" not in out
    assert r"\bmq" not in out
    assert r"\bmk" not in out
    assert r"\bmX" not in out
    assert r"\mathbf{o}_t" in out
    assert r"\mathbf{X}" in out


def test_polish_markdown_normalizes_nested_bm_math_macros():
    out = polish_markdown(r"$\bm{\mathcal{I}}_i+\bm{\mathrm{x}}+\bm{\mathbb{R}}$")

    assert r"\bm" not in out
    assert r"\mathcal{I}_i" in out
    assert r"\mathrm{x}" in out
    assert r"\mathbb{R}" in out
    assert "<code>" not in markdown_to_docx_xml(out)


def test_polish_markdown_normalizes_minimax_tensor_macros():
    out = polish_markdown(r"$\rvx=\rmX\erva+\tA+\etLambda+\tens{K}+\etens{V}+\mathsfit{Q}$")
    for macro in (r"\rvx", r"\rmX", r"\erva", r"\tA", r"\etLambda", r"\tens", r"\etens", r"\mathsfit"):
        assert macro not in out
    assert r"\mathbf{x}" in out
    assert r"\mathbf{X}" in out
    assert r"\mathrm{a}" in out


def test_polish_markdown_normalizes_position_transformer_macros():
    out = polish_markdown(r"$\matrix{A}=\tr{(\matrix{X}\matrix{W}^{(k)})}+\vector{b}$")
    for macro in (r"\matrix", r"\vector", r"\tr"):
        assert macro not in out
    assert r"\mathbf{A}" in out
    assert r"{(\mathbf{X}\mathbf{W}^{(k)})}^\top" in out
    assert r"\mathbf{b}" in out


def test_polish_markdown_expands_source_defined_custom_macros():
    out = polish_markdown(
        r"\formername 的核心公式是 $\mat{X}\in\RR,\ \T{\mat{K}}V$。",
        custom_macros={"formername": "TransNormer"},
        latex_macros={"RR": r"\mathbb{R}"},
        latex_arg_macros={"mat": r"\mathbf{#1}", "T": r"{#1}^{\top}"},
    )
    xml = markdown_to_docx_xml(
        out,
        latex_macros={"RR": r"\mathbb{R}"},
        latex_arg_macros={"mat": r"\mathbf{#1}", "T": r"{#1}^{\top}"},
    )

    assert r"\formername" not in out
    assert "TransNormer 的核心公式" in out
    assert r"\mat{" not in out
    assert r"\RR" not in out
    assert r"\T" not in out
    assert r"\mathbf{X}\in \mathbb{R}" in out
    assert r"{\mathbf{K}}^{\top}V" in out
    assert "<code>" not in xml


def test_polish_markdown_normalizes_text_macros_after_custom_expansion():
    out = polish_markdown(
        r"$\model+\RR$",
        latex_macros={"model": r"\textsc{TransNormer}\xspace", "RR": r"\mathbb{R}"},
    )

    assert r"\textsc" not in out
    assert r"\xspace" not in out
    assert r"\mathrm{TransNormer}" in out
    assert r"\mathbb{R}" in out


def test_polish_markdown_repairs_collapsed_cases_rows():
    out = polish_markdown(
        r"$\matrix{P}_{tj}=\begin{cases}\sin(t), & j\mathrm{even}  \cos(t), & j\mathrm{odd}\end{cases}$"
    )
    assert r"\matrix" not in out
    assert r"\begin{cases}\sin(t) & j\mathrm{even} \\ \cos(t) & j\mathrm{odd}\end{cases}" in out


def test_polish_markdown_strips_formula_labels_and_tags():
    out = polish_markdown(r"$$E=mc^2\tag{1}\label{eq:mass}\nonumber$$")
    assert r"\tag" not in out
    assert r"\label" not in out
    assert r"\nonumber" not in out


def test_polish_markdown_normalizes_fasa_latex_for_feishu():
    text = r"""
<latex>\mathrm{CA}_{\mathcal{K}}^{l,h,i}(q_t,\mathbf{K}_{1:t})=\frac{\mathrm{TopK\mbox{-}I}(\boldsymbol{\alpha}_{l,h},\mathcal{K})}{\mathcal{K}}</latex>
<latex>\mathcal{T}_t = \operatorname{TopK\mbox{-}I}(\mathbf{S}_t^{l,h}, N_{\mathrm{fac}})</latex>
<latex>\mathbf{S}_t^{l,h}\triangleq\sum_{i\in \mathcal{I}_{\mathrm{dom}}^{l,h}}\boldsymbol{\alpha}^{l,h,i}</latex>
"""
    out = polish_markdown(text)
    assert r"\mbox" not in out
    assert r"\operatorname" not in out
    assert r"\boldsymbol" not in out
    assert r"\triangleq" in out
    assert r"\mathrm{TopK-I}" in out
    assert r"\mathrm{TopK\mathrm{-}I}" not in out
    assert r"\alpha" in out
    assert "<code>" not in markdown_to_docx_xml(out)


def test_polish_markdown_preserves_rightarrow_and_text_spaces():
    text = r"""
<latex>Q_{\mathrm{AR}} \rightarrow (K_{\mathrm{AR}}, V_{\mathrm{AR}})\ \mathrm{with\ causal\ mask}</latex>
<latex>Q_{\mathrm{DM}} \rightarrow ([K_{\mathrm{AR}};K_{\mathrm{DM}}], [V_{\mathrm{AR}};V_{\mathrm{DM}}])\ \mathrm{with\ bidirectional\ mask}</latex>
"""
    out = polish_markdown(text)
    xml = markdown_to_docx_xml(out)
    assert r"\rightarrow" in out
    assert r"\mathrm{with causal mask}" in out
    assert r"\mathrm{with bidirectional mask}" in out
    assert r"\ causal" not in out
    assert r"\ bidirectional" not in out
    assert "<code>" not in xml
    assert xml.count("<latex>") == 2


def test_markdown_to_docx_xml_sanitizes_latex_before_publishing():
    md = r"公式：<latex>S=1,\qquadM=\max_{\substack{j\in \mathcal{B}_b\\j\le i}}S</latex>"
    xml = markdown_to_docx_xml(md)
    assert r"\substack" not in xml
    assert r"\qquadM" not in xml
    assert r"\qquad{}M=\max_{j\in \mathcal{B}_b, j\le i}S" in xml


def test_markdown_to_docx_xml_downgrades_latex_definition_commands():
    md = r"公式：<latex>\newcommand{\RR}{\mathbb{R}} x\in\RR</latex>"
    xml = markdown_to_docx_xml(md)
    assert "<latex>" not in xml
    assert "<code>" in xml
    assert r"\newcommand" in xml


def test_markdown_to_docx_xml_preserves_latex_and_tables():
    md = """# 标题

公式如下：<latex>x < y</latex>

| 方法 | 分数 |
| --- | --- |
| A | <latex>a+b</latex> |
"""
    xml = markdown_to_docx_xml(md)
    assert "<title>标题</title>" in xml
    assert "<h1>标题</h1>" not in xml
    assert "<latex>x &lt; y</latex>" in xml
    assert "<table>" in xml
    assert "<latex>a+b</latex>" in xml


def test_two_column_table_fills_document_width_with_weighted_columns():
    markdown = """| 项目 | 设置 |
| --- | --- |
| 任务 | 给定首帧、文本与相机轨迹 |
"""

    xml = markdown_to_docx_xml(markdown)

    assert '<col width="384"/><col width="816"/>' in xml
    assert xml.startswith("<table><colgroup>")


def test_wide_table_uses_readable_columns_and_horizontal_scroll_width():
    header = "| " + " | ".join(f"C{i}" for i in range(10)) + " |"
    separator = "| " + " | ".join("---" for _ in range(10)) + " |"
    row = "| " + " | ".join(str(i) for i in range(10)) + " |"

    xml = markdown_to_docx_xml("\n".join((header, separator, row)))

    assert xml.count('<col width="120"/>') == 10


def test_polish_markdown_compiles_raw_uncertainty_values_inside_tables():
    markdown = r"""| Gaia DR3 | M1 | M2 |
| --- | --- | --- |
| 40041022325608704 | 1.28^{+0.11}_{-0.10} | 1.27_{-0.12}^{+0.13} |
"""

    polished = polish_markdown(markdown)
    xml = markdown_to_docx_xml(polished)

    assert r"<latex>1.28^{+0.11}_{-0.10}</latex>" in polished
    assert r"<latex>1.27_{-0.12}^{+0.13}</latex>" in polished
    assert "<td><p>40041022325608704</p></td>" in xml
    assert r"<td><p><latex>1.28^{+0.11}_{-0.10}</latex></p></td>" in xml


def test_polish_markdown_splits_adjacent_tables_without_blank_line():
    markdown = """| Encoding | Property |
| --- | --- |
| RoPE | relative |
| Dataset | PPL | Score |
| --- | ---: | ---: |
| Wiki | 10.2 | 88 |
"""

    polished = polish_markdown(markdown)
    xml = markdown_to_docx_xml(polished)

    assert "| RoPE | relative |\n\n| Dataset | PPL | Score |" in polished
    assert xml.count("<table>") == 2


def test_table_formula_vertical_bars_do_not_split_into_extra_cells():
    markdown = r"""| 语义 | 约束 |
| --- | --- |
| 组内 | <latex>|\mathcal{T}_D|=|\mathcal{T}_A|\le q_e</latex> |
"""

    polished = polish_markdown(markdown)
    xml = markdown_to_docx_xml(polished)

    assert r"<latex>\vert \mathcal{T}_D\vert =\vert \mathcal{T}_A\vert \le q_e</latex>" in xml
    assert "&lt;latex&gt;" not in xml
    assert xml.count("<td>") == 4


def test_accuracy_vs_budget_figure_is_assigned_to_experiments():
    target = _figure_section_target(
        Path("llava15_llava_next_acc_vs_budget_1collum.png"),
        "Aggregate performance under different visual-token reduction ratios.",
    )

    assert target == "experiments"


def test_display_caption_localizes_performance_figures():
    assert display_caption(
        "Aggregate performance under a 94.4% visual-token reduction ratio.",
        Path("acc_drop_ratio_comparison_v5_3.png"),
    ) == "性能对比图"
    assert display_caption(
        "Aggregate performance under different visual-token reduction ratios.",
        Path("acc_vs_budget.png"),
    ) == "视觉 Token 削减性能图"


def test_render_asset_converts_eps_with_ghostscript(tmp_path, monkeypatch):
    source = tmp_path / "figure.eps"
    source.write_text("%!PS-Adobe-3.0 EPSF-3.0", encoding="ascii")
    output_dir = tmp_path / "rendered"
    output_dir.mkdir()

    monkeypatch.setattr("maxread.render.shutil.which", lambda name: "/usr/bin/gs" if name == "gs" else None)

    def fake_run(argv, **_kwargs):
        output = next(item.split("=", 1)[1] for item in argv if item.startswith("-sOutputFile="))
        Path(output).write_bytes(b"png")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("maxread.render.subprocess.run", fake_run)

    rendered = _render_asset(source, output_dir)

    assert rendered == output_dir / "figure.png"
    assert rendered.read_bytes() == b"png"


def test_markdown_to_docx_xml_keeps_multiline_latex_with_norm_bars_intact():
    md = "<latex>d=\\left\\|x\\right\\|\n=1</latex>"

    xml = markdown_to_docx_xml(md)

    assert "&lt;latex&gt;" not in xml
    assert "<latex>d=\\left\\|x\\right\\|\n=1</latex>" in xml


def test_polish_markdown_downgrades_unsupported_big_middle_delimiters():
    markdown = r"""合法候选进入全局池：

<latex>\mathcal{V}=\bigcup_{t=1}^{T}\Big\{(\alpha_{t,i},e_{t,i})\;\middle|\;\mathrm{Valid}(d_{t,i})=1\Big\}</latex>
"""

    polished = polish_markdown(markdown)
    xml = markdown_to_docx_xml(polished)

    assert r"\Big" not in polished
    assert r"\middle" not in polished
    assert r"\{(\alpha_{t,i},e_{t,i})\;\mid \;\mathrm{Valid}(d_{t,i})=1\}" in polished
    assert "<latex>" in xml
    assert "<table>" not in xml
    assert "<code>" not in xml


def test_markdown_to_docx_xml_accepts_loose_table_rows():
    md = """# 标题

| 框架 | 延迟 | 加速比
| --- | ---: | ---:
| Base | 10.0 | 1.0×
| Fast | 4.0 | 2.5×
"""
    xml = markdown_to_docx_xml(md)
    assert "<table>" in xml
    assert "Fast" in xml
    assert "2.5×" in xml


def test_markdown_to_docx_xml_keeps_breaks_and_title():
    xml = markdown_to_docx_xml("# Paper Title\n\nA<br/>B")
    assert "<title>Paper Title</title>" in xml
    assert "A<br/>B" in xml
    assert "&lt;br" not in xml


def test_markdown_to_docx_xml_avoids_break_immediately_after_latex():
    xml = markdown_to_docx_xml(
        "实现上，<latex>apply_p_rope</latex>\n"
        "先计算 <latex>rope_angles = int(rope_percentage * head_dim // 2)</latex>。"
    )

    assert "</latex><br/>" not in xml
    assert "<code>apply_p_rope</code><br/>先计算" in xml


def test_markdown_to_docx_xml_heading_with_following_lines():
    xml = markdown_to_docx_xml("# Paper Title\n**English Title**\nAuthors")
    assert "<title>Paper Title</title>" in xml
    assert "<h1>Paper Title</h1>" not in xml
    assert "# Paper Title" not in xml
    assert "<b>English Title</b><br/>Authors" in xml


def test_markdown_to_docx_xml_splits_adjacent_heading_and_table():
    xml = markdown_to_docx_xml(
        "### 5.2 近似策略消融\n"
        "| 模型 | 方法 | SSIM |\n"
        "| --- | --- | --- |\n"
        "| FLUX.1-dev | PISA-0th | 0.643 |"
    )

    assert "<h3>5.2 近似策略消融</h3>" in xml
    assert "<table>" in xml
    assert "FLUX.1-dev" in xml
    assert "| 模型 |" not in xml


def test_figure_markers_append_only_missing():
    inserts = figure_placeholders([(Path("overview.png"), "overview caption"), (Path("ablation.png"), "ablation caption")])
    md = "正文\n\n[MaxReadFigure:1:overview]\n"
    out = ensure_figure_markers(md, inserts, max_missing_append=1)
    assert out.count("[MaxReadFigure:1:overview]") == 1
    assert "[MaxReadFigure:2:ablation]" in out


def test_figure_markers_do_not_append_by_default():
    inserts = figure_placeholders([(Path("overview.png"), "overview caption")])
    out = ensure_figure_markers("正文\n", inserts)
    assert "图表补充" not in out
    assert "MaxReadFigure" not in out


def test_figure_markers_can_append_all_missing():
    inserts = figure_placeholders([(Path("overview.png"), "overview caption"), (Path("method.png"), "method caption")])
    out = ensure_figure_markers("正文\n", inserts, max_missing_append=len(inserts))
    assert "图表补充" in out
    assert "[MaxReadFigure:1:overview]" in out
    assert "[MaxReadFigure:2:method]" in out



def test_ensure_priority_figure_markers_inserts_missing_main_near_method():
    inserts = figure_placeholders([(Path("introfig.png"), "Main method overview."), (Path("ablation.png"), "Ablation.")])
    md = "# T\n\n## 3. 方法框架\n方法正文。\n"
    out = ensure_priority_figure_markers(md, inserts, max_missing=1)
    assert "[MaxReadFigure:1:introfig]" in out
    assert "[MaxReadFigure:2:ablation]" not in out
    assert out.index("[MaxReadFigure:1:introfig]") < out.index("方法正文")


def test_ensure_priority_figure_markers_does_not_treat_training_run_as_method_overview():
    inserts = figure_placeholders([(Path("fig_training_run.png"), "Left: Plot of pretrain loss over the 800B tokens on the main run. Right: Plot of val ppl.")])
    out = ensure_priority_figure_markers("# T\n\n## 2. 核心观察\n正文。\n", inserts, max_missing=1)
    assert "MaxReadFigure" not in out


def test_ensure_priority_figure_markers_does_not_treat_main_categories_as_method_overview():
    inserts = figure_placeholders([(Path("main_categories.png"), "Histograms of zero-shot adaptive exits for MMLU categories.")])
    out = ensure_priority_figure_markers("# T\n\n## 2. 核心观察\n正文。\n", inserts, max_missing=1)
    assert "MaxReadFigure" not in out


def test_ensure_priority_figure_markers_uses_visual_description_over_filename():
    inserts = figure_placeholders([(Path("unknown.png"), "A model diagram.")])
    visual = {"[MaxReadFigure:1:unknown]": "图中有输入、两个处理模块和输出箭头，展示整体架构。"}
    out = ensure_priority_figure_markers("# T\n\n## 3. 方法框架\n方法正文。\n", inserts, max_missing=1, visual_descriptions=visual)
    assert "[MaxReadFigure:1:unknown]" in out


def test_ensure_priority_figure_markers_ignores_priority_filename_when_caption_is_metric_plot():
    inserts = figure_placeholders([(Path("overview.png"), "Training loss and validation perplexity over time.")])
    out = ensure_priority_figure_markers("# T\n\n## 2. 核心观察\n正文。\n", inserts, max_missing=1)
    assert "MaxReadFigure" not in out


def test_ensure_referenced_figure_markers_rescues_one_method_overview_only():
    inserts = figure_placeholders([
        (Path("mechanism.png"), "Architecture overview of the method."),
        (Path("ablation.png"), "Appendix ablation results."),
    ])
    out = ensure_referenced_figure_markers("# T\n\n## 3. 方法框架\n方法正文。\n\n## 4. 实验结果\n实验正文。\n", inserts, max_missing=3)
    assert "## 图表补充" not in out
    assert "[MaxReadFigure:1:mechanism]" in out
    assert "[MaxReadFigure:2:ablation]" not in out
    assert out.index("方法正文") < out.index("[MaxReadFigure:1:mechanism]") < out.index("## 4. 实验结果")


def test_ensure_referenced_figure_markers_does_not_rescue_appendix_figures():
    inserts = figure_placeholders([
        (Path("appendix/ablation.png"), "Appendix ablation results."),
    ])
    out = ensure_referenced_figure_markers("# T\n\n## 4. 实验结果\n实验正文。\n\n## 5. 消融与补充分析\n消融正文。\n\n## 6. 局限性\n局限正文。\n", inserts, max_missing=3)
    assert "MaxReadFigure" not in out


def test_ensure_referenced_figure_markers_does_not_rescue_learnable_sink_figures():
    inserts = figure_placeholders([
        (Path("learnable_sink_vis.png"), "Attention received by the learnable sink and the first token after introducing a GPT-OSS-style sink parameter."),
    ])
    out = ensure_referenced_figure_markers(
        "# T\n\n## 3. 方法框架\n方法正文。\n\n## 5. 消融与补充分析\n消融正文。\n\n## 6. 局限性\n局限正文。\n",
        inserts,
        max_missing=3,
    )
    assert "MaxReadFigure" not in out


def test_ensure_referenced_figure_markers_skips_when_document_already_has_images():
    existing = "\n".join([
        "# T",
        "[MaxReadFigure:9:a]",
        "[MaxReadFigure:10:b]",
        "[MaxReadFigure:11:c]",
        "## 3. 方法框架",
        "方法正文。",
    ])
    inserts = figure_placeholders([(Path("architecture.png"), "Architecture overview.")])
    out = ensure_referenced_figure_markers(existing, inserts, max_missing=3)
    assert "[MaxReadFigure:1:architecture]" not in out


def test_ensure_referenced_figure_markers_does_not_append_without_matching_section():
    inserts = figure_placeholders([
        (Path("ablation.png"), "Ablation results."),
    ])
    out = ensure_referenced_figure_markers("# T\n\n正文。\n", inserts, max_missing=3)
    assert "图表补充" not in out
    assert "MaxReadFigure" not in out


def test_prepare_key_figures_prefers_main_text_figures_over_appendix(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "fig" / "appendix").mkdir(parents=True)
    for rel in ["fig/introfig.png", "fig/fig1.png", "fig/appendix/appendix1.png", "fig/appendix/scaleappendix.png"]:
        path = source_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 100), "white").save(path)
    bundle = _bundle(
        source_dir=source_dir,
        source_assets=["fig/appendix/appendix1.png", "fig/appendix/scaleappendix.png", "fig/introfig.png", "fig/fig1.png"],
        source_figures=[
            PaperFigure(asset="fig/introfig.png", caption="Main overview", label="introfig", figure_index=0),
            PaperFigure(asset="fig/fig1.png", caption="First main result", label="fig1", figure_index=1),
            PaperFigure(asset="fig/appendix/appendix1.png", caption="Appendix result", label="appendixfig1", figure_index=7),
            PaperFigure(asset="fig/appendix/scaleappendix.png", caption="Appendix scaling", label="scaleappendix", figure_index=8),
        ],
    )

    figures = prepare_key_figures(bundle, max_figures=2)

    assert [path.name for path, _caption in figures] == ["introfig.png", "fig1.png"]


def test_markdown_to_docx_xml_keeps_structure_after_leading_inline_formula():
    markdown = """# T

<latex>\\Omega</latex> excludes the prefix.

### 3.4 World state

Details.

| Metric | Value |
| --- | ---: |
| Score | 1 |

[MaxReadFigure:1:framework]
"""

    xml = markdown_to_docx_xml(markdown)

    assert "<h3>3.4 World state</h3>" in xml
    assert "<table>" in xml
    assert "<p>[MaxReadFigure:1:framework]</p>" in xml
    assert "<br/>### 3.4" not in xml


def test_prepare_key_figures_prioritizes_training_and_inference_schematics(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "fig").mkdir(parents=True)
    figures = []
    for index, name in enumerate(("result_a", "result_b", "result_c", "teacher_training", "inference")):
        path = source_dir / "fig" / f"{name}.png"
        Image.new("RGB", (160, 100), "white").save(path)
        caption = {
            "teacher_training": "A schematic of the sparse-attention teacher backbone and student training stages.",
            "inference": "The inference pipeline reads a world state bank and runs coarse-to-fine generation.",
        }.get(name, f"Experiment result {index}.")
        figures.append(PaperFigure(asset=f"fig/{name}.png", caption=caption, tex_file="method.tex", figure_index=index))
    bundle = _bundle(source_dir=source_dir, source_figures=figures)

    rendered = prepare_key_figures(bundle, max_figures=2)

    assert {path.name for path, _caption in rendered} == {"teacher_training.png", "inference.png"}

def test_remove_false_material_warning_when_source_has_tables():
    bundle = _bundle(source_tables=["table"])
    md = "# T\n\n**材料不足说明**：实验表缺失。\n\n**TL;DR**：有材料。\n"
    out = remove_false_material_warning(md, bundle)
    assert "材料不足" not in out
    assert "TL;DR" in out


def test_prepare_key_figures_uses_tex_figure_caption(tmp_path):
    source_dir = tmp_path / "source"
    (source_dir / "figures").mkdir(parents=True)
    (source_dir / "figures" / "attention.png").write_bytes(b"png")
    bundle = _bundle(
        source_dir=source_dir,
        source_assets=["figures/attention.png"],
        source_captions=["Wrong FLOPs caption"],
        source_figures=[PaperFigure(asset="figures/attention.png", caption="Different Attention Mechanisms in DiTs.")],
    )
    figures = prepare_key_figures(bundle)
    assert figures[0][1] == "Different Attention Mechanisms in DiTs."


def test_prepare_key_figures_keeps_referenced_figures_under_assets(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    figure_path = source_dir / "presentation" / "assets" / "overview.png"
    figure_path.parent.mkdir(parents=True)
    Image.new("RGB", (120, 80), "white").save(figure_path)
    bundle = _bundle(
        source_dir=source_dir,
        source_assets=["presentation/assets/overview.png"],
        source_figures=[
            PaperFigure(
                asset="presentation/assets/overview.png",
                caption="Overview of the recurrent architecture.",
                tex_file="ms.tex",
                figure_index=0,
            )
        ],
    )

    figures = prepare_key_figures(bundle)

    assert [path.name for path, _caption in figures] == ["overview.png"]


def test_prepare_key_figures_skips_logo_assets_without_caption(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "assets").mkdir(parents=True)
    (source_dir / "figures").mkdir(parents=True)
    Image.new("RGB", (240, 80), "white").save(source_dir / "assets" / "mm.png")
    Image.new("RGB", (100, 100), "white").save(source_dir / "figures" / "msa_arch.png")
    bundle = _bundle(
        source_dir=source_dir,
        source_assets=["assets/mm.png", "figures/msa_arch.png"],
        source_figures=[
            PaperFigure(asset="figures/msa_arch.png", caption="Overview of MSA architecture.", label="fig:msa-arch", figure_index=0),
        ],
    )
    figures = prepare_key_figures(bundle, max_figures=3)
    assert [path.name for path, _caption in figures] == ["msa_arch.png"]


def test_prepare_key_figures_composes_same_label_multi_image_figure(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "Figures").mkdir(parents=True)
    Image.new("RGB", (100, 200), "white").save(source_dir / "Figures" / "left.png")
    Image.new("RGB", (160, 200), "white").save(source_dir / "Figures" / "right.png")
    caption = "(left) Scaled Dot-Product Attention. (right) Multi-Head Attention."
    bundle = _bundle(
        source_dir=source_dir,
        source_assets=["Figures/left.png", "Figures/right.png"],
        source_figures=[
            PaperFigure(asset="Figures/left.png", caption=caption, tex_file="model.tex", label="fig:multi-head-att"),
            PaperFigure(asset="Figures/right.png", caption=caption, tex_file="model.tex", label="fig:multi-head-att"),
        ],
    )

    figures = prepare_key_figures(bundle, max_figures=3)

    assert len(figures) == 1
    assert figures[0][0].name == "fig_multi-head-att.png"
    assert figures[0][1] == caption
    assert figures[0][0].exists()


def test_prepare_key_figures_composes_same_label_pdf_images(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "figures").mkdir(parents=True)
    sample = tmp_path / "sample.png"
    Image.new("RGB", (120, 80), "white").save(sample)
    (source_dir / "figures" / "loss_vs_steps.pdf").write_bytes(sample.read_bytes())
    (source_dir / "figures" / "ppl_over_recur.pdf").write_bytes(sample.read_bytes())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    qlmanage = bin_dir / "qlmanage"
    qlmanage.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "src=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-o\" ]; then shift; out=\"$1\"; else src=\"$1\"; fi\n"
        "  shift\n"
        "done\n"
        "cp \"$src\" \"$out/thumb.png\"\n",
        encoding="utf-8",
    )
    qlmanage.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    try:
        caption = "Left: Plot of pretrain loss. Right: Plot of val ppl."
        bundle = _bundle(
            source_dir=source_dir,
            source_assets=["figures/loss_vs_steps.pdf", "figures/ppl_over_recur.pdf"],
            source_figures=[
                PaperFigure(asset="figures/loss_vs_steps.pdf", caption=caption, tex_file="main.tex", label="fig:training_run", asset_index=0, col=0),
                PaperFigure(asset="figures/ppl_over_recur.pdf", caption=caption, tex_file="main.tex", label="fig:training_run", asset_index=1, col=1),
            ],
        )
        figures = prepare_key_figures(bundle, max_figures=3)
    finally:
        os.environ["PATH"] = old_path

    assert [path.name for path, _caption in figures] == ["fig_training_run.png"]
    assert figures[0][1] == caption
    assert figures[0][0].exists()


def test_prepare_key_figures_defaults_to_all_semantic_figures(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "figures").mkdir(parents=True)
    source_figures = []
    assets = []
    for index in range(7):
        rel = f"figures/result-{index}.png"
        Image.new("RGB", (120, 80), "white").save(source_dir / rel)
        assets.append(rel)
        source_figures.append(PaperFigure(asset=rel, caption=f"Result comparison {index}.", label=f"fig:{index}", figure_index=index))
    bundle = _bundle(source_dir=source_dir, source_assets=assets, source_figures=source_figures)

    figures = prepare_key_figures(bundle)

    assert len(figures) == 7


def test_prepare_key_figures_keeps_all_body_figures_and_excludes_appendix(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "figures").mkdir(parents=True)
    for name in ("method", "result", "appendix"):
        Image.new("RGB", (120, 80), "white").save(source_dir / "figures" / f"{name}.png")
    bundle = _bundle(
        source_dir=source_dir,
        source_assets=[f"figures/{name}.png" for name in ("method", "result", "appendix")],
        source_figures=[
            PaperFigure(asset="figures/method.png", caption="Method overview.", figure_index=0),
            PaperFigure(asset="figures/result.png", caption="Main result comparison.", figure_index=1),
            PaperFigure(asset="figures/appendix.png", caption="Extra examples.", figure_index=2, is_appendix=True),
        ],
    )

    figures = prepare_key_figures(bundle)

    assert {path.name for path, _caption in figures} == {"method.png", "result.png"}


def test_prepare_key_figures_preserves_owner_sidecar(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    (source_dir / "figures").mkdir(parents=True)
    Image.new("RGB", (120, 80), "white").save(source_dir / "figures/method.png")
    bundle = _bundle(
        source_dir=source_dir,
        source_assets=["figures/method.png"],
        source_figures=[
            PaperFigure(
                asset="figures/method.png",
                caption="Method overview.",
                owner_section="method",
                owner_evidence="reference:fig:method:Method",
            )
        ],
    )

    prepared = prepare_key_figures_with_owners(bundle)

    assert [(path.name, owner) for path, _caption, owner in prepared] == [("method.png", "method")]


def test_compose_related_figure_groups_places_similar_figures_side_by_side(tmp_path):
    from PIL import Image

    left = tmp_path / "indexer_topk.png"
    right = tmp_path / "main_attention_topk.png"
    Image.new("RGB", (640, 480), "white").save(left)
    Image.new("RGB", (640, 480), "white").save(right)
    inserts = [
        ("[MaxReadFigure:1:indexer]", left, "Indexer top-k selection probability across layers."),
        ("[MaxReadFigure:2:main]", right, "Main attention top-k selection probability across layers."),
    ]
    visuals = {
        inserts[0][0]: "展示 indexer 在不同层选择的 top-k 索引概率",
        inserts[1][0]: "展示主干注意力在不同层计算的 top-k 索引概率",
    }

    grouped, grouped_visuals = compose_related_figure_groups(inserts, visuals)

    assert len(grouped) == 1
    assert "related-indexer_topk-main_attention_topk" in grouped[0][0]
    assert grouped[0][1].exists()
    assert grouped[0][0] in grouped_visuals
    with Image.open(grouped[0][1]) as image:
        assert image.width > image.height


def test_related_figures_require_same_nonempty_immutable_owner(tmp_path):
    from PIL import Image

    left = tmp_path / "pipeline_a.png"
    right = tmp_path / "pipeline_b.png"
    Image.new("RGB", (320, 240), "white").save(left)
    Image.new("RGB", (320, 240), "white").save(right)
    inserts = [
        ("[MaxReadFigure:1:a]", left, "Pipeline architecture overview."),
        ("[MaxReadFigure:2:b]", right, "Pipeline architecture details."),
    ]
    visuals = {inserts[0][0]: "模型流程", inserts[1][0]: "模型流程细节"}

    different = {inserts[0][0]: "method", inserts[1][0]: "experiments"}
    grouped, _ = compose_related_figure_groups(inserts, visuals, owner_sections=different)
    assert grouped == inserts
    assert different == {inserts[0][0]: "method", inserts[1][0]: "experiments"}

    unknown = {inserts[0][0]: "method", inserts[1][0]: ""}
    grouped, _ = compose_related_figure_groups(inserts, visuals, owner_sections=unknown)
    assert grouped == inserts

    same = {inserts[0][0]: "method", inserts[1][0]: "method"}
    grouped, _ = compose_related_figure_groups(inserts, visuals, owner_sections=same)
    assert len(grouped) == 1
    assert same == {grouped[0][0]: "method"}


def test_owner_compiler_moves_marker_from_results_back_to_method():
    marker = "[MaxReadFigure:1:pipeline]"
    markdown = f"""# T

## 3. 方法框架

方法总览。

## 4. 实验结果

实验正文。

{marker}
图题：方法架构图

## 5. 消融与补充分析

消融正文。
"""

    repaired = enforce_figure_owner_sections(
        markdown,
        [(marker, Path("pipeline.png"), "方法架构图")],
        {marker: "method"},
    )

    assert repaired.index(marker) < repaired.index("## 4. 实验结果")
    assert repaired.count(marker) == 1


def test_compose_related_figure_groups_keeps_unrelated_figures_separate(tmp_path):
    from PIL import Image

    method = tmp_path / "architecture.png"
    result = tmp_path / "accuracy.png"
    Image.new("RGB", (400, 300), "white").save(method)
    Image.new("RGB", (400, 300), "white").save(result)
    inserts = [
        ("[MaxReadFigure:1:method]", method, "Overview of the model architecture and modules."),
        ("[MaxReadFigure:2:result]", result, "Accuracy benchmark on ImageNet."),
    ]

    grouped, _visuals = compose_related_figure_groups(inserts, {})

    assert grouped == inserts


def test_compose_related_figure_groups_does_not_recombine_complete_latex_figures(tmp_path):
    from PIL import Image

    rendered = tmp_path / "rendered_figures"
    rendered.mkdir()
    data = rendered / "fig_data_pairs.png"
    results = rendered / "fig_visual-quality.png"
    Image.new("RGB", (1200, 320), "white").save(data)
    Image.new("RGB", (1200, 1500), "white").save(results)
    inserts = [
        ("[MaxReadFigure:1:data]", data, "Proxy paired data construction."),
        ("[MaxReadFigure:2:results]", results, "Proxy visual quality results."),
    ]
    visuals = {
        inserts[0][0]: "Proxy RGB pairs for training data.",
        inserts[1][0]: "Proxy RGB pairs for visual results.",
    }

    grouped, _visuals = compose_related_figure_groups(inserts, visuals)

    assert grouped == inserts


def test_related_panel_label_falls_back_to_ascii_without_cjk_font(monkeypatch):
    import maxread.render as render_module

    monkeypatch.setattr(render_module, "_cjk_figure_font_path", lambda: "")

    label = render_module._related_panel_label(
        "图中展示不同层的索引概率。",
        "Indexer top-k selection probability across layers.",
        Path("indexer.png"),
        0,
    )

    assert label.startswith("(a) Indexer top-k selection probability")
    assert all(ord(char) < 128 for char in label)


def test_compose_grid_figure_preserves_subfigure_captions(tmp_path):
    from PIL import Image
    from maxread.render import _compose_grid_figure

    items = []
    captions = [
        "Vanilla RoPE / V2PE",
        "MRoPE",
        "VideoRoPE / HoPE",
        "CircleRoPE",
        "IL-RoPE / Omni-RoPE",
        "MHRoPE / MRoPE-I",
    ]
    for index, caption in enumerate(captions):
        path = tmp_path / f"panel-{index}.png"
        Image.new("RGB", (320, 220), "white").save(path)
        items.append(
            (
                path,
                PaperFigure(
                    asset=path.name,
                    caption="Parent caption",
                    panel_caption=caption,
                    label="fig:parent",
                    asset_index=index,
                    row=index // 3,
                    col=index % 3,
                ),
            )
        )

    output = tmp_path / "grid.png"
    assert _compose_grid_figure(items, output, "Parent caption") == output
    with Image.open(output) as image:
        # Two rows each reserve a panel-caption band below the image.
        assert image.height >= 2 * 220 + 2 * 58


def test_pdf_render_cache_invalidates_low_resolution_output(tmp_path, monkeypatch):
    import maxread.render as render_module
    from PIL import Image

    source = tmp_path / "figure.pdf"
    source.write_bytes(b"%PDF-placeholder")

    def render_high_resolution(_source, output):
        Image.new("RGB", (1190, 850), "white").save(output)
        return output

    monkeypatch.setattr(render_module, "_render_pdf_with_pymupdf", render_high_resolution)
    original_which = render_module.shutil.which
    monkeypatch.setattr(
        render_module.shutil,
        "which",
        lambda name: None if name in {"qlmanage", "pdftoppm"} else original_which(name),
    )

    output_dir = tmp_path / "rendered"
    output_dir.mkdir()
    old = output_dir / "figure.png"
    Image.new("RGB", (595, 425), "white").save(old)

    rendered = _render_asset(source, output_dir)

    assert rendered == old
    with Image.open(rendered) as image:
        assert max(image.size) >= 1000


def test_pdf_render_falls_back_when_platform_thumbnailer_times_out(tmp_path, monkeypatch):
    import subprocess

    import maxread.render as render_module
    from PIL import Image

    source = tmp_path / "figure.pdf"
    source.write_bytes(b"%PDF-placeholder")
    output_dir = tmp_path / "rendered"
    output_dir.mkdir()

    def fake_which(name):
        return "/usr/bin/qlmanage" if name == "qlmanage" else None

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("qlmanage", 30)

    def render_fallback(_source, output):
        Image.new("RGB", (1200, 800), "white").save(output)
        return output

    monkeypatch.setattr(render_module.shutil, "which", fake_which)
    monkeypatch.setattr(render_module.subprocess, "run", fake_run)
    monkeypatch.setattr(render_module, "_render_pdf_with_pymupdf", render_fallback)

    rendered = _render_asset(source, output_dir)

    assert rendered == output_dir / "figure.png"
    assert rendered.exists()


def test_constrain_rendered_image_bounds_conversion_output(tmp_path):
    from PIL import Image

    source = tmp_path / "large-conversion.png"
    Image.effect_noise((2400, 1800), 96).convert("RGB").save(source)

    result = constrain_rendered_image(source, max_bytes=300_000, max_side=1600, max_pixels=2_000_000)

    assert result == source
    assert source.stat().st_size <= 300_000
    with Image.open(source) as opened:
        assert max(opened.size) <= 1600
        assert opened.width * opened.height <= 2_000_000


def test_prepare_key_figures_composes_multi_row_grid_figure(tmp_path):
    from PIL import Image

    source_dir = tmp_path / "source"
    methods = ["baseline", "ASA", "STA", "SVG"]
    frames = ["frame_0", "frame_40", "frame_80"]
    figures = []
    assets = []
    for row, frame in enumerate(frames):
        for col, method in enumerate(methods):
            rel = Path("ICLR26") / method / f"{frame}.jpg"
            path = source_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (120, 80), (40 + row * 40, 50 + col * 35, 120)).save(path)
            assets.append(rel.as_posix())
            figures.append(
                PaperFigure(
                    asset=rel.as_posix(),
                    caption="Comparison of generated videos at frame 0,40,80. Each row shows the same frame index across 4 methods.",
                    tex_file="main.tex",
                    label="fig:SSIM_compare",
                    figure_index=3,
                    asset_index=len(figures),
                    row=row,
                    col=col,
                )
            )
    bundle = _bundle(source_dir=source_dir, source_assets=assets, source_figures=figures)

    rendered = prepare_key_figures(bundle, max_figures=4)

    assert len(rendered) == 1
    assert rendered[0][0].name == "fig_SSIM_compare.png"
    assert rendered[0][0].exists()
    with Image.open(rendered[0][0]) as image:
        assert image.width > image.height
        assert image.width >= 1600
        assert image.height >= 900


def _bundle(**kwargs):
    defaults = dict(
        metadata=ArxivMetadata("1", "T", [], "", "", "", [], "", ""),
        pdf_path=None,
        source_path=None,
        source_dir=None,
        source_text="",
        pdf_text="",
    )
    defaults.update(kwargs)
    return PaperBundle(**defaults)


def test_display_caption_shortens_long_english_and_strips_macros():
    caption = r"(a): Qualitative comparison of attention matrices. The proposed \formername produces more similar patterns to vanilla transformer and alleviates attention dilution in early layers."
    out = display_caption(caption, Path("attention_matrix.png"))
    assert len(out) <= 39
    assert "\\formername" not in out
    assert "attention dilution" not in out


def test_pretty_grid_label_strips_engineering_filename_artifacts():
    assert _pretty_grid_label("final_triplet_no_labels_bird", ["final_triplet_no_labels_bird", "final_triplet_no_labels_cartoon"]) == "bird"
    assert _pretty_grid_label("final-triplet-no-labels-cartoon", ["final-triplet-no-labels-bird", "final-triplet-no-labels-cartoon"]) == "cartoon"
