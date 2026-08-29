from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import maxread.visual_qa as visual_qa_module
from deploy.visual_qa.maxread_pdf_qa import _doc_token, _export_ticket
from maxread.visual_qa import (
    RemoteVisualResult,
    VisualFinding,
    VisualQAController,
    VisualRepairResult,
    VisualRepairRound,
    _cleanup_successful_visual_runs,
    _last_json_object,
    _visual_qa_concurrency,
    repair_image_findings,
    repair_structural_blocks,
)


def test_successful_visual_run_cleanup_is_scoped_to_run_directory(tmp_path):
    root = tmp_path / "browser"
    run = root / "runs" / "paper-a"
    outside = tmp_path / "outside"
    run.mkdir(parents=True)
    outside.mkdir()
    screenshot = run / "page-1.png"
    screenshot.write_bytes(b"png")
    outside_file = outside / "keep.png"
    outside_file.write_bytes(b"png")
    result = VisualRepairResult(
        remote=RemoteVisualResult(status="ok", screenshots=[str(screenshot)]),
        rounds=[VisualRepairRound(round_index=0, status="passed", screenshots=[str(screenshot), str(outside_file)])],
    )

    removed = _cleanup_successful_visual_runs(str(root), result)

    assert removed == 1
    assert not run.exists()
    assert outside_file.exists()


def test_visual_qa_concurrency_defaults_to_one_and_validates_env(monkeypatch):
    monkeypatch.delenv("MAXREAD_VISUAL_QA_CONCURRENCY", raising=False)
    assert _visual_qa_concurrency() == 1
    monkeypatch.setenv("MAXREAD_VISUAL_QA_CONCURRENCY", "3")
    assert _visual_qa_concurrency() == 3
    monkeypatch.setenv("MAXREAD_VISUAL_QA_CONCURRENCY", "invalid")
    assert _visual_qa_concurrency() == 1


def test_pdf_export_ticket_and_doc_token_are_recoverable_from_cli_output():
    output = "Created export task: 7679279762142366688 Export task is still in progress. Continue with: lark-cli drive +task_result --scenario export --ticket 7679279762142366688"

    assert _export_ticket(output) == "7679279762142366688"
    assert _doc_token("https://tenant.feishu.cn/docx/MS1LdHHGUoXGvQxAeb7cidB4nVf") == "MS1LdHHGUoXGvQxAeb7cidB4nVf"


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

    def inspect_remote(self, doc_url: str, source_id: str = "", **kwargs) -> RemoteVisualResult:
        self.calls.append(source_id)
        return self.results.pop(0)


def test_export_pending_is_reported_as_infrastructure_not_visual_finding():
    controller = StubVisualQA([
        RemoteVisualResult(
            status="infrastructure_pending",
            error="Feishu PDF export is still processing; ticket=123",
        )
    ])

    result = controller.run(FakeVisualFeishu("<title>T</title>"), "https://tenant/docx/doc", source_id="paper")

    assert result.passed is False
    assert result.remote.status == "infrastructure_pending"
    assert any(item.startswith("visual-qa:infrastructure:export-pending:") for item in result.warnings)


class FormulaRepairLLM:
    def __init__(self):
        self.calls = 0
        self.users = []

    def responses_text(self, system, user, **kwargs):
        self.calls += 1
        self.users.append(user)
        return '{"repairs":[{"id":"formula","mode":"latex","value":"a=1\\\\text{ok}"}]}'


class RetryFormulaRepairLLM(FormulaRepairLLM):
    def responses_text(self, system, user, **kwargs):
        self.calls += 1
        self.users.append(user)
        if self.calls == 1:
            raise TimeoutError("upstream 524")
        return '{"repairs":[{"id":"formula","mode":"latex","value":"a=1"}]}'


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


def test_repair_structural_blocks_compiles_raw_uncertainty_inside_table():
    feishu = FakeVisualFeishu(
        '<title>T</title><table id="table-block"><tr><td><p>1.28^{+0.11}_{-0.10}</p></td></tr></table>'
    )

    changed, warnings, blocks = repair_structural_blocks(
        feishu,
        "doc",
        ["visual-qa:repairable-structural"],
    )

    assert changed is True
    assert warnings == ["visual-repair:structural-block:table-block"]
    assert blocks == ["table-block"]
    assert "<latex>1.28^{+0.11}_{-0.10}</latex>" in feishu.replacements[0][2]


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
    assert controller.calls == ["paper", "paper-visual-r1"]
    assert not any(warning.startswith("visual-qa:high:") for warning in result.warnings)


