from maxread.text_safety import sanitize_unicode_text, sanitize_unicode_value


def test_sanitize_unicode_text_replaces_isolated_surrogates():
    text, count = sanitize_unicode_text("before\ud8350 after\udc00")

    assert text == "before\uFFFD0 after\uFFFD"
    assert count == 2
    assert text.encode("utf-8")


def test_sanitize_unicode_text_combines_valid_surrogate_pair():
    text, count = sanitize_unicode_text("x\ud835\udc00y")

    assert text == "x\U0001D400y"
    assert count == 0
    assert text.encode("utf-8")


def test_sanitize_unicode_value_recurses_through_json_payload():
    payload, count = sanitize_unicode_value({"input": ["ok", "bad\ud8350"]})

    assert payload == {"input": ["ok", "bad\uFFFD0"]}
    assert count == 1
