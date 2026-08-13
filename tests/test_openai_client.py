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
