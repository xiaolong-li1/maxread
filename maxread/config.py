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
    openai_api_mode: str
    visual_openai_api_key: str
    visual_openai_base_url: str
    visual_openai_sub_module: str
    visual_openai_api_mode: str
    visual_model: str
    db_path: Path
    workdir: Path
    model: str
    feishu_as: str
    lark_cli: str
    arxiv_timeout: int
    arxiv_parallel_streams: int
    arxiv_parallel_min_bytes: int
    openai_timeout: int
    openai_reasoning_effort: str
    openai_review_reasoning_effort: str
    generation_repair_rounds: int
    quality_repair_rounds: int
    require_source: bool
    batch_workers: int
    batch_llm_concurrency: int
    batch_feishu_concurrency: int
    batch_max_items: int
    feedback_url: str
    queue_workers: int
    queue_stale_minutes: int
    queue_heartbeat_seconds: int
    visual_qa_enabled: bool
    visual_qa_host: str
    visual_qa_runner: str
    visual_qa_remote_root: str
    visual_qa_timeout: int
    visual_qa_inspect_retries: int
    visual_qa_max_sections: int
    visual_qa_max_repairs: int
    visual_qa_repair_rounds: int
    duty_timezone: str
    duty_chat_id: str
    duty_hour: int
    duty_minute: int
    duty_poll_seconds: int

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
            openai_api_mode=os.environ.get("MAXREAD_OPENAI_API_MODE", "responses"),
            visual_openai_api_key=os.environ.get("MAXREAD_VISUAL_OPENAI_API_KEY", ""),
            visual_openai_base_url=os.environ.get("MAXREAD_VISUAL_OPENAI_BASE_URL", ""),
            visual_openai_sub_module=os.environ.get("MAXREAD_VISUAL_OPENAI_SUB_MODULE", ""),
            visual_openai_api_mode=os.environ.get("MAXREAD_VISUAL_OPENAI_API_MODE", ""),
            visual_model=os.environ.get("MAXREAD_VISUAL_MODEL", ""),
            db_path=db_path,
            workdir=workdir,
            model=os.environ.get("MAXREAD_MODEL", "gpt-4.1"),
            feishu_as=os.environ.get("MAXREAD_FEISHU_AS", "bot"),
            lark_cli=os.environ.get("MAXREAD_LARK_CLI", "lark-cli"),
            arxiv_timeout=int(os.environ.get("MAXREAD_ARXIV_TIMEOUT", "45")),
            arxiv_parallel_streams=int(os.environ.get("MAXREAD_ARXIV_PARALLEL_STREAMS", "4")),
            arxiv_parallel_min_bytes=int(os.environ.get("MAXREAD_ARXIV_PARALLEL_MIN_BYTES", "1048576")),
            openai_timeout=int(os.environ.get("MAXREAD_OPENAI_TIMEOUT", "180")),
            openai_reasoning_effort=os.environ.get("MAXREAD_OPENAI_REASONING_EFFORT", "high"),
            openai_review_reasoning_effort=os.environ.get("MAXREAD_OPENAI_REVIEW_REASONING_EFFORT", "low"),
            generation_repair_rounds=max(0, int(os.environ.get("MAXREAD_GENERATION_REPAIR_ROUNDS", "2"))),
            quality_repair_rounds=max(0, int(os.environ.get("MAXREAD_QUALITY_REPAIR_ROUNDS", "3"))),
            require_source=os.environ.get("MAXREAD_REQUIRE_SOURCE", "true").lower() in {"1", "true", "yes", "on"},
            batch_workers=int(os.environ.get("MAXREAD_BATCH_WORKERS", "3")),
            batch_llm_concurrency=int(os.environ.get("MAXREAD_LLM_CONCURRENCY", "2")),
            batch_feishu_concurrency=int(os.environ.get("MAXREAD_FEISHU_CONCURRENCY", "1")),
            batch_max_items=int(os.environ.get("MAXREAD_BATCH_MAX_ITEMS", "6")),
            feedback_url=os.environ.get("MAXREAD_FEEDBACK_URL", ""),
            queue_workers=int(os.environ.get("MAXREAD_QUEUE_WORKERS", os.environ.get("MAXREAD_BATCH_WORKERS", "3"))),
            queue_stale_minutes=int(os.environ.get("MAXREAD_QUEUE_STALE_MINUTES", "30")),
            queue_heartbeat_seconds=int(os.environ.get("MAXREAD_QUEUE_HEARTBEAT_SECONDS", "15")),
            visual_qa_enabled=os.environ.get("MAXREAD_VISUAL_QA_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            visual_qa_host=os.environ.get("MAXREAD_VISUAL_QA_HOST", "ziplab-5090"),
            visual_qa_runner=os.environ.get(
                "MAXREAD_VISUAL_QA_RUNNER",
                "/home/lixiaolong/.local/share/maxread-browser/run_visual_qa.sh",
            ),
            visual_qa_remote_root=os.environ.get(
                "MAXREAD_VISUAL_QA_REMOTE_ROOT",
                "/home/lixiaolong/.local/share/maxread-browser",
            ),
            visual_qa_timeout=int(os.environ.get("MAXREAD_VISUAL_QA_TIMEOUT", "90")),
            visual_qa_inspect_retries=max(0, int(os.environ.get("MAXREAD_VISUAL_QA_INSPECT_RETRIES", "2"))),
            visual_qa_max_sections=int(os.environ.get("MAXREAD_VISUAL_QA_MAX_SECTIONS", "12")),
            visual_qa_max_repairs=int(os.environ.get("MAXREAD_VISUAL_QA_MAX_REPAIRS", "2")),
            visual_qa_repair_rounds=max(0, int(os.environ.get("MAXREAD_VISUAL_QA_REPAIR_ROUNDS", "3"))),
            duty_timezone=os.environ.get("MAXREAD_DUTY_TIMEZONE", "Asia/Shanghai"),
            duty_chat_id=os.environ.get("MAXREAD_DUTY_CHAT_ID", "").strip(),
            duty_hour=int(os.environ.get("MAXREAD_DUTY_HOUR", "7")),
            duty_minute=int(os.environ.get("MAXREAD_DUTY_MINUTE", "0")),
            duty_poll_seconds=int(os.environ.get("MAXREAD_DUTY_POLL_SECONDS", "30")),
        )
