from pathlib import Path

import fitz

from maxread.document_source import DocumentSourceClient, _pdf_bundle
from maxread.models import PaperRef


def test_pdf_bundle_recovers_text_title_and_captioned_figure(tmp_path):
    pdf_path = tmp_path / "paper" / "document.pdf"
    pdf_path.parent.mkdir(parents=True)
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((60, 70), "A Layout-Aware Technical Report", fontsize=22)
    page.insert_text((60, 120), "Abstract", fontsize=15)
    page.insert_text((60, 145), "We recover text, figures, tables, and their owning sections.", fontsize=10)
    page.insert_text((60, 210), "3 Method", fontsize=16)
    page.draw_rect(fitz.Rect(90, 250, 500, 520), color=(0, 0, 0), fill=(0.9, 0.95, 1.0))
    page.insert_text((150, 380), "input -> encoder -> output", fontsize=13)
    page.insert_text((60, 550), "Figure 1: The complete method pipeline.", fontsize=10)
    document.save(pdf_path)
    document.close()

    ref = PaperRef("pdf-test", "https://raw.githubusercontent.com/example/repo/main/report.pdf")
    bundle = _pdf_bundle(ref, ref.url, pdf_path, [])

    assert bundle.metadata.title == "A Layout-Aware Technical Report"
    assert bundle.metadata.source_kind == "document"
    assert "recover text, figures" in bundle.source_text
    assert len(bundle.source_figures) == 1
    assert bundle.source_figures[0].caption.startswith("Figure 1")
    assert bundle.source_figures[0].owner_section == "3 Method"
    assert (bundle.source_dir / bundle.source_figures[0].asset).exists()


def test_model_card_without_pdf_builds_standard_bundle(tmp_path, monkeypatch):
    client = DocumentSourceClient(tmp_path, timeout=10)
    metadata = b'{"siblings":[{"rfilename":"README.md"}]}'
    readme = b"""# Example Vision Model

## Introduction

This official model card explains the architecture and benchmark evidence in enough detail for a technical reading document.

| Method | Score |
| --- | --- |
| Example | 88.0 |
"""

    def fake_get(url):
        return metadata if "/api/models/" in url else readme

    monkeypatch.setattr(client, "_get", fake_get)
    ref = PaperRef("hf-example", "https://huggingface.co/example/vision-model")
    bundle = client.fetch(ref)

    assert bundle.metadata.title == "Example Vision Model"
    assert bundle.metadata.source_kind == "document"
    assert bundle.source_tables
    assert bundle.source_text.startswith("# Example Vision Model")
