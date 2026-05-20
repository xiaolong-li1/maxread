from pathlib import Path

from maxread.publishing import prepare_feishu_image, publish_marker_image


class FlakyFeishu:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.insert_calls = []
        self.removed = []

    def insert_image(self, doc_url, image_path, caption="", width=720, selection=""):
        self.insert_calls.append({"doc_url": doc_url, "image_path": image_path, "caption": caption, "width": width, "selection": selection})
        if len(self.insert_calls) <= self.fail_times:
            raise RuntimeError("server internal error")
        return {"ok": True}

    def remove_text(self, doc_url, text):
        self.removed.append(text)
        return {"ok": True}


def _png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGBA", (20, 12), (255, 0, 0, 120)).save(path)
    return path


def test_publish_marker_image_retries_and_removes_marker_after_success(tmp_path):
    image = _png(tmp_path / "main.png")
    feishu = FlakyFeishu(fail_times=1)

    result = publish_marker_image(feishu, "doc", image, "Main overview", "[MaxReadFigure:1:main]")

    assert result.inserted is True
    assert result.marker_removed is True
    assert len(feishu.insert_calls) == 2
    assert feishu.insert_calls[0]["selection"] == "[MaxReadFigure:1:main]"
    assert feishu.insert_calls[1]["width"] == 560
    assert feishu.removed == ["[MaxReadFigure:1:main]"]
    assert any("image-insert-failed:main.png:selected" in item for item in result.warnings)


def test_publish_marker_image_appends_as_last_resort(tmp_path):
    image = _png(tmp_path / "overview.png")
    feishu = FlakyFeishu(fail_times=2)

    result = publish_marker_image(feishu, "doc", image, "Overview", "[MaxReadFigure:1:overview]")

    assert result.inserted is True
    assert result.fallback_appended is True
    assert feishu.insert_calls[-1]["selection"] == ""
    assert feishu.removed == ["[MaxReadFigure:1:overview]"]


def test_publish_marker_image_keeps_marker_when_all_attempts_fail(tmp_path):
    image = _png(tmp_path / "pipeline.png")
    feishu = FlakyFeishu(fail_times=99)

    result = publish_marker_image(feishu, "doc", image, "Pipeline", "[MaxReadFigure:1:pipeline]")

    assert result.inserted is False
    assert feishu.removed == []
    assert any(item.startswith("image-missing:pipeline.png") for item in result.warnings)


def test_prepare_feishu_image_writes_safe_rgb_png(tmp_path):
    image = _png(tmp_path / "rgba.png")

    safe = prepare_feishu_image(image)

    assert safe.name == "rgba.png"
    assert safe.parent.name == "feishu_safe"
    assert safe.exists()
    from PIL import Image

    with Image.open(safe) as opened:
        assert opened.mode == "RGB"
