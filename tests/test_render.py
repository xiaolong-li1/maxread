import os
from pathlib import Path

from maxread.models import ArxivMetadata, PaperBundle, PaperFigure
from maxread.render import display_caption, ensure_figure_markers, ensure_priority_figure_markers, ensure_referenced_figure_markers, figure_placeholders, markdown_to_docx_xml, polish_markdown, prepare_key_figures, remove_false_material_warning, _pretty_grid_label


def test_polish_markdown_converts_math():
    text = "# $$\\na=b\\n$$\nInline $x+y$."
    out = polish_markdown(text)
    assert "# $$" not in out
    assert "<latex>\\na=b\\n</latex>" in out or "<latex>a=b</latex>" in out
    assert "<latex>x+y</latex>" in out


def test_markdown_to_docx_xml_keeps_figure_marker_in_its_own_block():
    marker = "[MaxReadFigure:1:overview]"

    xml = markdown_to_docx_xml(f"正文引用。\n{marker}\n**图：方法总览。**")

    assert f"<p>正文引用。</p><p>{marker}</p><p><b>图：方法总览。</b></p>" == xml


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
    assert "<h1>标题</h1>" in xml
    assert "<latex>x &lt; y</latex>" in xml
    assert "<table>" in xml
    assert "<latex>a+b</latex>" in xml


def test_markdown_to_docx_xml_keeps_multiline_latex_with_norm_bars_intact():
    md = "<latex>d=\\left\\|x\\right\\|\n=1</latex>"

    xml = markdown_to_docx_xml(md)

    assert "&lt;latex&gt;" not in xml
    assert "<latex>d=\\left\\|x\\right\\|\n=1</latex>" in xml
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
    assert "</latex> 先计算" in xml


def test_markdown_to_docx_xml_heading_with_following_lines():
    xml = markdown_to_docx_xml("# Paper Title\n**English Title**\nAuthors")
    assert "<title>Paper Title</title>" in xml
    assert "<h1>Paper Title</h1>" in xml
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
