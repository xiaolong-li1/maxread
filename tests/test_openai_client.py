import json

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
