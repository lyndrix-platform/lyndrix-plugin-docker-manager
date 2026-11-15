"""Audit trail: who did what to which container/host, and how it ended.

Append-only by construction — this module offers insert, read and the
retention prune, nothing that edits a row. All functions are blocking (DB).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..model import database as db
from ..model.models import DockerAuditRecord

DEFAULT_RETENTION_DAYS = 90
MAX_PAGE = 500
# Prune in short committed batches so the job never holds locks for long and
# stays far below the core's per-statement time limit.
PRUNE_BATCH = 500
PRUNE_MAX_BATCHES = 200

SYSTEM_ACTOR = "system"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clip(value: Any, limit: int) -> Optional[str]:
    return None if value is None else str(value)[:limit]


def record(
    *,
    actor: Optional[str],
    action: str,
    result: str,
    host_id: Optional[int] = None,
    host_name: Optional[str] = None,
    container: Optional[str] = None,
    detail: Optional[str] = None,
    instance: Optional[str] = None,
) -> None:
    """Insert one entry. Raises when the database is unavailable."""
    entry = dict(
        at=_now(),
        actor=_clip(actor or SYSTEM_ACTOR, 255),
        action=_clip(action, 32),
        host_id=host_id,
        host_name=_clip(host_name, 255),
        container=_clip(container, 255),
        result=_clip(result, 16),
        detail=_clip(detail, 500),
        instance=_clip(instance, 128),
    )

    def _tx() -> None:
        with db.get_session() as s:
            s.add(DockerAuditRecord(**entry))
            s.commit()

    db.retry_write(_tx)


def _to_dict(row: DockerAuditRecord) -> Dict[str, Any]:
    return {
        "id": int(row.id),
        "at": row.at.isoformat() + "Z",
        "actor": row.actor,
        "action": row.action,
        "host_id": row.host_id,
        "host_name": row.host_name,
        "container": row.container,
        "result": row.result,
        "detail": row.detail,
        "instance": row.instance,
    }


def list_entries(
    *,
    limit: int = 100,
    before_id: Optional[int] = None,
    host_id: Optional[int] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Newest first; page backwards with ``before_id`` (keyset, no OFFSET)."""
    limit = max(1, min(int(limit), MAX_PAGE))
    with db.get_session() as s:
        query = s.query(DockerAuditRecord)
        if before_id is not None:
            query = query.filter(DockerAuditRecord.id < int(before_id))
        if host_id is not None:
            query = query.filter(DockerAuditRecord.host_id == int(host_id))
        if action:
            query = query.filter(DockerAuditRecord.action == action)
        if actor:
            query = query.filter(DockerAuditRecord.actor == actor)
        return [_to_dict(r) for r in query.order_by(DockerAuditRecord.id.desc()).limit(limit).all()]


def prune(retention_days: int) -> int:
    """Delete entries older than ``retention_days`` (0 = keep forever).

    Returns the number of deleted rows. Runs on one instance only (the caller
    holds the prober lease); a concurrent insert never conflicts because only
    rows older than the cutoff are touched.
    """
    if retention_days <= 0:
        return 0
    cutoff = _now() - timedelta(days=retention_days)

    def _batch() -> int:
        with db.get_session() as s:
            ids = [
                row_id for (row_id,) in
                s.query(DockerAuditRecord.id).filter(DockerAuditRecord.at < cutoff)
                .order_by(DockerAuditRecord.id).limit(PRUNE_BATCH).all()
            ]
            if not ids:
                return 0
            s.query(DockerAuditRecord).filter(DockerAuditRecord.id.in_(ids)).delete(synchronize_session=False)
            s.commit()
            return len(ids)

    deleted = 0
    for _ in range(PRUNE_MAX_BATCHES):
        count = db.retry_write(_batch)
        deleted += count
        if count < PRUNE_BATCH:
            break
    return deleted
