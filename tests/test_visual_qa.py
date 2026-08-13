from __future__ import annotations

from maxread.visual_qa import (
    RemoteVisualResult,
    VisualFinding,
    VisualQAController,
    _last_json_object,
    repair_image_findings,
    repair_structural_blocks,
)


class FakeVisualFeishu:
    def __init__(self, content: str):
        self.content = content
        self.replacements = []

    def fetch_docx(self, doc_url, doc_format="xml", scope="", detail="simple"):
        return {"data": {"document": {"content": self.content}}}

    def block_replace(self, doc_url, block_id, content):
        self.replacements.append((doc_url, block_id, content))
        return {"ok": True}


class StubVisualQA(VisualQAController):
    def __init__(self, results):
        super().__init__(enabled=True, max_repairs=2)
        self.results = list(results)
        self.calls = []

    def inspect_remote(self, doc_url: str, source_id: str = "") -> RemoteVisualResult:
        self.calls.append(source_id)
        return self.results.pop(0)


def test_repair_structural_blocks_downgrades_code_like_formula():
    feishu = FakeVisualFeishu(
        '<title>T</title><p id="bad">调用 <latex>apply_p_rope<br /></latex> 后继续。</p>'
        '<p id="good">普通正文不应被替换。</p>'
    )

    changed, warnings, blocks = repair_structural_blocks(
        feishu,
        "https://tenant.feishu.cn/docx/doc",
        ["post-publish:quality:formula:xml:high:html-tag-in-formula"],
    )

    assert changed is True
    assert blocks == ["bad"]
    assert warnings == ["visual-repair:structural-block:bad"]
    assert len(feishu.replacements) == 1
    replacement = feishu.replacements[0][2]
    assert '<code>apply_p_rope</code>' in replacement
    assert 'id="bad"' not in replacement


def test_repair_structural_blocks_normalizes_joined_spacing_command():
    feishu = FakeVisualFeishu('<title>T</title><p id="bad"><latex>a=1,\\quadw=2</latex></p>')

    changed, _warnings, blocks = repair_structural_blocks(
        feishu,
        "doc",
        ["post-publish:quality:formula:xml:high:joined-spacing-command"],
    )

    assert changed is True
    assert blocks == ["bad"]
    assert "\\quad{}w" in feishu.replacements[0][2]


def test_repair_structural_blocks_strips_fused_raw_tex_but_preserves_latex():
    feishu = FakeVisualFeishu(
        '<title>T</title><p id="caption">图：\\textbfThe algorithm pipeline</p>'
        '<p id="formula"><latex>\\mathrm{rank}(X)</latex></p>'
        '<p id="code">示例 <code>\\textbf{literal}</code></p>'
    )

    changed, _warnings, blocks = repair_structural_blocks(
        feishu,
        "doc",
        ["visual-qa:repairable-structural"],
    )

    assert changed is True
    assert blocks == ["caption"]
    assert "\\textbf" not in feishu.replacements[0][2]
    assert "The algorithm pipeline" in feishu.replacements[0][2]


def test_repair_image_findings_maps_dom_block_id_and_preserves_ratio():
    feishu = FakeVisualFeishu(
        '<title>T</title><img id="image-block" token="token-1" name="figure.png" width="900" height="450"/>'
    )
    finding = VisualFinding(
        kind="image-overflow",
        severity="high",
        block_id="image-block",
        autofixable=True,
        data={"editor_width": 820},
    )

    changed, _warnings, blocks = repair_image_findings(feishu, "doc", [finding], max_repairs=1)

    assert changed is True
    assert blocks == ["image-block"]
    replacement = feishu.replacements[0][2]
    assert 'token="token-1"' in replacement
    assert 'width="672"' in replacement
    assert 'height="336"' in replacement
    assert 'id="image-block"' not in replacement


def test_controller_rechecks_after_remote_structural_repair():
    feishu = FakeVisualFeishu('<title>T</title><p id="caption">\\textbfThe pipeline</p>')
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="raw-formatting", severity="high", autofixable=True)],
            ),
            RemoteVisualResult(status="ok"),
        ]
    )

    result = controller.run(feishu, "doc", source_id="paper")

    assert result.changed is True
    assert result.repaired_blocks == ["caption"]
    assert controller.calls == ["paper", "paper-formula-recheck"]
    assert not any(warning.startswith("visual-qa:high:") for warning in result.warnings)


def test_last_json_object_ignores_ssh_banner():
    payload = _last_json_object('banner\n{"status":"ok","findings":[]}\n')

    assert payload == {"status": "ok", "findings": []}
