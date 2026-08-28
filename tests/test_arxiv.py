import io
import sys
import tarfile
import types
from pathlib import Path

import maxread.arxiv as arxiv_module
from maxread.arxiv import ArxivClient, _clip_source_with_appendix, _extract_pdf_text_with_python, _extract_tables, _parse_content_range_total, _split_ranges, extract_arxiv_refs


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


def test_extract_tables_keeps_appendix_ablation_tables_beyond_eight():
    text = "\n".join(
        rf"\begin{{table}}\begin{{tabular}}{{cc}}variant-{index}&score\end{{tabular}}\caption{{Ablation {index}.}}\end{{table}}"
        for index in range(12)
    )

    tables = _extract_tables(text)

    assert len(tables) == 12
    assert "variant-11" in tables[-1]


def test_pdf_text_fallback_replaces_isolated_surrogate(monkeypatch, tmp_path):
    import subprocess
    import types
    import maxread.arxiv as arxiv_module

    paper_dir = tmp_path / "papers" / "2503.10696"
    paper_dir.mkdir(parents=True)
    (paper_dir / "2503.10696.pdf").write_bytes(b"%PDF-placeholder")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr="failed"),
    )
    monkeypatch.setattr(arxiv_module, "_extract_pdf_text_with_python", lambda _path: ("math \ud8350 frame", ""))

    _path, text, warnings = ArxivClient(tmp_path).fetch_pdf_text("2503.10696", paper_dir)

    assert text == "math \uFFFD0 frame"
    assert any("isolated Unicode surrogate" in warning for warning in warnings)


def test_fetch_skips_pdf_extraction_when_tex_source_is_available(tmp_path):
    class SourceFirstArxiv(ArxivClient):
        def fetch_metadata(self, paper_id):
            from maxread.models import ArxivMetadata
            return ArxivMetadata(paper_id, "T", [], "A", "", "", [], "pdf", "abs")

        def fetch_source_text(self, paper_id, paper_dir):
            source = paper_dir / f"{paper_id}.source"
            source.write_text("source")
            return source, paper_dir / "source", "\\section{Method} source", "main.tex", [], [], [], [], {}, {}, {}, []

        def fetch_pdf_text(self, paper_id, paper_dir):
            raise AssertionError("PDF extraction must not run when source is available")

    bundle = SourceFirstArxiv(tmp_path).fetch("2503.10696")

    assert bundle.source_text
    assert bundle.pdf_text == ""
    assert bundle.pdf_path is None
    assert any("PDF text extraction skipped" in warning for warning in bundle.parse_warnings)


def test_fetch_continues_source_path_when_metadata_temporarily_fails(tmp_path):
    class MetadataUnavailableClient(ArxivClient):
        def fetch_metadata(self, paper_id):
            raise OSError("connection reset by peer")

        def fetch_source_text(self, paper_id, paper_dir):
            source = paper_dir / f"{paper_id}.source"
            source.write_text("source", encoding="utf-8")
            return source, paper_dir / "source", "\\section{Method} source", "main.tex", [], [], [], [], {}, {}, {}, []

        def fetch_pdf_text(self, paper_id, paper_dir):
            raise AssertionError("PDF fallback must not run when source succeeds")

    bundle = MetadataUnavailableClient(tmp_path).fetch("2608.24646")

    assert bundle.source_text
    assert bundle.metadata.title == "arXiv 2608.24646"
    assert any("continued with source/PDF fallback" in warning for warning in bundle.parse_warnings)


def test_fetch_source_prefers_export_eprint_endpoint(tmp_path):
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as archive:
        content = b"\\section{Method} source"
        info = tarfile.TarInfo("main.tex")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    calls = []

    class SourceEndpointClient(ArxivClient):
        def _download_to_path(self, url, output_path):
            calls.append(url)
            output_path.write_bytes(blob.getvalue())

    client = SourceEndpointClient(tmp_path)
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    result = client.fetch_source_text("2608.24646", paper_dir)

    assert result[0] == paper_dir / "2608.24646.source"
    assert "section{Method}" in result[2]
    assert calls == ["https://export.arxiv.org/e-print/2608.24646"]


