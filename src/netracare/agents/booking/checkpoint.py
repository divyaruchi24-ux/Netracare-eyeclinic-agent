"""Checkpointers for the booking agent: SQLite in a separate file (prod), memory (tests).

A separate file keeps checkpoint writes away from the main database's BEGIN IMMEDIATE lock.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from netracare.config import Settings


def make_checkpointer(settings: Settings) -> Any:
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(settings.agent_checkpoint_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def memory_checkpointer() -> Any:
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()
