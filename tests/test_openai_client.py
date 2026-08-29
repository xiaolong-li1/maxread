import json
from pathlib import Path

import pytest

from maxread.openai_client import OpenAIClient
from maxread import openai_client


class _StreamResponse:
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, events):
        self._lines = [("data: " + json.dumps(event) + "\n").encode() for event in events]

    def __enter__(self):
        return iter(self._lines)

    def __exit__(self, *_args):
        return False


def test_stream_rejects_partial_text_without_completed_event():
    original = openai_client.urllib.request.urlopen
    openai_client.urllib.request.urlopen = lambda *_args, **_kwargs: _StreamResponse([
        {"type": "response.output_text.delta", "delta": "partial"},
    ])
    try:
        client = OpenAIClient("key", "model")
        try:
            client._post_stream_text("/responses", {})
        except RuntimeError as exc:
            assert "before response.completed" in str(exc)
        else:
            raise AssertionError("partial stream was accepted")
    finally:
        openai_client.urllib.request.urlopen = original


def test_chat_mode_routes_responses_text_to_chat_completions():
    client = OpenAIClient("key", "model", api_mode="chat", reasoning_effort="high")
    calls = []

    def fake_chat(system, user, reasoning_effort=None):
        calls.append((system, user, reasoning_effort))
        return "chat result"

    client.chat_completions_text = fake_chat
    assert client.responses_text("system", "user") == "chat result"
    assert calls == [("system", "user", None)]


def test_chat_completions_includes_reasoning_effort():
    client = OpenAIClient("key", "model", reasoning_effort="high")
    captured = {}
    client._post = lambda path, payload: captured.update(path=path, payload=payload) or {
        "choices": [{"message": {"content": "ok"}}]
    }
    assert client.chat_completions_text("system", "user") == "ok"
    assert captured["path"] == "/chat/completions"
    assert captured["payload"]["reasoning_effort"] == "high"


def test_responses_text_uses_separate_instructions_and_input():
    client = OpenAIClient("key", "model", reasoning_effort="high")
    captured = {}

    def fake_stream(path, payload):
        captured.update(path=path, payload=payload)
        return "ok"

    client._post_stream_text = fake_stream
    assert client.responses_text("system", "user") == "ok"
    assert captured["path"] == "/responses"
    assert captured["payload"]["instructions"] == "system"
    assert captured["payload"]["input"] == "user"


def test_responses_text_sanitizes_surrogates_before_json_transport():
    client = OpenAIClient("key", "model")
    captured = {}

    def fake_stream(_path, payload):
        captured.update(payload)
        return "ok"

    client._post_stream_text = fake_stream

    assert client.responses_text("sys\ud835", "prompt\ud8350") == "ok"
    assert captured["instructions"] == "sys\uFFFD"
    assert captured["input"] == "prompt\uFFFD0"
    assert json.dumps(captured, ensure_ascii=False).encode("utf-8")


def test_responses_image_text_can_override_reasoning_effort(tmp_path):
    image = tmp_path / "figure.png"
    image.write_bytes(b"image")
    client = OpenAIClient("key", "model", reasoning_effort="high")
    captured = {}
    client._post = lambda path, payload: captured.update(path=path, payload=payload) or {"output_text": "ok"}

    assert client.responses_image_text("system", "user", image, reasoning_effort="low") == "ok"
    assert captured["payload"]["reasoning"] == {"effort": "low"}


def test_stream_retries_transient_url_error(monkeypatch):
    client = OpenAIClient("key", "model")
    calls = []

    def flaky(_path, _payload):
        calls.append(1)
        if len(calls) < 3:
            raise openai_client.urllib.error.URLError("EOF occurred in violation of protocol")
        return "ok"

    client._post_stream_text_once = flaky
    monkeypatch.setattr(openai_client.time, "sleep", lambda _seconds: None)

    assert client._post_stream_text("/responses", {}) == "ok"
    assert len(calls) == 3


def test_stream_retries_transient_http_502(monkeypatch):
    client = OpenAIClient("key", "model")
    calls = []

    def flaky(path, _payload):
        calls.append(1)
        if len(calls) == 1:
            raise openai_client.OpenAIRequestError(path, 502, "Bad Gateway")
        return "ok"

    client._post_stream_text_once = flaky
    monkeypatch.setattr(openai_client.time, "sleep", lambda _seconds: None)

    assert client._post_stream_text("/responses", {}) == "ok"
    assert len(calls) == 2


def test_stream_total_timeout_is_not_extended_by_heartbeats(monkeypatch):
    client = OpenAIClient("key", "model", timeout=1)
    response = _StreamResponse([
        {"type": "response.output_text.delta", "delta": "first"},
        {"type": "response.output_text.delta", "delta": "second"},
    ])
    ticks = iter([0.0, 0.5, 1.1])
    monkeypatch.setattr(openai_client.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(openai_client.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(TimeoutError, match="total timeout"):
        client._post_stream_text("/responses", {})


def test_stream_timeout_is_not_retried(monkeypatch):
    client = OpenAIClient("key", "model", timeout=1)
    calls = []

    def timed_out(_path, _payload):
        calls.append(1)
        raise TimeoutError("deadline")

    client._post_stream_text_once = timed_out
    monkeypatch.setattr(openai_client.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="deadline"):
        client._post_stream_text("/responses", {})
    assert len(calls) == 1
