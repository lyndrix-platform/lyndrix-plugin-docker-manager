"""Database helpers for the Docker Manager plugin (cluster-safe).

Pattern shared with the Server Manager plugin: idempotent DDL serialized by the
core's bootstrap advisory lock, short READ COMMITTED sessions and a retry
wrapper for MariaDB snapshot-isolation conflicts (1020) and lock errors.
"""
from __future__ import annotations

import time
from contextlib import contextmanager, nullcontext
from typing import Callable, Generator, TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.api import db_instance, get_logger

from .models import _Base

log = get_logger("Plugin:DockerManager:DB")

T = TypeVar("T")

TABLE = "docker_manager_hosts"

# A lost bootstrap race: a peer created the table / column / index first.
_DDL_RACE_CODES = ("1050", "1060", "1061")
_DDL_RACE_TEXT = ("already exists", "duplicate column", "duplicate key name")

# MariaDB >= 11.6 (innodb_snapshot_isolation=ON) raises 1020 when a row changed
# since the transaction's first read; 1205 = lock wait timeout, 1213 = deadlock.
_RETRY_CODES = ("1020", "1205", "1213")


def is_connected() -> bool:
    return bool(getattr(db_instance, "is_connected", False)) and getattr(db_instance, "engine", None) is not None


def _is_ddl_race(exc: Exception) -> bool:
    msg = str(getattr(exc, "orig", exc))
    return any(code in msg for code in _DDL_RACE_CODES) or any(t in msg.lower() for t in _DDL_RACE_TEXT)


def _is_retryable(exc: Exception, *, integrity: bool) -> bool:
    if integrity and isinstance(exc, IntegrityError):
        return True
    return any(code in str(getattr(exc, "orig", exc)) for code in _RETRY_CODES)


def bootstrap_lock():
    """``db_instance.advisory_lock("lyndrix_bootstrap")`` on cores that have it.

    Never nest two of these in one thread: MariaDB GET_LOCK runs on a dedicated
    connection per call, so a nested acquisition would wait for its own lock.
    """
    lock = getattr(db_instance, "advisory_lock", None)
    return lock("lyndrix_bootstrap") if callable(lock) else nullcontext()


def ensure_tables() -> None:
    """Create ``docker_manager_hosts`` if missing (idempotent, blocking)."""
    if not is_connected():
        raise RuntimeError("Database engine not ready — call after db:connected")
    with bootstrap_lock():
        try:
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
        except Exception as exc:
            if not _is_ddl_race(exc):
                raise
            log.info(f"DB: create_all lost a bootstrap race ({exc}); re-checking")
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
    log.info(f"DB: {TABLE} table ready")


def retry_write(
    fn: Callable[[], T], *, attempts: int = 4, pause: float = 0.05, integrity: bool = False
) -> T:
    """Run ``fn()`` — one complete short transaction — again on 1020/1205/1213.

    ``integrity=True`` additionally retries an ``IntegrityError`` (a peer inserted
    the same unique name between our read and our insert; the next attempt sees
    the row and updates it). ``fn`` must open its own session per attempt.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt + 1 < attempts and _is_retryable(exc, integrity=integrity):
                log.debug(f"DB: retrying write after {exc!s:.120}")
                time.sleep(pause * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a READ COMMITTED session (no-op on SQLite), rolling back on exception."""
    if not is_connected() or not getattr(db_instance, "SessionLocal", None):
        raise RuntimeError("Database not connected")
    session: Session = db_instance.SessionLocal()
    try:
        _read_committed(session)
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _read_committed(session: Session) -> None:
    try:
        bind = session.get_bind()
        if bind is not None and bind.dialect.name in ("mysql", "mariadb"):
            session.connection(execution_options={"isolation_level": "READ COMMITTED"})
    except Exception as exc:  # isolation is an optimisation, never a reason to fail
        log.debug(f"DB: READ COMMITTED not applied: {exc}")
