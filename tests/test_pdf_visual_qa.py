import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT = Path(__file__).parents[1] / "deploy" / "visual_qa" / "maxread_pdf_qa.py"
SPEC = importlib.util.spec_from_file_location("maxread_pdf_qa", SCRIPT)
pdf_qa = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(pdf_qa)


def test_pdf_qa_treats_expected_counts_as_telemetry_only():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "persisted_formula_count" in source
    assert "missing-formula" not in source
    assert "_append_missing_inventory" not in source


def test_pdf_qa_detects_visible_raw_formatting():
    assert pdf_qa._raw_formatting_artifacts(r"结果显示 \textbf{bad}") == [r"\textbf"]
    assert pdf_qa._raw_formatting_artifacts("正常渲染内容") == []


def test_pdf_qa_selects_pages_evenly():
    pages = [Path(f"page-{index}.png") for index in range(1, 11)]

    selected = pdf_qa._select_evenly(pages, 4)

    assert [index for index, _path in selected] == [1, 4, 7, 10]


def test_pdf_qa_parses_pretty_multiline_json():
    payload = pdf_qa._last_json('{\n  "ok": true,\n  "data": {"value": 3}\n}')

    assert payload["data"]["value"] == 3


def test_pdf_qa_blank_page_detection(tmp_path):
    blank = tmp_path / "blank.png"
    content = tmp_path / "content.png"
    Image.new("RGB", (800, 1000), "white").save(blank)
    page = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((80, 80, 720, 900), fill="black")
    page.save(content)

    assert pdf_qa._page_is_blank(blank) is True
    assert pdf_qa._page_is_blank(content) is False
