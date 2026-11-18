"""FastAPI dependencies: DB session, clock, settings."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings
from netracare.db.session import begin_write
from netracare.domain.clock import Clock
from netracare.errors import AppError
from netracare.security.rate_limit import LoginThrottle

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


def get_db(request: Request) -> Iterator[Session]:
    """One transaction per request: commit on success, rollback on error.

    Mutating requests start a write transaction up-front (SQLite BEGIN IMMEDIATE) so that
    concurrent writers are serialised instead of racing on lock upgrades. An `AppError`
    raised with `commit=True` keeps its side effects (e.g. failed-login audit rows).
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    if request.method not in _READ_METHODS:
        begin_write(session)
    try:
        yield session
        session.commit()
    except AppError as exc:
        if exc.commit:
            session.commit()
        else:
            session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
ThrottleDep = Annotated[LoginThrottle, Depends(get_throttle)]
