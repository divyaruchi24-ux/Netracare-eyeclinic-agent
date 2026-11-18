"""Engine and session factories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import get_settings

WRITE_TX_OPTION = "netracare_write_tx"


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        Path(url[len(prefix) :]).parent.mkdir(parents=True, exist_ok=True)


def make_engine(url: str | None = None, echo: bool = False) -> Engine:
    url = url or get_settings().database_url
    is_sqlite = url.startswith("sqlite")
    connect_args: dict[str, Any] = {"check_same_thread": False} if is_sqlite else {}
    if is_sqlite:
        _ensure_sqlite_dir(url)
    engine = create_engine(url, echo=echo, connect_args=connect_args, future=True)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
            # Take over transaction control from pysqlite so we can emit BEGIN IMMEDIATE.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn: Any) -> None:
            # Write transactions take the write lock up-front: concurrent writers queue on
            # busy_timeout instead of failing with SQLITE_BUSY_SNAPSHOT on lock upgrade.
            immediate = conn.get_execution_options().get(WRITE_TX_OPTION, False)
            conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def begin_write(session: Session) -> None:
    """Start the session's transaction as a write transaction (BEGIN IMMEDIATE on SQLite).

    Must be called before the session runs any statement; a no-op on other databases.
    """
    session.connection(execution_options={WRITE_TX_OPTION: True})


@contextmanager
def session_scope(factory: sessionmaker[Session], *, write: bool = False) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on any exception."""
    session = factory()
    if write:
        begin_write(session)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