def test_get_uses_arxiv_relay_after_direct_connection_failures(monkeypatch, tmp_path):
    client = ArxivClient(tmp_path)
    client.arxiv_relay_url = "http://10.214.232.141:18080"
    monkeypatch.setattr(arxiv_module.time, "sleep", lambda _seconds: None)

    def fail_direct(*_args, **_kwargs):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(arxiv_module.urllib.request, "urlopen", fail_direct)
    monkeypatch.setattr(client, "_get_via_relay", lambda _url: b"relay bytes")

    assert client._get_once("https://arxiv.org/src/2608.24646") == b"relay bytes"


def test_process_wide_arxiv_pacing_interval_is_conservative():
    from maxread import arxiv as module

    assert module._ARXIV_REQUEST_INTERVAL_SECONDS >= 3.0


def test_range_helpers_parse_and_split():
    assert _parse_content_range_total("bytes 0-0/10485760") == 10485760
    assert _parse_content_range_total("bytes */0") == 0
    assert _split_ranges(10, 4) == [(0, 2), (3, 5), (6, 8), (9, 9)]
    assert _split_ranges(3, 8) == [(0, 0), (1, 1), (2, 2)]


def test_python_pdf_text_fallback_uses_pypdf_when_available(tmp_path):
    class FakePage:
        def extract_text(self):
            return "Extracted page text"

    class FakeReader:
        def __init__(self, _path):
            self.pages = [FakePage()]

    fake_pypdf = types.ModuleType("pypdf")
    fake_pypdf.PdfReader = FakeReader
    previous = sys.modules.get("pypdf")
    sys.modules["pypdf"] = fake_pypdf
    try:
        text, warning = _extract_pdf_text_with_python(Path(tmp_path) / "paper.pdf")
    finally:
        if previous is None:
            sys.modules.pop("pypdf", None)
        else:
            sys.modules["pypdf"] = previous

    assert text == "Extracted page text"
    assert warning == ""


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


def test_fetch_source_text_extracts_includegraphics_path_on_following_line(tmp_path):
    paper_dir = tmp_path / "papers" / "2608.07193"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2608.07193.source"
    tex = br"""
\begin{figure}[t]
  \includegraphics[width=\columnwidth]
  {Figures/result.png}
  \caption{Aggregate performance under aggressive pruning.}
  \label{fig:result}
\end{figure}
"""
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        image = b"png"
        info = tarfile.TarInfo("Figures/result.png")
        info.size = len(image)
        tf.addfile(info, io.BytesIO(image))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, _assets, _captions, figures, _tables, _macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2608.07193", paper_dir)

    assert warnings == []
    assert len(figures) == 1
    assert figures[0].asset == "Figures/result.png"
    assert figures[0].caption == "Aggregate performance under aggressive pruning."
    assert figures[0].label == "fig:result"


def test_fetch_source_text_marks_figures_after_appendix_boundary(tmp_path):
    paper_dir = tmp_path / "papers" / "2608.99999"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2608.99999.source"
    tex = br"""
\begin{figure}\includegraphics{fig/main.png}\caption{Main method.}\label{fig:main}\end{figure}
\appendix
\begin{figure}\includegraphics{fig/extra.png}\caption{Extra qualitative examples.}\label{fig:extra}\end{figure}
"""
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        for name in ("main", "extra"):
            image = b"png"
            info = tarfile.TarInfo(f"fig/{name}.png")
            info.size = len(image)
            tf.addfile(info, io.BytesIO(image))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, _assets, _captions, figures, _tables, _macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2608.99999", paper_dir)

    assert warnings == []
    assert [figure.asset for figure in figures] == ["fig/main.png", "fig/extra.png"]
    assert [figure.is_appendix for figure in figures] == [False, True]


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
    assert [figure.panel_caption for figure in figures] == [
        "First subcaption.",
        "Second subcaption.",
        "Third subcaption.",
    ]
    assert [figure.figure_index for figure in figures] == [0, 0, 0]
    assert [figure.asset_index for figure in figures] == [0, 1, 2]


