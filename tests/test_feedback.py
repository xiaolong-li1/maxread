from maxread.feedback import classify_feedback_text, visible_feedback_rows


class _Classifier:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def responses_text(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.response


def test_explicit_feedback_uses_rule_without_ai_call():
    llm = _Classifier('{"is_feedback":false,"category":"other","confidence":1}')

    decision = classify_feedback_text(llm, "反馈：公式坏了")

    assert decision.is_feedback is True
    assert decision.source == "rule"
    assert llm.calls == []


def test_natural_feedback_uses_ai_classifier():
    llm = _Classifier('{"is_feedback":true,"category":"quality","confidence":0.96}')

    decision = classify_feedback_text(llm, "这篇方法框架图不见了，公式也很糟糕")

    assert decision.is_feedback is True
    assert decision.source == "ai"
    assert decision.category == "quality"
    assert decision.confidence == 0.96
    assert llm.calls[0][2]["reasoning_effort"] == "minimal"


def test_classifier_skips_plain_greeting_and_help_requests():
    llm = _Classifier('{"is_feedback":true,"category":"bug","confidence":1}')

    assert classify_feedback_text(llm, "hello").is_feedback is False
    assert classify_feedback_text(llm, "你是谁").is_feedback is False
    assert llm.calls == []


def test_classifier_handles_feedback_that_mentions_maxread():
    llm = _Classifier('{"is_feedback":true,"category":"bug","confidence":0.91}')

    decision = classify_feedback_text(llm, "MaxRead 怎么又没有图了")

    assert decision.is_feedback is True
    assert len(llm.calls) == 1


def test_classifier_fails_closed_on_low_confidence_or_invalid_output():
    low = _Classifier('{"is_feedback":true,"category":"bug","confidence":0.6}')
    invalid = _Classifier("yes")

    assert classify_feedback_text(low, "这个文档有点奇怪").is_feedback is False
    assert classify_feedback_text(invalid, "这个文档有点奇怪").is_feedback is False


def test_visible_feedback_rows_accepts_ai_rows_but_hides_historical_noise():
    rows = [
        {"content": "图片完全不见了", "feedback_source": "ai"},
        {"content": "反馈：公式坏了", "feedback_source": ""},
        {"content": "hello", "feedback_source": ""},
    ]

    assert visible_feedback_rows(rows) == rows[:2]
