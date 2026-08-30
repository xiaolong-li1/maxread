from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _int(values: dict[str, str], key: str, default: int, minimum: int = 0) -> int:
    value = int(values.get(key, os.environ.get(key, str(default))))
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _float(values: dict[str, str], key: str, default: float, minimum: float = 0.0) -> float:
    value = float(values.get(key, os.environ.get(key, str(default))))
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class PipelineSettings:
    root: Path
    collector_root: Path
    mailbox_env_file: Path
    mailbox_address: str
    data_dir: Path
    db_path: Path
    interval_days: float
    scan_limit: int
    model: str
    reasoning_effort: str
    api_key: str
    api_base_url: str
    api_mode: str
    api_timeout: int
    llm_concurrency: int
    pdf_workers: int
    retry_attempts: int
    retry_base_seconds: float
    run_stale_minutes: int
    feishu_as: str
    lark_cli: str
    node_bin: str
    base_token: str
    table_id: str
    docs_parent_token: str
    mark_interview_assigned: bool
    notify_enabled: bool
    notify_chat_id: str
    mailbox_env_files: tuple[Path, ...] = ()
    mailbox_addresses: tuple[str, ...] = ()
    team_addresses: tuple[str, ...] = ()

    @classmethod
    def load(cls, root: Path, mailbox_env_file: str | Path) -> "PipelineSettings":
        root = root.resolve()
        mailbox_env = Path(mailbox_env_file).expanduser()
        if not mailbox_env.is_absolute():
            mailbox_env = (root / mailbox_env).resolve()
        account = _read_env(mailbox_env)
        project = _read_env(root / ".env")
        merged = {**project, **account}

        collector_root = mailbox_env.parent.parent.parent
        data_dir = Path(merged.get("MAIL_DATA_DIR", "./data")).expanduser()
        if not data_dir.is_absolute():
            data_dir = (collector_root / data_dir).resolve()
        db_path = Path(merged.get("MAIL_DB_PATH", str(data_dir / "mail_collector.sqlite3"))).expanduser()
        if not db_path.is_absolute():
            db_path = (collector_root / db_path).resolve()

        def value(key: str, default: str = "") -> str:
            return merged.get(key, os.environ.get(key, default)).strip()

        def boolean(key: str, default: bool) -> bool:
            raw = value(key, "1" if default else "0").lower()
            return raw in {"1", "true", "yes", "on"}

        base_token = value("RECRUITING_BASE_TOKEN")
        table_id = value("RECRUITING_TABLE_ID")
        if not base_token or not table_id:
            raise ValueError("RECRUITING_BASE_TOKEN and RECRUITING_TABLE_ID are required")
        if value("MAIL_READ_ONLY", "1").lower() not in {"1", "true", "yes", "on"}:
            raise ValueError("MAIL_READ_ONLY must remain 1")
        configured_envs = [mailbox_env]
        for token in re.split(r"[,;\n]+", value("RECRUITING_MAIL_ACCOUNT_ENVS")):
            if not token.strip():
                continue
            candidate = Path(token.strip()).expanduser()
            if not candidate.is_absolute():
                candidate = (root / candidate).resolve()
            configured_envs.append(candidate)
        account_envs = tuple(dict.fromkeys(configured_envs))
        account_addresses = tuple(
            dict.fromkeys(
                values.get("IMAP_USERNAME", "").strip().casefold()
                for values in (_read_env(path) for path in account_envs)
                if values.get("IMAP_USERNAME", "").strip()
            )
        )
        team_addresses = tuple(
            dict.fromkeys(
                token.strip().casefold()
                for token in re.split(r"[,;\n]+", value("RECRUITING_TEAM_ADDRESSES"))
                if token.strip()
            )
        )

        return cls(
            root=root,
            collector_root=collector_root,
            mailbox_env_file=mailbox_env,
            mailbox_address=value("IMAP_USERNAME"),
            data_dir=data_dir,
            db_path=db_path,
            interval_days=_float(merged, "RECRUITING_SCAN_INTERVAL_DAYS", 1.0, 0.01),
            scan_limit=_int(merged, "MAIL_SCAN_LIMIT", 100, 1),
            model=value("RECRUITING_MODEL", "gpt-5.6-sol"),
            reasoning_effort=value("RECRUITING_REASONING_EFFORT", "medium"),
            api_key=value("RECRUITING_OPENAI_API_KEY", value("OPENAI_API_KEY")),
            api_base_url=value("RECRUITING_OPENAI_BASE_URL", value("OPENAI_BASE_URL", "https://api.openai.com/v1")),
            api_mode=value("RECRUITING_OPENAI_API_MODE", value("MAXREAD_OPENAI_API_MODE", "responses")),
            api_timeout=_int(merged, "RECRUITING_OPENAI_TIMEOUT", 180, 5),
            llm_concurrency=_int(merged, "RECRUITING_LLM_CONCURRENCY", 2, 1),
            pdf_workers=_int(merged, "RECRUITING_PDF_WORKERS", 4, 1),
            retry_attempts=_int(merged, "RECRUITING_RETRY_ATTEMPTS", 3, 1),
            retry_base_seconds=_float(merged, "RECRUITING_RETRY_BASE_SECONDS", 2.0, 0.1),
            run_stale_minutes=_int(merged, "RECRUITING_RUN_STALE_MINUTES", 10, 1),
            feishu_as=value("RECRUITING_FEISHU_AS", value("MAXREAD_FEISHU_AS", "bot")),
            lark_cli=value("RECRUITING_LARK_CLI", value("MAXREAD_LARK_CLI", "lark-cli")),
            node_bin=value("MAXREAD_NODE", str(Path.home() / ".local/node/bin/node")),
            base_token=base_token,
            table_id=table_id,
            docs_parent_token=value("RECRUITING_DOCS_PARENT_TOKEN"),
            mark_interview_assigned=boolean("RECRUITING_MARK_INTERVIEW_ASSIGNED", True),
            notify_enabled=boolean("RECRUITING_NOTIFY_ENABLED", False),
            notify_chat_id=value("RECRUITING_NOTIFY_CHAT_ID"),
            mailbox_env_files=account_envs,
            mailbox_addresses=account_addresses,
            team_addresses=team_addresses,
        )

    def command_env(self) -> dict[str, str]:
        env = dict(os.environ)
        node = Path(self.node_bin).expanduser()
        node_dir = node.parent if node.name == "node" else node
        env["PATH"] = f"{node_dir}:{node_dir.parent / 'bin'}:{env.get('PATH', '')}"
        return env
