"""API test fixtures: a seeded DB (built once per session, copied per test), a controllable
clock frozen at Tue 2026-11-10 09:00 IST, and login helpers for every role."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from netracare.api.main import create_app
from netracare.config import Settings
from netracare.db.models import AppointmentSlot, AuditLog, Doctor, Patient, Slot
from netracare.db.session import make_engine, make_session_factory
from netracare.domain.enums import SlotStatus
from netracare.seed.ids import seed_id
from netracare.seed.patients import make_patients
from netracare.seed.runner import Seeder
from tests.conftest import ROOT, alembic_config
from tests.helpers import MutableClock

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 11, 10)  # a Tuesday
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=IST)
PASSWORD = "NetraCare#Dev1"
ADMIN = "admin@netracare.example"
FRONTDESK = "frontdesk1@netracare.example"
FRONTDESK2 = "frontdesk2@netracare.example"
DOCTOR_EMAILS = {
    "D1": "ananya.rao@netracare.example",
    "D2": "vikram.shetty@netracare.example",
    "D3": "meera.iyer@netracare.example",
    "D4": "arjun.menon@netracare.example",
    "D5": "kavya.nair@netracare.example",
}
SEEDED = {p.key: p for p in make_patients(TODAY)}


def patient_email(key: str) -> str:
    """Login email for an adult patient with their own account (P006+)."""
    email = SEEDED[key].email
    assert email is not None
    return email


def guardian_email(key: str) -> str:
    """Login email of the guardian of a dependant patient (P000-P005)."""
    g = SEEDED[key].guardian
    assert g is not None
    return g.email


def pid(key: str) -> str:
    return seed_id("patient", key)


def did(code: str) -> str:
    return seed_id("doctor", code)


def make_settings(db_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="dev",
        database_url=db_url,
        jwt_secret="test-secret-test-secret-test-secret-123",  # type: ignore[arg-type]
        opik_enabled=False,
    )


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("seed") / "template.db"
    url = f"sqlite:///{path.as_posix()}"
    command.upgrade(alembic_config(url), "head")
    engine = make_engine(url)
    with make_session_factory(engine)() as s:
        Seeder(
            s,
            data_dir=ROOT / "seed" / "data",
            kb_dir=ROOT / "kb",
            root=ROOT,
            today=TODAY,
            tz=IST,
            password=PASSWORD,
        ).run()
        s.commit()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    return path


@pytest.fixture
def api_db_url(seeded_template: Path, tmp_path: Path) -> str:
    target = tmp_path / "api.db"
    shutil.copyfile(seeded_template, target)
    return f"sqlite:///{target.as_posix()}"


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(NOW)


@pytest.fixture
def api_engine(api_db_url: str) -> Iterator[Engine]:
    engine = make_engine(api_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def app(api_db_url: str, api_engine: Engine, clock: MutableClock) -> FastAPI:
    return create_app(make_settings(api_db_url), clock=clock, engine=api_engine)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(api_engine: Engine) -> Iterator[Session]:
    """A separate session for assertions (sees committed data)."""
    factory: sessionmaker[Session] = make_session_factory(api_engine)
    with factory() as s:
        yield s


Headers = dict[str, str]


class _AuthHeaders(dict[str, str]):
    """Authorization header that re-logs-in transparently when the test clock has moved
    past the access-token lifetime (tests jump the clock by hours)."""

    def __init__(self, client: TestClient, clock: MutableClock, email: str, password: str) -> None:
        super().__init__()
        self._client, self._clock, self._email, self._password = client, clock, email, password
        self._issued: datetime | None = None
        self._refresh()

    def _refresh(self) -> None:
        r = self._client.post(
            "/auth/login", json={"email": self._email, "password": self._password}
        )
        assert r.status_code == 200, r.text
        self["Authorization"] = f"Bearer {r.json()['access_token']}"
        self._issued = self._clock.now_utc()

    def _ensure(self) -> None:
        assert self._issued is not None
        if self._clock.now_utc() - self._issued >= timedelta(minutes=10) or (
            self._clock.now_utc() < self._issued
        ):
            self._refresh()

    def keys(self):  # type: ignore[no-untyped-def,override]
        self._ensure()
        return super().keys()

    def __getitem__(self, key: str) -> str:
        self._ensure()
        return super().__getitem__(key)

    def items(self):  # type: ignore[no-untyped-def,override]
        self._ensure()
        return super().items()

    def __iter__(self):  # type: ignore[no-untyped-def,override]
        self._ensure()
        return super().__iter__()


@pytest.fixture
def login(client: TestClient, clock: MutableClock) -> Callable[..., Headers]:
    cache: dict[str, Headers] = {}

    def _login(email: str, password: str = PASSWORD) -> Headers:
        if email not in cache:
            cache[email] = _AuthHeaders(client, clock, email, password)
        return cache[email]

    return _login


@pytest.fixture
def admin_headers(login: Callable[..., Headers]) -> Headers:
    return login(ADMIN)


@pytest.fixture
def frontdesk_headers(login: Callable[..., Headers]) -> Headers:
    return login(FRONTDESK)


@pytest.fixture
def patient_headers(login: Callable[..., Headers]) -> Headers:
    """P010: an ordinary adult patient with no no-show flag."""
    return login(patient_email("P010"))


@pytest.fixture
def doctor_headers(login: Callable[..., Headers]) -> Callable[[str], Headers]:
    return lambda code: login(DOCTOR_EMAILS[code])


def fresh(db: Session) -> Session:
    """End the assertion session's read transaction so it sees the latest commits (WAL)."""
    db.rollback()
    return db


