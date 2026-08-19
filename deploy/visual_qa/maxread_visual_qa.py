from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List

from PIL import Image, ImageStat
from playwright.sync_api import Locator, Page, sync_playwright


INVALID_TEXTS = ("无效公式", "Invalid formula")
STRUCTURAL_SELECTORS = (
    ".docx-text-block, .docx-heading-block, .docx-table-block, "
    ".docx-image-block, .docx-code-block, .docx-bullet-block"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Visual QA for a published Feishu Docx")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-sections", type=int, default=12)
    parser.add_argument("--expected-images", type=int, default=0)
    parser.add_argument("--expected-formulas", type=int, default=0)
    parser.add_argument("--expected-tables", type=int, default=0)
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=1000)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _prune_old_runs(output_dir.parent, current=output_dir, keep=60)
    report: Dict[str, Any] = {
        "status": "ok",
        "url": args.url,
        "title": "",
        "findings": [],
        "screenshots": [],
        "sections_checked": 0,
        "metrics": {},
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(
                viewport={"width": max(1024, args.viewport_width), "height": max(720, args.viewport_height)},
                locale="zh-CN",
                device_scale_factor=1,
            )
            page.goto(args.url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(4_500)
            report["url"] = page.url
            report["title"] = page.title()
            if "accounts.feishu" in page.url or ("登录" in report["title"] and not _has_document_editor(page)):
                report["status"] = "auth_required"
                report["findings"].append(_finding("auth-required", "high", "文档访问跳转到登录页"))
            else:
                _inspect_document(
                    page,
                    output_dir,
                    report,
                    max(1, args.max_sections),
                    expected_images=max(0, args.expected_images),
                    expected_formulas=max(0, args.expected_formulas),
                    expected_tables=max(0, args.expected_tables),
                )
            browser.close()
    except Exception as exc:
        report["status"] = "error"
        report["error"] = _clip(str(exc), 600)

    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # The local coordinator consumes one compact JSON object from stdout.
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] in {"ok", "issues"} else 2


def _inspect_document(
    page: Page,
    output_dir: Path,
    report: Dict[str, Any],
    max_sections: int,
    expected_images: int = 0,
    expected_formulas: int = 0,
    expected_tables: int = 0,
) -> None:
    scroller = _main_scroller(page)
    editor = _editor_box(page)
    _stabilize_document_height(page, scroller)
    inventory = _rendered_object_inventory(page, scroller)
    catalogue = _catalogue_links(page, max_sections)
    catalogue_budget = min(len(catalogue), max(0, (max_sections - 1) // 2))
    catalogue = _select_evenly(catalogue, catalogue_budget)
    scroll_targets = _scroll_targets(scroller, max(0, max_sections - 1 - catalogue_budget))
    targets: List[Dict[str, str]] = [{"section": "开头", "href": ""}]
    for index in range(max(len(catalogue), len(scroll_targets))):
        if index < len(catalogue):
            targets.append(catalogue[index])
        if index < len(scroll_targets):
            targets.append(scroll_targets[index])
    image_sections = [
        {"section": f"图片-{index}", "scroll_top": str(item.get("scroll_top", 0))}
        for index, item in enumerate(inventory["images"], start=1)
    ]
    targets.extend(_select_evenly(image_sections, min(6, len(image_sections))))
    targets = _dedupe_targets(targets)

    findings: List[Dict[str, Any]] = report["findings"]
    screenshots: List[str] = report["screenshots"]
    seen_viewports = set()
    invalid_count = 0
    raw_formatting_count = 0
    overlap_count = 0
    max_overflow = 0.0
    image_count = 0
    samples: List[Dict[str, Any]] = []

    for index, target in enumerate(targets):
        _navigate_target(page, scroller, target, editor)
        page.wait_for_timeout(600)
        scroll_top = _scroll_top(scroller)
        key = round(scroll_top / 120)
        if key in seen_viewports:
            continue
        seen_viewports.add(key)
        samples.append({"section": target.get("section", ""), "scroll_top": round(scroll_top)})

        slug = f"{index:02d}-{_slug(target.get('section') or 'section')}"
        shot_path = output_dir / f"{slug}.png"
        page.screenshot(path=str(shot_path), full_page=False)
        screenshots.append(str(shot_path))
        report["sections_checked"] += 1
        finding_start = len(findings)

        visible_text = _visible_text(page)
        current_invalid = sum(visible_text.count(text) for text in INVALID_TEXTS)
        invalid_contexts = _visible_invalid_formula_contexts(page)
        current_invalid = max(current_invalid, len(invalid_contexts))
        invalid_count += current_invalid
        if current_invalid:
            findings.append(
                _finding(
                    "invalid-formula",
                    "high",
                    f"章节视口出现 {current_invalid} 个无效公式提示",
                    section=target.get("section", ""),
                    autofixable=True,
                    data={"contexts": invalid_contexts[:8]},
                )
            )
        raw_artifacts = _raw_formatting_artifacts(visible_text)
        raw_formatting_count += len(raw_artifacts)
        if raw_artifacts:
            findings.append(
                _finding(
                    "raw-formatting",
                    "high",
                    "页面显示了格式化控制字符：" + ", ".join(raw_artifacts[:4]),
                    section=target.get("section", ""),
                    autofixable=True,
                )
            )

        blocks = _visible_blocks(page)
        overlaps = _detect_overlaps(blocks)
        overlap_count += len(overlaps)
        if overlaps:
            findings.append(
                _finding(
                    "block-overlap",
                    "high",
                    f"检测到 {len(overlaps)} 处正文 block 垂直重叠",
                    section=target.get("section", ""),
                    data={"pairs": overlaps[:4]},
                )
            )
        for image in _visible_images(page):
            image_count += 1
            name = image.get("name") or image.get("block_id") or "image"
            overflow = float(image.get("right", 0)) - float(editor.get("right", 0))
            max_overflow = max(max_overflow, overflow)
            if overflow > 12 or float(image.get("left", 0)) < float(editor.get("left", 0)) - 12:
                findings.append(
                    _finding(
                        "image-overflow",
                        "high",
                        f"图片超出正文区域 {round(max(overflow, 0))}px",
                        section=target.get("section", ""),
                        image_name=name,
                        block_id=str(image.get("block_id") or ""),
                        autofixable=True,
                        data={"editor_width": round(float(editor.get("width", 0))), "render_width": round(float(image.get("width", 0)))},
                    )
                )
            if float(image.get("width", 0)) > float(editor.get("width", 0)) * 0.94:
                findings.append(
                    _finding(
                        "image-too-wide",
                        "medium",
                        "图片接近占满正文宽度，建议复核可读性",
                        section=target.get("section", ""),
                        image_name=name,
                        block_id=str(image.get("block_id") or ""),
                        data={"editor_width": round(float(editor.get("width", 0))), "render_width": round(float(image.get("width", 0)))},
                    )
                )
            image_box = _crop_image_box(shot_path, image, page.viewport_size or {})
            if image_box:
                blank = _edge_blank_ratios(image_box)
                severe_blank = blank["total"] > 0.72 or blank["bottom"] > 0.62 or blank["top"] > 0.55
                notable_blank = blank["total"] > 0.46 or blank["bottom"] > 0.38 or blank["top"] > 0.30
                if notable_blank:
                    findings.append(
                        _finding(
                            "image-large-white-border",
                            "high" if severe_blank else "medium",
                            f"图片内容周围空白过大（总空白约 {round(blank['total'] * 100)}%）",
                            section=target.get("section", ""),
                            image_name=name,
                            block_id=str(image.get("block_id") or ""),
                            data=blank,
                        )
                    )
        if _viewport_is_mostly_blank(shot_path, editor):
            findings.append(
                _finding(
                    "large-empty-viewport",
                    "medium",
                    "章节视口正文区域大面积空白，可能存在图片画布或 block 高度异常",
                    section=target.get("section", ""),
                )
            )
        for finding in findings[finding_start:]:
            finding.setdefault("screenshot", str(shot_path))

    rendered_images = len(inventory["images"])
    rendered_formulas = len(inventory["formulas"])
    rendered_tables = len(inventory["tables"])
    count_screenshot = screenshots[0] if screenshots else ""
    for kind, actual, expected, label in (
        ("missing-image", rendered_images, expected_images, "图片"),
        ("missing-formula", rendered_formulas, expected_formulas, "公式"),
        ("missing-table", rendered_tables, expected_tables, "表格"),
    ):
        if expected and actual < expected:
            findings.append(
                _finding(
                    kind,
                    "high",
                    f"真实页面只渲染出 {actual}/{expected} 个{label}",
                    screenshot=count_screenshot,
                    data={"actual": actual, "expected": expected},
                )
            )
    scrollable_table_count = 0
    for table in inventory["tables"]:
        if int(table.get("rows", 0)) < 1 or int(table.get("cells", 0)) < 1:
            findings.append(
                _finding(
                    "table-render-failed",
                    "high",
                    "表格节点存在，但没有渲染出有效行列",
                    screenshot=count_screenshot,
                    data={"rows": table.get("rows", 0), "cells": table.get("cells", 0)},
                )
            )
        overflow = float(table.get("right", 0)) - float(editor.get("right", 0))
        if overflow > 12 or float(table.get("left", 0)) < float(editor.get("left", 0)) - 12:
            if table.get("horizontally_scrollable"):
                scrollable_table_count += 1
            else:
                findings.append(
                    _finding(
                        "table-clipped",
                        "high",
                        f"表格超出正文区域 {round(max(overflow, 0))}px，且没有可用的横向滚动容器",
                        screenshot=count_screenshot,
                        data={"editor_width": round(float(editor.get("width", 0))), "render_width": round(float(table.get("width", 0)))},
                    )
                )

    report["findings"] = _dedupe_findings(findings)
    report["status"] = "issues" if report["findings"] else "ok"
    report["metrics"] = {
        "invalid_formula_count": invalid_count,
        "raw_formatting_count": raw_formatting_count,
        "overlap_count": overlap_count,
        "image_observations": image_count,
        "rendered_image_count": rendered_images,
        "rendered_formula_count": rendered_formulas,
        "rendered_table_count": rendered_tables,
        "scrollable_table_count": scrollable_table_count,
        "expected_image_min": expected_images,
        "expected_formula_min": expected_formulas,
        "expected_table_min": expected_tables,
        "max_image_overflow_px": round(max_overflow, 1),
        "scroll_height": round(_scroll_height(scroller)),
        "scroll_client_height": round(_scroll_client_height(scroller)),
    }
    report["samples"] = samples


def _main_scroller(page: Page) -> Locator:
    preferred = page.locator(".bear-web-x-container").first
    if preferred.count():
        try:
            if preferred.evaluate("el => el.scrollHeight > el.clientHeight + 300"):
                return preferred
        except Exception:
            pass
    candidates = page.locator("*")
    handle = candidates.evaluate_all(
        """els => {
          const rows = els.map((e, i) => { const r=e.getBoundingClientRect(), s=getComputedStyle(e); return {
            i, area:r.width*r.height, h:r.height, sh:e.scrollHeight, oy:s.overflowY, cls:String(e.className||'')};
          }).filter(x => x.h > 500 && x.sh > x.h + 500 && ['scroll','auto'].includes(x.oy));
          rows.sort((a,b) => b.area-a.area); return rows[0]?.i ?? -1;
        }"""
    )
    if handle < 0:
        return page.locator("body")
    return candidates.nth(handle)


def _editor_box(page: Page) -> Dict[str, float]:
    locator = page.locator(".page-main-item.editor, .editor-container").first
    box = locator.bounding_box()
    if not box:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        return {"left": 260.0, "right": float(viewport["width"] - 120), "width": float(viewport["width"] - 380)}
    return {"left": box["x"], "right": box["x"] + box["width"], "width": box["width"]}


def _catalogue_links(page: Page, limit: int) -> List[Dict[str, str]]:
    links = page.locator("a[href^='#']").evaluate_all(
        r"""els => els.map(e => ({href:e.getAttribute('href')||'', section:(e.innerText||'').trim()}))
          .filter(x => /^#doxcn/.test(x.href) && /^\d+(?:\.\d+)?[.\s]/.test(x.section))"""
    )
    major = [item for item in links if re.match(r"^\d+[.\s]", item["section"])]
    sub = [item for item in links if item not in major]
    return (major + sub)[:limit]


def _image_section_targets(page: Page, scroller: Locator) -> List[Dict[str, str]]:
    height = _scroll_height(scroller)
    if height <= 0:
        return []
    targets = []
    steps = min(18, max(4, math.ceil(height / 700)))
    for index in range(steps + 1):
        top = round(height * index / steps)
        _set_scroll(scroller, top)
        page.wait_for_timeout(160)
        images = _visible_images(page)
        if images:
            targets.append({"section": f"图片-{len(targets) + 1}", "scroll_top": str(top)})
    _set_scroll(scroller, 0)
    return targets


def _scroll_targets(scroller: Locator, count: int) -> List[Dict[str, str]]:
    height = _scroll_height(scroller)
    viewport_height = _scroll_client_height(scroller) or 900.0
    max_top = max(0.0, height - viewport_height)
    if max_top < 240 or count <= 0:
        return []
    return [
        {"section": f"全文采样-{index}", "scroll_top": str(round(max_top * index / count))}
        for index in range(1, count + 1)
    ]


def _navigate_target(page: Page, scroller: Locator, target: Dict[str, str], editor: Dict[str, float]) -> None:
    if "scroll_top" in target:
        _set_scroll(scroller, int(target["scroll_top"]))
        return
    href = target.get("href", "")
    if not href:
        _set_scroll(scroller, 0)
        return
    link = page.locator(f"a[href='{href}']").first
    if link.count():
        try:
            link.click(timeout=3_000)
            return
        except Exception:
            pass
    # Catalogue links are the authoritative mapping between section and block
    # position. Use their ordered index when Feishu's click handler stalls.
    catalogue = page.locator("a[href^='#doxcn']").evaluate_all(
        "els => els.map(e => e.getAttribute('href') || '')"
    )
    try:
        index = catalogue.index(href)
    except ValueError:
        index = 0
    height = _scroll_height(scroller)
    _set_scroll(scroller, height * index / max(1, len(catalogue) - 1))


def _visible_text(page: Page) -> str:
    editor = page.locator(".page-main-item.editor, .editor-container").first
    if not editor.count():
        return str(page.locator("body").inner_text(timeout=10_000))
    return str(
        editor.evaluate(
            """element => { const clone=element.cloneNode(true);
              clone.querySelectorAll('pre, code').forEach(node => node.remove());
              return clone.innerText || clone.textContent || ''; }"""
        )
    )


def _visible_blocks(page: Page) -> List[Dict[str, Any]]:
    return page.locator(STRUCTURAL_SELECTORS).evaluate_all(
        """els => els.map((e, i) => { const r=e.getBoundingClientRect(); return {
          i, cls:String(e.className||'').slice(0,100), text:String(e.innerText||e.textContent||'').trim().slice(0,80),
          top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:r.width,height:r.height};
        }).filter(x => x.width > 20 && x.height > 5 && x.bottom > 64 && x.top < window.innerHeight)"""
    )


def _visible_images(page: Page) -> List[Dict[str, Any]]:
    return page.locator("img.docx-image").evaluate_all(
        """els => els.map(e => { const r=e.getBoundingClientRect(), u=new URL(e.src, location.href); return {
          left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,
          natural_width:e.naturalWidth,natural_height:e.naturalHeight,
          name:u.searchParams.get('name') || e.getAttribute('alt') || '',
          block_id:u.searchParams.get('mount_node_token') || '', src:e.src};
        }).filter(x => x.width > 20 && x.height > 20 && x.bottom > 64 && x.top < window.innerHeight)"""
    )


def _visible_formulas(page: Page) -> List[Dict[str, Any]]:
    editor = page.locator(".page-main-item.editor, .editor-container").first
    if not editor.count():
        return []
    # Feishu renders a formula as one equation block containing a KaTeX tree.
    # Count the block, not its internal .mord/.mrel spans.
    return editor.locator(".docx-equation-block").evaluate_all(
        """els => els.map(e => { const r=e.getBoundingClientRect();
          const rendered = e.querySelector('.katex, .katex-html, .equation-katex-span');
          return {
          left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,
          text:String(e.textContent || '').trim().slice(0,200),
          html:String(e.outerHTML || '').slice(0,500),
          rendered:Boolean(rendered && rendered.getBoundingClientRect().width > 1 && rendered.getBoundingClientRect().height > 1)};
        }).filter(x => x.rendered && x.width > 1 && x.height > 1 && x.bottom > 64 && x.top < window.innerHeight)"""
    )


def _visible_invalid_formula_contexts(page: Page) -> List[Dict[str, str]]:
    editor = page.locator(".page-main-item.editor, .editor-container").first
    if not editor.count():
        return []
    return editor.locator("*").evaluate_all(
        """els => { const rows = els.filter(el => String(el.textContent || '').trim() === '无效公式' ||
          String(el.textContent || '').trim() === 'Invalid formula').map(el => {
          let current = el;
          let block = '';
          let context = '';
          for (let i = 0; i < 6 && current; i++, current = current.parentElement) {
            for (const key of ['data-block-id','data-block-token','data-node-id','data-token','data-mount-node-token']) {
              if (!block && current.getAttribute && current.getAttribute(key)) block = current.getAttribute(key);
            }
            if (!context && current.parentElement) context = String(current.parentElement.innerText || '').trim().slice(0, 360);
          }
          return {block_id: block, context};
        }); return rows.filter((row, index) => rows.findIndex(other =>
          other.block_id === row.block_id && other.context === row.context) === index).slice(0, 8); }"""
    )


def _visible_tables(page: Page) -> List[Dict[str, Any]]:
    editor = page.locator(".page-main-item.editor, .editor-container").first
    if not editor.count():
        return []
    return editor.locator("table").evaluate_all(
        """els => els.map(e => { const r=e.getBoundingClientRect();
          let current=e.parentElement, horizontallyScrollable=false, scrollContainerWidth=0, scrollContentWidth=0;
          for (let depth=0; current && depth<8; depth++, current=current.parentElement) {
            const style=getComputedStyle(current);
            const canScroll=current.scrollWidth > current.clientWidth + 4 && !['hidden','clip'].includes(style.overflowX);
            if (canScroll) {
              horizontallyScrollable=true; scrollContainerWidth=current.clientWidth; scrollContentWidth=current.scrollWidth; break;
            }
          }
          return {
          left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,
          rows:e.querySelectorAll('tr').length,cells:e.querySelectorAll('th,td').length,
          horizontally_scrollable:horizontallyScrollable,
          scroll_container_width:scrollContainerWidth,scroll_content_width:scrollContentWidth,
          text:String(e.innerText || e.textContent || '').trim().slice(0,300)};
        }).filter(x => x.width > 1 && x.height > 1 && x.bottom > 64 && x.top < window.innerHeight)"""
    )


def _rendered_object_inventory(page: Page, scroller: Locator) -> Dict[str, List[Dict[str, Any]]]:
    height = _scroll_height(scroller)
    client_height = _scroll_client_height(scroller) or 900.0
    max_top = max(0.0, height - client_height)
    steps = min(48, max(4, math.ceil(max_top / max(320.0, client_height * 0.55))))
    images: Dict[str, Dict[str, Any]] = {}
    formulas: Dict[str, Dict[str, Any]] = {}
    tables: Dict[str, Dict[str, Any]] = {}
    for index in range(steps + 1):
        requested_top = round(max_top * index / max(1, steps))
        _set_scroll(scroller, requested_top)
        page.wait_for_timeout(180)
        scroll_top = _scroll_top(scroller)
        for item in _visible_images(page):
            key = str(item.get("block_id") or item.get("src") or _object_position_key("image", item, scroll_top))
            images[key] = {**item, "scroll_top": round(scroll_top)}
        for item in _visible_formulas(page):
            key = _object_position_key("formula", item, scroll_top)
            formulas[key] = {**item, "scroll_top": round(scroll_top)}
        for item in _visible_tables(page):
            key = _object_position_key("table", item, scroll_top)
            tables[key] = {**item, "scroll_top": round(scroll_top)}
    _set_scroll(scroller, 0)
    page.wait_for_timeout(240)
    return {"images": list(images.values()), "formulas": list(formulas.values()), "tables": list(tables.values())}


def _object_position_key(kind: str, item: Dict[str, Any], scroll_top: float) -> str:
    absolute_top = round((float(item.get("top", 0)) + scroll_top) / 4) * 4
    left = round(float(item.get("left", 0)) / 4) * 4
    content = str(item.get("text") or item.get("html") or item.get("src") or "")[:120]
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{absolute_top}:{left}:{digest}"


def _has_document_editor(page: Page) -> bool:
    return page.locator(".page-main-item.editor, .editor-container, .bear-web-x-container").count() > 0


def _detect_overlaps(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(blocks, key=lambda item: (float(item["top"]), float(item["left"])))
    overlaps = []
    for previous, current in zip(ordered, ordered[1:]):
        horizontal = min(previous["right"], current["right"]) - max(previous["left"], current["left"])
        vertical = min(previous["bottom"], current["bottom"]) - max(previous["top"], current["top"])
        if horizontal > 80 and vertical > 10:
            nested = (
                previous["top"] <= current["top"] + 2
                and previous["bottom"] >= current["bottom"] - 2
                and previous["left"] <= current["left"] + 2
                and previous["right"] >= current["right"] - 2
            ) or (
                current["top"] <= previous["top"] + 2
                and current["bottom"] >= previous["bottom"] - 2
                and current["left"] <= previous["left"] + 2
                and current["right"] >= previous["right"] - 2
            )
            if nested:
                continue
            # Parent/child wrappers share the same rectangle; only compare
            # structural blocks whose class identifies different block types.
            if previous["cls"] == current["cls"] and abs(previous["top"] - current["top"]) < 2:
                continue
            overlaps.append({"a": previous["text"], "b": current["text"], "px": round(vertical)})
    return overlaps


def _raw_formatting_artifacts(text: str) -> List[str]:
    patterns = [
        r"\\(?:textbf|textit|textsc|mathrm|operatorname|mathbf|mathcal)(?:\b|(?=[A-Z]))",
        r"(?<!\\)\$\$",
        r"\\\(|\\\)|\\\[|\\\]",
        r"(?m)^\s*\|\s*[-:]+(?:\s*\|\s*[-:]+)+\s*\|?\s*$",
        r"(?<![\w.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:\^\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}\s*_\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}|_\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\}\s*\^\s*\{\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*\})(?![\w.])",
    ]
    found = []
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            found.append(match.group(0)[:50])
    return found


def _crop_image_box(screenshot: Path, image: Dict[str, Any], viewport: Dict[str, int]) -> Image.Image | None:
    try:
        with Image.open(screenshot) as shot:
            left = max(0, min(shot.width - 1, round(float(image["left"]))))
            top = max(0, min(shot.height - 1, round(float(image["top"]))))
            right = max(left + 1, min(shot.width, round(float(image["right"]))))
            bottom = max(top + 1, min(shot.height, round(float(image["bottom"]))))
            if right - left < 30 or bottom - top < 30:
                return None
            return shot.convert("RGB").crop((left, top, right, bottom))
    except Exception:
        return None


def _edge_blank_ratios(image: Image.Image) -> Dict[str, float]:
    gray = image.convert("L")
    mask = gray.point(lambda value: 255 if value < 245 else 0)
    box = mask.getbbox()
    if not box:
        return {"top": 1.0, "bottom": 1.0, "left": 1.0, "right": 1.0, "total": 1.0}
    left, top, right, bottom = box
    width, height = image.size
    content_area = max(1, right - left) * max(1, bottom - top)
    total = 1.0 - content_area / max(1, width * height)
    return {
        "top": round(top / height, 4),
        "bottom": round((height - bottom) / height, 4),
        "left": round(left / width, 4),
        "right": round((width - right) / width, 4),
        "total": round(total, 4),
    }


def _viewport_is_mostly_blank(screenshot: Path, editor: Dict[str, float]) -> bool:
    try:
        with Image.open(screenshot) as image:
            left = max(0, round(editor["left"]))
            right = min(image.width, round(editor["right"]))
            top = 80
            bottom = image.height - 20
            crop = image.convert("L").crop((left, top, right, bottom))
            pixels = ImageStat.Stat(crop).mean[0]
            dark = crop.point(lambda value: 255 if value < 242 else 0)
            ratio = ImageStat.Stat(dark).mean[0] / 255
            return pixels > 247 and ratio < 0.012
    except Exception:
        return False


def _scroll_height(scroller: Locator) -> float:
    try:
        return float(scroller.evaluate("el => el.scrollHeight"))
    except Exception:
        return 0.0


def _scroll_client_height(scroller: Locator) -> float:
    try:
        return float(scroller.evaluate("el => el.clientHeight"))
    except Exception:
        return 0.0


def _stabilize_document_height(page: Page, scroller: Locator) -> None:
    previous = 0.0
    stable_rounds = 0
    for _ in range(7):
        height = _scroll_height(scroller)
        _set_scroll(scroller, height)
        page.wait_for_timeout(420)
        updated = _scroll_height(scroller)
        if abs(updated - height) < 40 and abs(updated - previous) < 40:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        previous = updated
    _set_scroll(scroller, 0)
    page.wait_for_timeout(300)


def _scroll_top(scroller: Locator) -> float:
    try:
        return float(scroller.evaluate("el => el.scrollTop"))
    except Exception:
        return 0.0


def _set_scroll(scroller: Locator, top: float) -> None:
    try:
        scroller.evaluate("(el, top) => { el.scrollTop = top; el.dispatchEvent(new Event('scroll')); }", top)
    except Exception:
        pass


def _finding(kind: str, severity: str, detail: str, **fields: Any) -> Dict[str, Any]:
    return {"kind": kind, "severity": severity, "detail": detail, **fields}


def _dedupe_targets(items: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    output = []
    for item in items:
        key = item.get("href") or item.get("scroll_top") or item.get("section")
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _select_evenly(items: List[Dict[str, str]], limit: int) -> List[Dict[str, str]]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[len(items) // 2]]
    indexes = [round(index * (len(items) - 1) / (limit - 1)) for index in range(limit)]
    return [items[index] for index in indexes]


def _dedupe_findings(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = (item.get("kind"), item.get("section"), item.get("image_name"), item.get("detail"))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _slug(value: str) -> str:
    ascii_part = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:36]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part or 'section'}-{digest}"


def _clip(value: str, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _prune_old_runs(parent: Path, current: Path, keep: int) -> None:
    try:
        runs = sorted(
            (item for item in parent.iterdir() if item.is_dir() and item != current),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in runs[max(0, keep - 1) :]:
            shutil.rmtree(stale, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
