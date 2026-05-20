from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_base_url: str
    openai_sub_module: str
    db_path: Path
    workdir: Path
    model: str
    feishu_as: str
    lark_cli: str
    arxiv_timeout: int
    openai_timeout: int
    require_source: bool
    batch_workers: int
    batch_llm_concurrency: int
    batch_feishu_concurrency: int
    batch_max_items: int
    feedback_url: str
    queue_workers: int
    queue_stale_minutes: int
    queue_heartbeat_seconds: int

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = root or Path.cwd()
        _load_dotenv(root / ".env")
        db_path = Path(os.environ.get("MAXREAD_DB", "./maxread.sqlite3")).expanduser()
        workdir = Path(os.environ.get("MAXREAD_WORKDIR", "./var/maxread")).expanduser()
        if not db_path.is_absolute():
            db_path = root / db_path
        if not workdir.is_absolute():
            workdir = root / workdir
        return cls(
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_sub_module=os.environ.get("OPENAI_SUB_MODULE", ""),
            db_path=db_path,
            workdir=workdir,
            model=os.environ.get("MAXREAD_MODEL", "gpt-4.1"),
            feishu_as=os.environ.get("MAXREAD_FEISHU_AS", "bot"),
            lark_cli=os.environ.get("MAXREAD_LARK_CLI", "lark-cli"),
            arxiv_timeout=int(os.environ.get("MAXREAD_ARXIV_TIMEOUT", "45")),
            openai_timeout=int(os.environ.get("MAXREAD_OPENAI_TIMEOUT", "180")),
            require_source=os.environ.get("MAXREAD_REQUIRE_SOURCE", "true").lower() in {"1", "true", "yes", "on"},
            batch_workers=int(os.environ.get("MAXREAD_BATCH_WORKERS", "3")),
            batch_llm_concurrency=int(os.environ.get("MAXREAD_LLM_CONCURRENCY", "2")),
            batch_feishu_concurrency=int(os.environ.get("MAXREAD_FEISHU_CONCURRENCY", "1")),
            batch_max_items=int(os.environ.get("MAXREAD_BATCH_MAX_ITEMS", "6")),
            feedback_url=os.environ.get("MAXREAD_FEEDBACK_URL", ""),
            queue_workers=int(os.environ.get("MAXREAD_QUEUE_WORKERS", os.environ.get("MAXREAD_BATCH_WORKERS", "3"))),
            queue_stale_minutes=int(os.environ.get("MAXREAD_QUEUE_STALE_MINUTES", "30")),
            queue_heartbeat_seconds=int(os.environ.get("MAXREAD_QUEUE_HEARTBEAT_SECONDS", "15")),
        )
