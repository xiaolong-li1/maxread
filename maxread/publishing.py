from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Protocol

from .render import display_caption


class ImageDocClient(Protocol):
    def insert_image(self, doc_url: str, image_path: str, caption: str = "", width: int = 720, selection: str = ""):
        ...

    def remove_text(self, doc_url: str, text: str):
        ...


@dataclass
class ImagePublishResult:
    marker: str
    image_path: Path
    inserted: bool = False
    fallback_appended: bool = False
    marker_removed: bool = False
    warnings: List[str] = field(default_factory=list)


def publish_marker_image(feishu: ImageDocClient, doc_url: str, image_path: Path | str, caption: str, marker: str) -> ImagePublishResult:
    """Insert an image for a marker with defensive fallbacks.

    Feishu media insertion has two independent failure modes: locating the
    selection block and binding the uploaded media.  A selected main figure
    should not disappear just because one media call fails, so we normalize the
    raster file and then try selected insertion before appending as a last-resort
    fallback.
    """
    original = Path(image_path)
    safe_path = prepare_feishu_image(original)
    display = display_caption(caption, safe_path)
    result = ImagePublishResult(marker=marker, image_path=safe_path)

    attempts = [
        ("selected", marker, 720),
        ("selected-small", marker, 560),
        ("append-fallback", "", 640),
    ]
    last_error = ""
    for label, selection, width in attempts:
        try:
            feishu.insert_image(doc_url, str(safe_path), caption=display, selection=selection, width=width)
            result.inserted = True
            result.fallback_appended = label == "append-fallback"
            if label != "selected":
                result.warnings.append(f"image-insert-fallback:{original.name}:{label}")
            break
        except Exception as exc:  # keep trying lower-risk placements
            last_error = str(exc)
            result.warnings.append(f"image-insert-failed:{original.name}:{label}:{_short_error(last_error)}")

    if not result.inserted:
        result.warnings.append(f"image-missing:{original.name}:{_short_error(last_error)}")
        return result

    try:
        feishu.remove_text(doc_url, marker)
        result.marker_removed = True
    except Exception as exc:
        result.warnings.append(f"image-marker-remove-failed:{original.name}:{_short_error(str(exc))}")
    return result


def prepare_feishu_image(image_path: Path | str) -> Path:
    """Return an RGB PNG that Feishu's media API tends to accept reliably."""
    path = Path(image_path)
    try:
        from PIL import Image
    except Exception:
        return path
    if not path.exists() or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return path
    safe_dir = path.parent / "feishu_safe"
    safe_path = safe_dir / f"{path.stem}.png"
    try:
        source_mtime = path.stat().st_mtime
        if safe_path.exists() and safe_path.stat().st_mtime >= source_mtime:
            return safe_path
        safe_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as image:
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            background.alpha_composite(image)
            rgb = background.convert("RGB")
            max_side = max(rgb.size)
            if max_side > 2200:
                scale = 2200 / max_side
                rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))), Image.LANCZOS)
            rgb.save(safe_path, format="PNG", optimize=True)
        return safe_path
    except Exception:
        return path


def _short_error(text: str, limit: int = 260) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."
