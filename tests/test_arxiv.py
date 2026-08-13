import io
import tarfile

from maxread.arxiv import ArxivClient, _clip_source_with_appendix, _parse_content_range_total, _split_ranges, extract_arxiv_refs


def test_extract_plain_id():
    refs = extract_arxiv_refs("看 2511.19416")
    assert [r.paper_id for r in refs] == ["2511.19416"]


def test_extract_abs_and_pdf_links_dedupes_versions():
    refs = extract_arxiv_refs(
        "https://arxiv.org/abs/2505.18091v2 and https://arxiv.org/pdf/2505.18091.pdf"
    )
    assert [r.paper_id for r in refs] == ["2505.18091"]


def test_extract_html_link_maps_to_versioned_pdf_url():
    refs = extract_arxiv_refs("读 https://arxiv.org/html/2503.08067v1")
    assert [r.paper_id for r in refs] == ["2503.08067"]
    assert [r.url for r in refs] == ["https://arxiv.org/pdf/2503.08067v1"]


def test_extract_multiple_with_noise():
    refs = extract_arxiv_refs(
        "Minimax 之前的文章 https://arxiv.org/pdf/2506.13585.pdf，公式看 2511.19416"
    )
    assert [r.paper_id for r in refs] == ["2506.13585", "2511.19416"]


def test_range_helpers_parse_and_split():
    assert _parse_content_range_total("bytes 0-0/10485760") == 10485760
    assert _parse_content_range_total("bytes */0") == 0
    assert _split_ranges(10, 4) == [(0, 2), (3, 5), (6, 8), (9, 9)]
    assert _split_ranges(3, 8) == [(0, 0), (1, 1), (2, 2)]


def test_clip_source_with_appendix_preserves_appendix_excerpt():
    text = "\\section{Intro}\n" + ("main body\n" * 5000) + "\\appendix\n\\section{Ablation}\nimportant appendix evidence"
    clipped = _clip_source_with_appendix(text, 20_000)
    assert "main body" in clipped
    assert "\\appendix" in clipped
    assert "important appendix evidence" in clipped
    assert "preserved appendix excerpt" in clipped


def test_fetch_source_text_extracts_to_source_dir(tmp_path):
    paper_dir = tmp_path / "papers" / "2604.12946"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2604.12946.source"
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        tex = b"\\section{Intro} hi\\begin{figure}\\includegraphics{figures/a.png}\\caption{Overview figure.}\\end{figure}\\begin{table}\\begin{tabular}{cc}A&B\\end{tabular}\\caption{Main table.}\\end{table}"
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        img = b"png"
        info = tarfile.TarInfo("figures/a.png")
        info.size = len(img)
        tf.addfile(info, io.BytesIO(img))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, source_dir, source_text, source_tree, assets, captions, figures, tables, macros, latex_macros, latex_arg_macros, warnings = client.fetch_source_text("2604.12946", paper_dir)

    assert warnings == []
    assert source_dir == paper_dir / "source"
    assert (source_dir / "main.tex").exists()
    assert "main.tex" in source_tree
    assert assets == ["figures/a.png"]
    assert captions == ["Overview figure.", "Main table."]
    assert len(figures) == 1
    assert figures[0].asset == "figures/a.png"
    assert figures[0].caption == "Overview figure."
    assert len(tables) == 1
    assert "tabular" in tables[0]
    assert "section{Intro}" in source_text
    assert macros == {}
    assert latex_macros == {}
    assert latex_arg_macros == {}


def test_fetch_source_text_extracts_overpic_and_captionof_figures(tmp_path):
    paper_dir = tmp_path / "papers" / "2504.10825"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2504.10825.source"
    tex = br"""
\begin{figure}[t]
  \begin{overpic}[width=\linewidth]{fig/overview.png}
    \put(0,0){Input}
  \end{overpic}
  \captionof{figure}{Overview of the proposed method.}
  \label{fig:overview}
\end{figure}
"""
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        img = b"png"
        info = tarfile.TarInfo("fig/overview.png")
        info.size = len(img)
        tf.addfile(info, io.BytesIO(img))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, assets, captions, figures, _tables, _macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2504.10825", paper_dir)

    assert warnings == []
    assert assets == ["fig/overview.png"]
    assert captions == ["Overview of the proposed method."]
    assert len(figures) == 1
    assert figures[0].asset == "fig/overview.png"
    assert figures[0].caption == "Overview of the proposed method."
    assert figures[0].label == "fig:overview"