def test_fetch_source_text_expands_parameterized_graphics_wrapper(tmp_path):
    paper_dir = tmp_path / "papers" / "2608.25927"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2608.25927.source"
    tex = br"""
\newcommand{\resultcase}[3]{%
  \begin{minipage}[t]{\textwidth}
    \includegraphics[width=\linewidth]{#2}
    \vspace{-3pt}
    {\scriptsize\textbf{Prompt (#1):} #3\par}
  \end{minipage}}
\begin{figure*}
\resultcase{A}{figures/case_a.png}{First prompt.}
\vspace{2pt}
\resultcase{B}{figures/case_b.png}{Second prompt.}
\caption{Visual quality results.}\label{fig:visual-quality}
\end{figure*}
"""
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        for name in ("case_a", "case_b"):
            image = b"png"
            info = tarfile.TarInfo(f"figures/{name}.png")
            info.size = len(image)
            tf.addfile(info, io.BytesIO(image))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, _assets, _captions, figures, _tables, _macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2608.25927", paper_dir)

    assert warnings == []
    assert [figure.asset for figure in figures] == ["figures/case_a.png", "figures/case_b.png"]
    assert [figure.label for figure in figures] == ["fig:visual-quality"] * 2
    assert [figure.caption for figure in figures] == ["Visual quality results."] * 2
    assert [figure.panel_caption for figure in figures] == [
        "(A) First prompt.",
        "(B) Second prompt.",
    ]
    assert [(figure.row, figure.col) for figure in figures] == [(0, 0), (1, 0)]


def test_fetch_source_text_keeps_hfilled_nested_minipages_on_one_row(tmp_path):
    paper_dir = tmp_path / "papers" / "2608.25928"
    paper_dir.mkdir(parents=True)
    source_path = paper_dir / "2608.25928.source"
    tex = br"""
\begin{figure*}
  \begin{subfigure}{0.495\textwidth}
    \begin{minipage}{0.49\linewidth}
      \includegraphics{figures/game_rgb.png}\vspace{-2pt}{\scriptsize RGB target}
    \end{minipage}\hfill
    \begin{minipage}{0.49\linewidth}
      \includegraphics{figures/game_proxy.png}\vspace{-2pt}{\scriptsize proxy}
    \end{minipage}
    \caption{Game pair data.}
  \end{subfigure}\hfill
  \begin{subfigure}{0.495\textwidth}
    \begin{minipage}{0.49\linewidth}
      \includegraphics{figures/real_rgb.png}\vspace{-2pt}{\scriptsize RGB target}
    \end{minipage}\hfill
    \begin{minipage}{0.49\linewidth}
      \includegraphics{figures/real_proxy.png}\vspace{-2pt}{\scriptsize proxy}
    \end{minipage}
    \caption{Real pair data.}
  \end{subfigure}
  \caption{Paired data construction.}\label{fig:data-pairs}
\end{figure*}
"""
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        info = tarfile.TarInfo("main.tex")
        info.size = len(tex)
        tf.addfile(info, io.BytesIO(tex))
        for name in ("game_rgb", "game_proxy", "real_rgb", "real_proxy"):
            image = b"png"
            info = tarfile.TarInfo(f"figures/{name}.png")
            info.size = len(image)
            tf.addfile(info, io.BytesIO(image))
    source_path.write_bytes(blob.getvalue())

    client = ArxivClient(tmp_path)
    _, _source_dir, _source_text, _tree, _assets, _captions, figures, _tables, _macros, _latex_macros, _latex_arg_macros, warnings = client.fetch_source_text("2608.25928", paper_dir)

    assert warnings == []
    assert [figure.asset for figure in figures] == [
        "figures/game_rgb.png",
        "figures/game_proxy.png",
        "figures/real_rgb.png",
        "figures/real_proxy.png",
    ]
    assert [(figure.row, figure.col) for figure in figures] == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert [figure.panel_caption for figure in figures] == [
        "Game pair data.",
        "Game pair data.",
        "Real pair data.",
        "Real pair data.",
    ]


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
