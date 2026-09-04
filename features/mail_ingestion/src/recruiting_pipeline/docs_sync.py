from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import PipelineSettings
from .retry import is_transient_error, retry_call


class DocsSync:
    def __init__(self, settings: PipelineSettings):
        self.settings = settings

    def create(self, title: str, content: str) -> tuple[str, str]:
        args = [self.settings.lark_cli, "docs", "+create", "--title", title]
        if self.settings.docs_parent_token:
            args.extend(["--parent-token", self.settings.docs_parent_token])
        args.extend(["--doc-format", "markdown", "--content", content, "--as", self.settings.feishu_as])
        result = self._call(args)
        document = result.get("data", {}).get("document", {})
        document_id = str(document.get("document_id") or "")
        url = str(document.get("url") or "")
        if not document_id or not url:
            raise RuntimeError(f"document create returned no URL: {result}")
        return document_id, url

    def update_title(self, document_id: str, title: str) -> None:
        self._call([
            self.settings.lark_cli,
            "drive",
            "+update-title",
            "--url",
            f"https://ccnsbbr30xgq.feishu.cn/docx/{document_id}",
            "--title",
            title,
            "--as",
            self.settings.feishu_as,
            "--format",
            "json",
        ])

    def append(self, document_id: str, content: str) -> None:
        self._call([
            self.settings.lark_cli,
            "docs",
            "+update",
            "--doc",
            document_id,
            "--command",
            "append",
            "--doc-format",
            "markdown",
            "--content",
            content,
            "--as",
            self.settings.feishu_as,
        ])

    def materialized_markers(self, document_id: str, markers: set[str]) -> set[str]:
        """Return deterministic message headings already present in a document."""
        if not markers:
            return set()
        fetched = self._call([
            self.settings.lark_cli,
            "docs",
            "+fetch",
            "--doc",
            document_id,
            "--doc-format",
            "markdown",
            "--detail",
            "simple",
            "--scope",
            "full",
            "--as",
            self.settings.feishu_as,
        ])
        content = str(fetched.get("data", {}).get("document", {}).get("content") or "")
        headings = {
            line[4:].strip()
            for line in content.splitlines()
            if line.startswith("### ")
        }
        return markers & headings

    def replace_summary(self, document_id: str, fields: Any, latest_time: str | None) -> bool:
        """Refresh simple summary lines without touching media blocks.

        ``block_replace`` can cause tokenized file resources to be replayed by
        some Drive backends.  Plain text replacement keeps PDF cards intact.
        The purpose paragraph is written at document creation time and is not
        rewritten here; new evidence is added through the follow-up delta.
        """
        fetched = self._call([
            self.settings.lark_cli,
            "docs",
            "+fetch",
            "--doc",
            document_id,
            "--doc-format",
            "markdown",
            "--detail",
            "simple",
            "--scope",
            "full",
            "--as",
            self.settings.feishu_as,
        ])
        content = str(fetched.get("data", {}).get("document", {}).get("content") or "")
        values = {
            "姓名": fields.name,
            "邮件类型": "其他" if fields.mail_type == "other" else "候选人来信",
            "院校 / 就读信息": fields.school_study_display,
            "院校": fields.school,
            "专业信息": fields.major,
            "申请项目": "—" if fields.mail_type == "other" else "、".join(fields.projects),
            "学业表现": fields.academic_display,
            "排名": fields.rank,
            "排名依据": fields.rank_evidence,
            "院校标签": f"985={fields.is_985}；C9={fields.is_c9}",
            "来源邮箱": "、".join(fields.source_accounts),
            "最新邮件时间": latest_time or "unknown",
        }
        changed = False
        for label, value in values.items():
            pattern = re.compile(rf"(?m)^- {re.escape(label)}：.*$")
            match = pattern.search(content)
            if not match:
                continue
            replacement = f"- {label}：{value}"
            if match.group(0) == replacement:
                continue
            self._call([
                self.settings.lark_cli,
                "docs",
                "+update",
                "--doc",
                document_id,
                "--command",
                "str_replace",
                "--pattern",
                match.group(0),
                "--content",
                replacement,
                "--doc-format",
                "markdown",
                "--as",
                self.settings.feishu_as,
            ])
            content = content[: match.start()] + replacement + content[match.end() :]
            changed = True
        return changed

    def replace_attachment_summary(self, document_id: str, lines: list[str]) -> bool:
        """Refresh the human-readable attachment list without touching file cards."""
        fetched = self._call([
            self.settings.lark_cli,
            "docs",
            "+fetch",
            "--doc",
            document_id,
            "--doc-format",
            "markdown",
            "--detail",
            "simple",
            "--scope",
            "full",
            "--as",
            self.settings.feishu_as,
        ])
        content = str(fetched.get("data", {}).get("document", {}).get("content") or "")
        match = re.search(r"(?ms)^## 附件\n\n(.*?)(?=\n\n(?:## |>|$))", content)
        if not match:
            return False
        previous = match.group(1).strip()
        replacement = "\n".join(lines).strip()
        if not replacement or replacement == previous:
            return False
        self._call([
            self.settings.lark_cli,
            "docs",
            "+update",
            "--doc",
            document_id,
            "--command",
            "str_replace",
            "--pattern",
            previous,
            "--content",
            replacement,
            "--doc-format",
            "markdown",
            "--as",
            self.settings.feishu_as,
        ])
        return True

    def insert_file(self, document_id: str, path: Path) -> None:
        resolved = path.expanduser().resolve()
        self._call([
            self.settings.lark_cli,
            "docs",
            "+media-insert",
            "--doc",
            document_id,
            "--type",
            "file",
            "--file",
            f"./{resolved.name}",
            "--as",
            self.settings.feishu_as,
            "--format",
            "json",
        ], cwd=resolved.parent)

    def deduplicate_files(self, document_id: str) -> int:
        """Remove replayed file cards while retaining distinct file versions."""
        # Drive applies text edits and media replay asynchronously; wait for
        # tokenized blocks to settle before reading the document back.
        time.sleep(3)
        fetched = self._call([
            self.settings.lark_cli,
            "docs",
            "+fetch",
            "--doc",
            document_id,
            "--doc-format",
            "xml",
            "--detail",
            "with-ids",
            "--scope",
            "full",
            "--as",
            self.settings.feishu_as,
        ])
        content = str(fetched.get("data", {}).get("document", {}).get("content") or "")
        figures = re.findall(r'<figure id="([^"]+)"[^>]*>.*?<source([^>]*)/>.*?</figure>', content, flags=re.S)
        seen: set[tuple[str, str]] = set()
        removed = 0
        for block_id, attributes in figures:
            name_match = re.search(r'\bname="([^"]+)"', attributes)
            size_match = re.search(r'\bsize="([^"]+)"', attributes)
            if not name_match:
                continue
            identity = (name_match.group(1), size_match.group(1) if size_match else "")
            if identity not in seen:
                seen.add(identity)
                continue
            self._call([
                self.settings.lark_cli,
                "docs",
                "+update",
                "--doc",
                document_id,
                "--command",
                "block_delete",
                "--block-id",
                block_id,
                "--format",
                "json",
                "--as",
                self.settings.feishu_as,
            ])
            removed += 1
        return removed

    def _call(self, args: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            completed = subprocess.run(
                args,
                cwd=cwd or self.settings.root,
                env=self.settings.command_env(),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if completed.returncode != 0:
                command_text = " ".join(args[:5])
                raise RuntimeError(f"{command_text}: {completed.stderr.strip() or completed.stdout.strip() or 'Feishu document command failed'}")
            # Some lark-cli shortcuts print progress lines before JSON. Take the
            # last JSON object rather than logging those lines as user content.
            decoder = json.JSONDecoder()
            candidates: list[dict[str, Any]] = []
            for index, character in enumerate(completed.stdout):
                if character != "{":
                    continue
                try:
                    value, _ = decoder.raw_decode(completed.stdout[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    candidates.append(value)
            if candidates:
                value = next((item for item in reversed(candidates) if "ok" in item or "data" in item), candidates[-1])
                if value.get("ok") is False:
                    raise RuntimeError(json.dumps(value, ensure_ascii=False))
                return value
            raise RuntimeError(f"Feishu document command returned no JSON: {completed.stdout[-500:]}")

        return retry_call(operation, attempts=self.settings.retry_attempts, base_seconds=self.settings.retry_base_seconds, retryable=is_transient_error)
