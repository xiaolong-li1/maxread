#!/usr/bin/env python3
"""Send a local image to an OpenAI-compatible Responses API.

Examples:
    python examples/openai_responses_image_demo.py ./image.png "这张图有什么问题？"
    python examples/openai_responses_image_demo.py ./image.png --interactive
    python examples/openai_responses_image_demo.py ./image.png --dry-run

By default the script uses ~/.codex/config.toml and ~/.codex/auth.json, matching
the model provider used by the local Codex app. Pass --no-codex-config to use
OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_SUB_MODULE, and OPENAI_IMAGE_MODEL from
the repository's .env file or process environment instead.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_codex_defaults() -> dict:
    config_path = Path.home() / ".codex" / "config.toml"
    auth_path = Path.home() / ".codex" / "auth.json"
    if not config_path.is_file():
        return {}
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    provider_name = str(config.get("model_provider") or "").strip()
    provider = config.get("model_providers", {}).get(provider_name, {})
    defaults = {
        "model": str(config.get("model") or "").strip(),
        "base_url": str(provider.get("base_url") or "").strip(),
    }
    if auth_path.is_file():
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        defaults["api_key"] = str(auth.get("OPENAI_API_KEY") or "").strip()
    return defaults


def image_data_url(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"image not found: {path}")
    mime_type = mimetypes.guess_type(path.name)[0] or ""
    if not mime_type.startswith("image/"):
        raise ValueError(f"unsupported image type: {path.suffix or '(no extension)'}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def initial_payload(args: argparse.Namespace) -> dict:
    return {
        "model": args.model,
        "instructions": args.system,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": args.prompt},
                    {
                        "type": "input_image",
                        "image_url": image_data_url(args.image),
                        "detail": args.detail,
                    },
                ],
            }
        ],
        "reasoning": {"effort": args.reasoning_effort},
        "text": {"verbosity": args.verbosity},
    }


def follow_up_payload(args: argparse.Namespace, response_id: str, prompt: str) -> dict:
    return {
        "model": args.model,
        "previous_response_id": response_id,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "reasoning": {"effort": args.reasoning_effort},
        "text": {"verbosity": args.verbosity},
    }


def post_response(args: argparse.Namespace, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if args.sub_module:
        headers["X-Sub-Module"] = args.sub_module
    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Responses API returned HTTP {exc.code}: {body[:2000]}") from exc


def output_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    if not chunks:
        raise RuntimeError(f"response contains no output text: {json.dumps(response, ensure_ascii=False)[:2000]}")
    return "\n".join(chunks).strip()


def redacted_payload(payload: dict) -> dict:
    clone = json.loads(json.dumps(payload, ensure_ascii=False))
    for item in clone.get("input", []):
        for content in item.get("content", []):
            image_url = content.get("image_url")
            if isinstance(image_url, str) and image_url.startswith("data:"):
                prefix, encoded = image_url.split(",", 1)
                content["image_url"] = f"{prefix},<base64:{len(encoded)} chars>"
    return clone


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Local PNG/JPEG/WebP/GIF path")
    parser.add_argument("prompt", nargs="?", default="请详细描述这张图片，并指出明显异常。")
    parser.add_argument("--system", default="你是严谨的视觉分析助手。只描述图片中有依据的内容。")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--sub-module")
    parser.add_argument(
        "--codex-config",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use ~/.codex model provider and auth (default: enabled)",
    )
    parser.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high", "xhigh"), default="medium")
    parser.add_argument("--verbosity", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--detail", choices=("auto", "low", "high"), default="high")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--interactive", action="store_true", help="Continue with previous_response_id")
    parser.add_argument("--dry-run", action="store_true", help="Print a redacted request without calling the API")
    parser.add_argument("--save-response", type=Path, help="Save the latest raw response JSON")
    args = parser.parse_args()
    codex = load_codex_defaults() if args.codex_config else {}
    args.model = args.model or codex.get("model") or os.environ.get("OPENAI_IMAGE_MODEL", "gpt-5.6-sol")
    args.base_url = args.base_url or codex.get("base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    args.api_key = args.api_key or codex.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
    args.sub_module = args.sub_module if args.sub_module is not None else os.environ.get("OPENAI_SUB_MODULE", "")
    args.image = args.image.expanduser().resolve()
    if not args.dry_run and not args.api_key:
        parser.error("OPENAI_API_KEY is missing; set it in .env or the process environment")
    return args


def main() -> int:
    args = parse_args()
    payload = initial_payload(args)
    if args.dry_run:
        print(json.dumps(redacted_payload(payload), ensure_ascii=False, indent=2))
        return 0

    response = post_response(args, payload)
    print(f"assistant> {output_text(response)}")
    if args.save_response:
        args.save_response.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")

    while args.interactive:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not prompt or prompt.lower() in {"exit", "quit", "/exit", "/quit"}:
            break
        response_id = str(response.get("id") or "").strip()
        if not response_id:
            raise RuntimeError("gateway response has no id; previous_response_id dialogue is unavailable")
        response = post_response(args, follow_up_payload(args, response_id, prompt))
        print(f"assistant> {output_text(response)}")
        if args.save_response:
            args.save_response.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
