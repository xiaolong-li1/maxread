from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class CleanupResult:
    source_kind: str
    source_id: str
    files_removed: int = 0
    bytes_removed: int = 0
    source_found: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def cleanup_source_cache(
    workdir: Path,
    source_kind: str,
    source_id: str,
    *,
    dry_run: bool = False,
) -> CleanupResult:
    collection = "papers" if source_kind == "paper" else "articles" if source_kind == "article" else ""
    if not collection:
        raise ValueError(f"unsupported source kind: {source_kind}")
    root = workdir.resolve()
    source_dir = (root / collection / source_id).resolve()
    expected_parent = (root / collection).resolve()
    if source_dir.parent != expected_parent:
        raise ValueError("source cache path escaped its collection root")
    if not source_dir.exists():
        return CleanupResult(source_kind, source_id, dry_run=dry_run)

    files_removed = 0
    bytes_removed = 0
    for child in source_dir.iterdir():
        if child.name == "pipeline_artifacts":
            continue
        child_files, child_bytes = _path_usage(child)
        files_removed += child_files
        bytes_removed += child_bytes
        if dry_run:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    return CleanupResult(source_kind, source_id, files_removed, bytes_removed, True, dry_run)


def cleanup_completed_cache(store, workdir: Path, older_than_hours: float, *, dry_run: bool = False) -> dict[str, object]:
    cutoff = datetime.now(UTC) - timedelta(hours=max(0.0, float(older_than_hours)))
    candidates = store.list_cache_cleanup_candidates(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
    results = [
        cleanup_source_cache(workdir, row["source_kind"], row["source_id"], dry_run=dry_run)
        for row in candidates
    ]
    return {
        "ok": True,
        "dry_run": dry_run,
        "candidates": len(candidates),
        "sources_found": sum(item.source_found for item in results),
        "files_removed": sum(item.files_removed for item in results),
        "bytes_removed": sum(item.bytes_removed for item in results),
    }


def local_date_cutoff_utc(value: str, timezone_name: str = "Asia/Shanghai") -> str:
    local_date = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=ZoneInfo(timezone_name))
    return local_date.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _path_usage(path: Path) -> tuple[int, int]:
    if path.is_file() or path.is_symlink():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 1, 0
    files = 0
    size = 0
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        files += 1
        try:
            size += child.stat().st_size
        except OSError:
            pass
    return files, size