def assert_audited(db: Session, action: str, entity_id: str | None = None) -> AuditLog:
    fresh(db)
    stmt = select(AuditLog).where(AuditLog.action == action)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    row = db.scalars(stmt.order_by(AuditLog.at_utc.desc())).first()
    assert row is not None, f"no audit row for {action} / {entity_id}"
    return row


def first_doctor_slot_ids(db: Session, code: str, after: datetime, n: int = 1) -> list[str]:
    """IDs of the first `n` consecutive open, unheld slots for a doctor starting after `after`."""
    held = select(AppointmentSlot.slot_id).where(AppointmentSlot.active)
    rows = list(
        db.scalars(
            select(Slot)
            .where(
                Slot.doctor_id == did(code),
                Slot.status == SlotStatus.OPEN,
                Slot.start_utc > after,
                Slot.id.not_in(held),
            )
            .order_by(Slot.start_utc)
        )
    )
    for i in range(len(rows) - n + 1):
        window = rows[i : i + n]
        if all(window[j].end_utc == window[j + 1].start_utc for j in range(n - 1)):
            return [r.id for r in window]
    raise AssertionError(f"no {n} consecutive open slots for {code}")


def doctor(db: Session, code: str) -> Doctor:
    d = db.scalar(select(Doctor).where(Doctor.code == code))
    assert d is not None
    return d


def patient(db: Session, key: str) -> Patient:
    p = db.get(Patient, pid(key))
    assert p is not None
    return p


def local(day: int, hh: int, mm: int = 0, month: int = 11) -> datetime:
    """A clinic-local datetime in 2026 (defaults to November)."""
    return datetime(2026, month, day, hh, mm, tzinfo=IST)


def slot_ids_at(db: Session, code: str, start_local: datetime, n: int = 1) -> list[str]:
    """IDs of `n` back-to-back slots for a doctor starting exactly at `start_local`."""
    ids: list[str] = []
    t = start_local
    for _ in range(n):
        slot = db.scalar(select(Slot).where(Slot.doctor_id == did(code), Slot.start_utc == t))
        assert slot is not None, f"no slot for {code} at {t}"
        ids.append(slot.id)
        t = slot.end_utc
    return ids


_key_counter = iter(range(1_000_000))


def new_key() -> str:
    return f"test-key-{next(_key_counter):06d}"


def book_api(
    client: TestClient,
    headers: Headers,
    patient_id: str,
    doctor_code: str,
    slot_ids: list[str],
    *,
    type: str = "routine",
    key: str | None = None,
    **extra: object,
) -> Any:
    body = {
        "patient_id": patient_id,
        "doctor_id": did(doctor_code),
        "slot_ids": slot_ids,
        "type": type,
        **extra,
    }
    return client.post(
        "/appointments", json=body, headers={**headers, "Idempotency-Key": key or new_key()}
    )


def booked(
    client: TestClient,
    headers: Headers,
    patient_id: str,
    doctor_code: str,
    slot_ids: list[str],
    **kw: Any,
) -> dict[str, Any]:
    r = book_api(client, headers, patient_id, doctor_code, slot_ids, **kw)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out
