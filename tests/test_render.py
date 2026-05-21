import os
from pathlib import Path

from maxread.models import ArxivMetadata, PaperBundle, PaperFigure
from maxread.render import display_caption, ensure_figure_markers, ensure_priority_figure_markers, figure_placeholders, markdown_to_docx_xml, polish_markdown, prepare_key_figures, remove_false_material_warning, _pretty_grid_label


def test_polish_markdown_converts_math():
    text = "# $$\\na=b\\n$$\nInline $x+y$."
    out = polish_markdown(text)
    assert "# $$" not in out
    assert "<latex>\\na=b\\n</latex>" in out or "<latex>a=b</latex>" in out
    assert "<latex>x+y</latex>" in out


def test_polish_markdown_repairs_common_latex_join_errors():
    text = r"$$\tau\sim N,\quadr\sim P$$ eta $5\mathrm{e}{-4}$"
    out = polish_markdown(text)
    assert "\\quadr" not in out
    assert "\\quad r" in out
    assert "5\\times10^{-4}" in out


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


def test_markdown_to_docx_xml_keeps_breaks_and_title():
    xml = markdown_to_docx_xml("# Paper Title\n\nA<br/>B")
    assert "<title>Paper Title</title>" in xml
    assert "A<br/>B" in xml
    assert "&lt;br" not in xml


def test_markdown_to_docx_xml_heading_with_following_lines():
    xml = markdown_to_docx_xml("# Paper Title\n**English Title**\nAuthors")
    assert "<title>Paper Title</title>" in xml
    assert "<h1>Paper Title</h1>" in xml
    assert "# Paper Title" not in xml
    assert "<b>English Title</b><br/>Authors" in xml


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