def test_controller_accepts_nonblocking_visual_findings_without_repair_loop():
    feishu = FakeVisualFeishu("<title>T</title><table><tr><td><p>wide</p></td></tr></table>")
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[
                    VisualFinding(kind="table-clipped", severity="high", detail="388px"),
                    VisualFinding(kind="formula-count-drift", severity="medium", detail="12/13"),
                    VisualFinding(kind="image-large-white-border", severity="high", detail="80%"),
                ],
            )
        ]
    )

    result = controller.run(
        feishu,
        "doc",
        source_id="paper",
        previous_feedback=["visual round 0: invalid-formula: unsupported macro remained"],
    )

    assert result.passed is True
    assert controller.calls == ["paper"]
    assert result.rounds[0].status == "passed-with-warnings"
    assert result.warnings == [
        "visual-qa:medium:table-clipped:388px",
        "visual-qa:medium:formula-count-drift:12/13",
        "visual-qa:medium:image-large-white-border:80%",
    ]
    assert feishu.replacements == []


def test_controller_downgrades_virtualized_long_document_count_drift_after_api_pass():
    feishu = FakeVisualFeishu("<title>T</title>")
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[
                    VisualFinding(kind="missing-image", severity="high", data={"actual": 20, "expected": 23}),
                    VisualFinding(kind="missing-table", severity="high", data={"actual": 12, "expected": 18}),
                    VisualFinding(kind="missing-formula", severity="high", data={"actual": 219, "expected": 274}),
                ],
                raw={
                    "sections_checked": 12,
                    "metrics": {"scroll_height": 26509, "scroll_client_height": 936},
                },
            )
        ]
    )
    controller.max_sections = 12

    result = controller.run(
        feishu,
        "doc",
        initial_warnings=[],
        expected_formula_min=274,
        expected_table_min=18,
    )

    assert result.passed is True
    assert result.rounds[0].status == "passed-with-warnings"
    assert result.warnings == [
        "visual-qa:medium:missing-image-sampling-drift:长文抽样 DOM 仅加载 20/23；API 全文结构计数已通过",
        "visual-qa:medium:missing-table-sampling-drift:长文抽样 DOM 仅加载 12/18；API 全文结构计数已通过",
        "visual-qa:medium:missing-formula-sampling-drift:长文抽样 DOM 仅加载 219/274；API 全文结构计数已通过",
    ]


def test_controller_keeps_count_drift_blocking_when_api_count_also_failed():
    feishu = FakeVisualFeishu("<title>T</title>")
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="missing-formula", severity="high", data={"actual": 10, "expected": 20})],
                raw={
                    "sections_checked": 12,
                    "metrics": {"scroll_height": 20000, "scroll_client_height": 900},
                },
            )
        ]
    )
    controller.max_sections = 12

    result = controller.run(
        feishu,
        "doc",
        initial_warnings=["post-publish:missing-latex:10/20"],
        expected_formula_min=20,
    )

    assert result.passed is False
    assert any(warning.startswith("visual-qa:high:missing-formula") for warning in result.warnings)


def test_controller_downgrades_medium_document_formula_count_drift_when_render_has_no_errors():
    feishu = FakeVisualFeishu("<title>T</title>")
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="missing-formula", severity="high", data={"actual": 57, "expected": 64})],
                raw={
                    "sections_checked": 16,
                    "metrics": {
                        "scroll_height": 10779,
                        "scroll_client_height": 936,
                        "invalid_formula_count": 0,
                    },
                },
            )
        ]
    )

    result = controller.run(feishu, "doc", initial_warnings=[], expected_formula_min=64)

    assert result.passed is True
    assert result.warnings == [
        "visual-qa:medium:missing-formula-sampling-drift:长文抽样 DOM 仅加载 57/64；API 全文结构计数已通过"
    ]


def test_controller_keeps_zero_render_formula_count_blocking():
    feishu = FakeVisualFeishu("<title>T</title>")
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="missing-formula", severity="high", data={"actual": 0, "expected": 10})],
                raw={"metrics": {"invalid_formula_count": 0}},
            )
        ]
    )

    result = controller.run(feishu, "doc", initial_warnings=[], expected_formula_min=10)

    assert result.passed is False


def test_controller_keeps_missing_image_blocking_when_publish_marker_remains():
    feishu = FakeVisualFeishu("<title>T</title>")
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="missing-image", severity="high", data={"actual": 20, "expected": 23})],
            )
        ]
    )

    result = controller.run(
        feishu,
        "doc",
        initial_warnings=["post-publish:marker-left-after-publish"],
        expected_image_min=23,
    )

    assert result.passed is False
    assert any(warning.startswith("visual-qa:high:missing-image") for warning in result.warnings)


def test_controller_keeps_zero_render_image_count_blocking():
    feishu = FakeVisualFeishu("<title>T</title>")
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="missing-image", severity="high", data={"actual": 0, "expected": 3})],
            )
        ]
    )

    result = controller.run(feishu, "doc", initial_warnings=[], expected_image_min=3)

    assert result.passed is False


