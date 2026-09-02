from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .base_sync import BaseSync
from .config import PipelineSettings
from .store import PipelineStore


SYNC_KEY = "feishu_base_pull"


def sync_base_to_cache(settings: PipelineSettings) -> dict[str, Any]:
    """Pull the authoritative Feishu Base into the 5090 SQLite read model."""
    started_at = datetime.now(UTC).isoformat()
    store = PipelineStore(settings.db_path)
    store.initialize()
    try:
        states = BaseSync(settings).all_states(refresh=True)
        counts = store.apply_base_snapshot(states, snapshot_started_at=started_at)
    except Exception as exc:
        store.record_sync_state(
            SYNC_KEY,
            status="failed",
            started_at=started_at,
            error=str(exc),
        )
        raise
    result: dict[str, Any] = {"ok": True, "started_at": started_at, **counts}
    store.record_sync_state(
        SYNC_KEY,
        status="completed",
        started_at=started_at,
        details=result,
    )
    return result