def test_fetch_source_text_splits_multiple_captions_in_one_figure_block(tmp_path):
    paper_dir = tmp_path / "papers" / "2508.10774"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2508.10774.source"
    tex = br"""
\begin{figure*}
\includegraphics{figures/a.png}\includegraphics{figures/b.png}
\caption{First visual comparison.}\label{fig:first}
\includegraphics{figures/c.png}\includegraphics{figures/d.png}
\caption{Second visual comparison.}\label{fig:second}
\end{figure*}
"""
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        for name in ["a", "b", "c", "d"]:
            img = b"png"
            info = tarfile.TarInfo(f"figures/{name}.png")
            info.size = len(img)
            tf.addfile(info, io.BytesIO(img))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, _assets, _captions, figures, _tables, _macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2508.10774", paper_dir)

    assert warnings == []
    assert [figure.label for figure in figures] == ["fig:first", "fig:first", "fig:second", "fig:second"]
    assert [figure.caption for figure in figures] == [
        "First visual comparison.",
        "First visual comparison.",
        "Second visual comparison.",
        "Second visual comparison.",
    ]
    assert [figure.figure_index for figure in figures] == [0, 0, 1, 1]


def test_fetch_source_text_groups_subfigures_under_parent_caption(tmp_path):
    paper_dir = tmp_path / "papers" / "2605.15514"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2605.15514.source"
    tex = br"""
\begin{figure*}
\begin{subfigure}{0.31\textwidth}
\includegraphics{figures/a.pdf}
\caption{First subcaption.}\label{fig:a}
\end{subfigure}
\begin{subfigure}{0.31\textwidth}
\includegraphics{figures/b.pdf}
\caption{Second subcaption.}\label{fig:b}
\end{subfigure}
\begin{subfigure}{0.31\textwidth}
\includegraphics{figures/c.pdf}
\caption{Third subcaption.}\label{fig:c}
\end{subfigure}
\caption{Parent caption for all panels.}\label{fig:parent}
\end{figure*}
"""
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        for name in ["a", "b", "c"]:
            pdf = b"%PDF-1.4\n%%EOF"
            info = tarfile.TarInfo(f"figures/{name}.pdf")
            info.size = len(pdf)
            tf.addfile(info, io.BytesIO(pdf))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, _assets, _captions, figures, _tables, _macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2605.15514", paper_dir)

    assert warnings == []
    assert [figure.asset for figure in figures] == ["figures/a.pdf", "figures/b.pdf", "figures/c.pdf"]
    assert [figure.label for figure in figures] == ["fig:parent", "fig:parent", "fig:parent"]
    assert [figure.caption for figure in figures] == ["Parent caption for all panels."] * 3
    assert [figure.figure_index for figure in figures] == [0, 0, 0]
    assert [figure.asset_index for figure in figures] == [0, 1, 2]


def test_fetch_source_text_expands_simple_caption_macros(tmp_path):
    paper_dir = tmp_path / "papers" / "2210.10340"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2210.10340.source"
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        tex = br"\def\formername{\textsc{TransNormer}\xspace}\begin{figure}\includegraphics{figures/a.png}\caption{Architecture overview of the proposed \formername.}\end{figure}"
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        img = b"png"
        info = tarfile.TarInfo("figures/a.png")
        info.size = len(img)
        tf.addfile(info, io.BytesIO(img))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, source_text, _tree, _assets, captions, figures, _tables, macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2210.10340", paper_dir)

    assert warnings == []
    assert macros["formername"] == "TransNormer"
    assert "\\formername" not in source_text
    assert captions == ["Architecture overview of the proposed TransNormer."]
    assert figures[0].caption == "Architecture overview of the proposed TransNormer."


def test_fetch_source_text_extracts_custom_latex_macros(tmp_path):
    paper_dir = tmp_path / "papers" / "2601.00001"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2601.00001.source"
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        tex = br"""
\newcommand{\RR}{\mathbb{R}}
\newcommand{\mat}[1]{\mathbf{#1}}
\def\T#1{{#1}^{\top}}
\newcommand{\pair}[2]{(#1,#2)}
\section{Method} We use $\mat{X}\in\RR$.
"""
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, _assets, _captions, _figures, _tables, _macros, latex_macros, latex_arg_macros, warnings = client.fetch_source_text("2601.00001", paper_dir)

    assert warnings == []
    assert latex_macros["RR"] == r"\mathbb{R}"
    assert latex_arg_macros["mat"] == r"\mathbf{#1}"
    assert latex_arg_macros["T"] == r"{#1}^{\top}"
    assert "pair" not in latex_arg_macros