def test_controller_retries_visual_repair_for_three_rounds_and_uses_final_pass():
    feishu = FakeVisualFeishu('<title>T</title><p id="caption">\\textbfThe pipeline</p>')
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="raw-formatting", severity="high", autofixable=True)],
            ),
            RemoteVisualResult(status="issues", findings=[VisualFinding(kind="invalid-formula", severity="high")]),
            RemoteVisualResult(status="issues", findings=[VisualFinding(kind="invalid-formula", severity="high")]),
            RemoteVisualResult(status="ok"),
        ]
    )
    controller.repair_rounds = 3

    result = controller.run(feishu, "doc", source_id="paper")

    assert result.passed is True
    assert len(result.rounds) == 4
    assert controller.calls == ["paper", "paper-visual-r1", "paper-visual-r2", "paper-visual-r3"]
    assert result.rounds[-1].status == "passed"
    assert result.rounds[0].changed is True
    assert not any(warning.startswith("visual-qa:high:") for warning in result.warnings)


def test_controller_repairs_structural_and_image_findings_in_one_round():
    feishu = FakeVisualFeishu(
        '<title>T</title><p id="caption">\\textbfThe pipeline</p>'
        '<img id="image-block" name="figure.png" width="900" height="450"/>'
    )
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[
                    VisualFinding(kind="raw-formatting", severity="high", autofixable=True),
                    VisualFinding(
                        kind="image-overflow",
                        severity="high",
                        image_name="figure.png",
                        block_id="image-block",
                        autofixable=True,
                        data={"editor_width": 820},
                    ),
                ],
            ),
            RemoteVisualResult(status="ok"),
        ]
    )

    result = controller.run(feishu, "doc", source_id="paper")

    assert result.passed is True
    assert result.rounds[0].changed is True
    assert result.rounds[0].repair_strategy == "deterministic-structural+deterministic-image"
    assert {block_id for _url, block_id, _content in feishu.replacements} == {"caption", "image-block"}


def test_controller_uses_model_after_deterministic_formula_repair_makes_no_change():
    feishu = FakeVisualFeishu('<title>T</title><p id="formula"><latex>\\unsupportedmacro{x}</latex></p>')
    llm = FormulaRepairLLM()
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="invalid-formula", severity="high", autofixable=True)],
            ),
            RemoteVisualResult(status="ok"),
        ]
    )
    controller.llm = llm

    result = controller.run(
        feishu,
        "doc",
        source_id="paper",
        previous_feedback=["visual round 0: invalid-formula: unsupported macro remained"],
    )

    assert result.passed is True
    assert llm.calls == 1
    assert result.rounds[0].repair_strategy == "model-formula"
    assert result.rounds[0].model_used is True
    assert "unsupported macro remained" in llm.users[0]
    assert "不得重复同样的无效修改" in llm.users[0]
    assert feishu.replacements[0][1] == "formula"
    assert "<latex>a=1" in feishu.replacements[0][2]


def test_controller_retries_transient_visual_repair_model_failure():
    feishu = FakeVisualFeishu('<title>T</title><p id="formula"><latex>\\unsupportedmacro{x}</latex></p>')
    controller = StubVisualQA(
        [
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="invalid-formula", severity="high", autofixable=True)],
            ),
            RemoteVisualResult(
                status="issues",
                findings=[VisualFinding(kind="invalid-formula", severity="high", autofixable=True)],
            ),
            RemoteVisualResult(status="ok"),
        ]
    )
    controller.llm = RetryFormulaRepairLLM()

    result = controller.run(feishu, "doc", source_id="paper")

    assert result.passed is True
    assert controller.llm.calls == 2
    assert len(result.rounds) == 3
    assert result.rounds[0].status == "retryable-failure"
    assert result.rounds[1].changed is True


def test_last_json_object_ignores_ssh_banner():
    payload = _last_json_object('banner\n{"status":"ok","findings":[]}\n')

    assert payload == {"status": "ok", "findings": []}


def test_inspect_remote_retries_timeout_with_larger_budget(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "status": "ok",
                    "findings": [],
                    "screenshots": ["/tmp/page.png"],
                }
            ),
        )

    monkeypatch.setattr(visual_qa_module.subprocess, "run", fake_run)
    controller = VisualQAController(
        enabled=True,
        host="local",
        timeout=15,
        inspect_retries=2,
    )

    result = controller.inspect_remote("https://tenant.feishu.cn/docx/doc123", source_id="paper")

    assert result.status == "ok"
    assert calls == [15, 30]
    assert [item["status"] for item in result.raw["inspect_attempts"]] == ["error", "ok"]


def test_from_settings_uses_independent_visual_model_when_configured():
    settings = SimpleNamespace(
        visual_qa_enabled=True,
        visual_openai_api_key="visual-key",
        visual_openai_base_url="https://visual.example/v1",
        visual_openai_sub_module="visual",
        visual_openai_api_mode="responses",
        visual_model="vision-model",
        model="primary-model",
        openai_base_url="https://primary.example/v1",
        openai_sub_module="primary",
        openai_api_mode="responses",
        openai_timeout=42,
        openai_reasoning_effort="high",
    )

    controller = VisualQAController.from_settings(settings, llm=object())

    assert controller.llm.api_key == "visual-key"
    assert controller.llm.model == "vision-model"
    assert controller.llm.base_url == "https://visual.example/v1"
