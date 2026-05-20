from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: int = 180,
        base_url: str = "https://api.openai.com/v1",
        sub_module: str = "",
    ):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for real summaries")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.sub_module = sub_module

    def responses_text(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user}]},
            ],
        }
        try:
            data = self._post("/responses", payload)
        except urllib.error.HTTPError as exc:
            if exc.code not in {400, 404}:
                raise
            return self.chat_completions_text(system, user)
        text = _extract_output_text(data)
        if not text:
            raise RuntimeError(f"OpenAI response had no output text: {data}")
        return text

    def chat_completions_text(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = self._post("/chat/completions", payload)
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Chat completions response had no message content: {data}") from exc

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
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
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"{path} failed with HTTP {exc.code}: {body}") from exc


def _extract_output_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()
