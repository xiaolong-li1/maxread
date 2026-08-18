from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict


class OpenAIRequestError(RuntimeError):
    def __init__(self, path: str, status: int, body: str):
        super().__init__(f"{path} failed with HTTP {status}: {body}")
        self.path = path
        self.status = status
        self.body = body


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: int = 180,
        base_url: str = "https://api.openai.com/v1",
        sub_module: str = "",
        reasoning_effort: str = "",
        api_mode: str = "responses",
    ):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for real summaries")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.sub_module = sub_module
        self.reasoning_effort = _normalize_reasoning_effort(reasoning_effort)
        self.api_mode = _normalize_api_mode(api_mode)

    def responses_text(self, system: str, user: str, reasoning_effort: str | None = None) -> str:
        if self.api_mode == "chat":
            return self.chat_completions_text(system, user, reasoning_effort=reasoning_effort)
        base_payload = {
            "model": self.model,
            "instructions": system.strip(),
            "input": user.strip(),
            "text": {"verbosity": "medium"},
            "stream": True,
        }
        effort = self.reasoning_effort if reasoning_effort is None else _normalize_reasoning_effort(reasoning_effort)
        data = None
        streamed_text = ""
        for current_effort in _reasoning_attempts(effort):
            payload = dict(base_payload)
            if current_effort:
                payload["reasoning"] = {"effort": current_effort}
            try:
                streamed_text = self._post_stream_text("/responses", payload)
                break
            except OpenAIRequestError as exc:
                if exc.status in {400, 404}:
                    return self.chat_completions_text(system, user, reasoning_effort=current_effort)
                if exc.status == 524 and current_effort != _reasoning_attempts(effort)[-1]:
                    continue
                raise
        if streamed_text:
            return streamed_text
        if data is None:
            raise RuntimeError("/responses failed")
        text = _extract_output_text(data)
        if not text:
            raise RuntimeError(f"OpenAI response had no output text: {data}")
        return text

    def responses_image_text(self, system: str, user: str, image_path: str | Path) -> str:
        image_url = _image_data_url(Path(image_path))
        payload = {
            "model": self.model,
            "instructions": system.strip(),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user.strip()},
                        {"type": "input_image", "image_url": image_url, "detail": "high"},
                    ],
                },
            ],
            "reasoning": {"effort": self.reasoning_effort} if self.reasoning_effort else {},
        }
        data = self._post("/responses", payload)
        text = _extract_output_text(data)
        if not text:
            raise RuntimeError(f"OpenAI image response had no output text: {data}")
        return text

    def chat_completions_text(
        self,
        system: str,
        user: str,
        reasoning_effort: str | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        effort = self.reasoning_effort if reasoning_effort is None else _normalize_reasoning_effort(reasoning_effort)
        if effort:
            payload["reasoning_effort"] = effort
        data = self._post("/chat/completions", payload)
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Chat completions response had no message content: {data}") from exc

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.7.1",
        }
        if self.sub_module:
            headers["X-Sub-Module"] = self.sub_module
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        last_error = None
        for attempt in range(1, 5):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:1000]
                last_error = OpenAIRequestError(path, exc.code, body)
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= 4:
                    raise last_error from exc
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else min(8 * attempt, 24)
                time.sleep(delay)
        if last_error:
            raise last_error
        raise RuntimeError(f"{path} failed")

    def _post_stream_text(self, path: str, payload: Dict[str, Any]) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "curl/8.7.1",
        }
        if self.sub_module:
            headers["X-Sub-Module"] = self.sub_module
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                chunks: list[str] = []
                completed = None
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_line = line[5:].strip()
                    if not data_line or data_line == "[DONE]":
                        continue
                    try:
                        event = json.loads(data_line)
                    except json.JSONDecodeError:
                        continue
                    event_type = str(event.get("type") or "")
                    if isinstance(event.get("delta"), str):
                        chunks.append(event["delta"])
                    elif isinstance(event.get("text"), str) and event_type.endswith(".delta"):
                        chunks.append(event["text"])
                    if event_type == "response.completed":
                        completed = event.get("response")
                    elif event_type in {"response.failed", "response.incomplete"}:
                        raise RuntimeError(f"{path} stream failed: {event}")
                if not isinstance(completed, dict):
                    raise RuntimeError(f"{path} stream ended before response.completed")
                text = "".join(chunks).strip()
                if text:
                    return text
                if isinstance(completed, dict):
                    text = _extract_output_text(completed)
                    if text:
                        return text
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise OpenAIRequestError(path, exc.code, body) from exc
        raise RuntimeError(f"{path} stream had no output text")


def _extract_output_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def _normalize_reasoning_effort(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "extra_high": "xhigh",
        "extrahigh": "xhigh",
        "very_high": "xhigh",
        "mid": "medium",
        "middle": "medium",
    }
    return aliases.get(text, text)


def _normalize_api_mode(value: str) -> str:
    text = str(value or "responses").strip().lower().replace("-", "_")
    return "chat" if text in {"chat", "chat_completions", "chatcompletion"} else "responses"


def _reasoning_attempts(effort: str) -> list[str]:
    order = ["minimal", "low", "medium", "high", "xhigh"]
    effort = _normalize_reasoning_effort(effort)
    if not effort or effort not in order:
        return [effort] if effort else [""]
    index = order.index(effort)
    attempts = [effort]
    for fallback in reversed(order[:index]):
        if fallback not in attempts:
            attempts.append(fallback)
    return attempts


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"
