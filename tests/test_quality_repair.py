import json

from maxread.quality import paper_markdown_completeness_errors
from maxread.quality_repair import repair_until_quality_passes


class RepairLLM:
    def __init__(self, repaired_markdown: str):
        self.repaired_markdown = repaired_markdown
        self.calls = 0
        self.users = []

    def responses_text(self, system, user, **kwargs):
        self.calls += 1
        self.users.append(user)
        assert "本轮确定性质检错误" in user
        return json.dumps(
            {"markdown": self.repaired_markdown, "issues": []},
            ensure_ascii=False,
        )


class NoChangeLLM:
    def __init__(self, markdown: str):
        self.markdown = markdown
        self.calls = 0

    def responses_text(self, system, user, **kwargs):
        self.calls += 1
        return json.dumps({"markdown": self.markdown, "issues": []}, ensure_ascii=False)


def test_quality_repair_loops_until_deterministic_checks_pass():
    bad = "# T\n\n正文里残留 \\textbf{bad}。\n"
    fixed = "# T\n\n正文已修复。\n"
    llm = RepairLLM(fixed)

    result = repair_until_quality_passes(
        llm,
        bad,
        [],
        render_xml=lambda markdown: f"<doc><p>{markdown}</p></doc>",
        normalize_markdown=lambda markdown: markdown.strip() + "\n",
        max_repair_rounds=3,
    )

    assert result.passed is True
    assert result.markdown == fixed
    assert len(result.attempts) == 2
    assert result.attempts[0].blocking_warnings == [
        "quality:format:markdown:high:raw-tex-formatting-command",
        "quality:format:xml:high:raw-tex-formatting-command",
    ]
    assert result.attempts[0].changed is True
    assert llm.calls == 1


def test_quality_repair_emits_state_machine_loop_events():
    bad = "# T\n\n正文里残留 \\textbf{bad}。\n"
    llm = RepairLLM("# T\n\n正文已修复。\n")
    events = []

    result = repair_until_quality_passes(
        llm,
        bad,
        [],
        render_xml=lambda markdown: f"<doc><p>{markdown}</p></doc>",
        normalize_markdown=lambda markdown: markdown.strip() + "\n",
        max_repair_rounds=1,
        on_workflow_event=lambda event, detail: events.append((event.value, detail)),
    )

    assert result.passed is True
    assert [event for event, _detail in events] == ["quality_repair_required", "quality_recheck"]


def test_quality_repair_stops_after_configured_rounds_when_model_makes_no_change():
    bad = "# T\n\n正文里残留 \\textbf{bad}。\n"
    llm = NoChangeLLM(bad)

    result = repair_until_quality_passes(
        llm,
        bad,
        [],
        render_xml=lambda markdown: f"<doc><p>{markdown}</p></doc>",
        normalize_markdown=lambda markdown: markdown.strip() + "\n",
        max_repair_rounds=2,
    )

    assert result.passed is False
    assert len(result.attempts) == 1
    assert llm.calls == 1
    assert result.repair_warnings == ["quality-repair:round-1:no-change"]


def test_quality_repair_can_fix_structural_completeness_errors():
    bad = "前置说明\n\n" + ("正文。" * 700)
    fixed = "# T\n\n**TL;DR**：摘要。\n\n" + "\n\n".join(
        f"## {number}. Section\n\n" + ("正文。" * 80)
        for number in range(1, 8)
    )
    llm = RepairLLM(fixed)

    result = repair_until_quality_passes(
        llm,
        bad,
        [],
        render_xml=lambda markdown: f"<doc>{markdown}</doc>",
        normalize_markdown=lambda markdown: markdown.strip() + "\n",
        completeness_check=paper_markdown_completeness_errors,
    )

    assert result.passed is True
    assert result.markdown.startswith("# T\n")
    assert any("missing-h1" in warning for warning in result.attempts[0].blocking_warnings)


def test_quality_repair_prompt_includes_previous_failure_ledger():
    bad = "# T\n\n正文里残留 \\textbf{bad}。\n"
    llm = RepairLLM("# T\n\n正文已修复。\n")

    result = repair_until_quality_passes(
        llm,
        bad,
        [],
        render_xml=lambda markdown: f"<doc><p>{markdown}</p></doc>",
        normalize_markdown=lambda markdown: markdown.strip() + "\n",
        prior_feedback=["attempt 2: missing-section-7", "visual round 1: invalid-formula"],
    )

    assert result.passed is True
    assert "missing-section-7" in llm.users[0]
    assert "visual round 1: invalid-formula" in llm.users[0]
    assert "不可回退清单" in llm.users[0]
