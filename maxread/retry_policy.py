from __future__ import annotations

from collections.abc import Mapping


_DELIVERY_INFRASTRUCTURE_MARKERS = (
    "visual-qa:infrastructure:",
    "visual-qa:remote-error",
    "visual-qa:recheck-error",
    "visual runner failed",
    "browser timeout",
    "browser timed out",
    "browser crashed",
    "login.feishu.cn/accounts/trap",
    "feishu pdf export failed",
    "pdf export timeout",
    "pdf export timed out",
    "export-pending",
)

_DETERMINISTIC_QUALITY_MARKERS = (
    "post-publish:quality:",
    "quality:formula:",
    "quality:format:",
    "quality:figure:",
    "quality:xml:",
    "visual-qa:high:",
    "visual-qa:medium:",
    "invalid-formula",
    "missing-formula",
    "raw-formatting",
    "raw-tex-formatting-command",
    "missing-image",
    "missing-figure",
    "table-overflow",
    "table-clipped",
)


def has_published_checkpoint(job: Mapping) -> bool:
    return bool(
        str(job.get("checkpoint_json") or "").strip()
        or str(job.get("doc_url") or "").strip()
    )


def should_resume_published(error: str, *, has_checkpoint: bool) -> bool:
    """Resume only when delivery tooling failed around an otherwise sound document."""
    if not has_checkpoint:
        return False
    value = str(error or "").strip().lower()
    if not value or any(marker in value for marker in _DETERMINISTIC_QUALITY_MARKERS):
        return False
    return any(marker in value for marker in _DELIVERY_INFRASTRUCTURE_MARKERS)


def retry_requires_rebuild(job: Mapping) -> bool:
    return not should_resume_published(
        str(job.get("error") or ""),
        has_checkpoint=has_published_checkpoint(job),
    )
