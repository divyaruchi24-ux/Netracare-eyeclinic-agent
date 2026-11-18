from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import (
    AuditLog,
    Doctor,
    Holiday,
    KnowledgeDoc,
    Leave,
    Patient,
    PatientGuardian,
    Slot,
    Specialty,
    StaffUser,
    User,
)
from netracare.db.session import make_engine, make_session_factory
from netracare.domain.enums import Role, SlotStatus
from netracare.domain.policies import is_minor
from netracare.security.passwords import verify_password
from netracare.seed.kb import FrontMatterError, parse_front_matter, scan_kb
from netracare.seed.patients import make_patients
from netracare.seed.runner import Seeder
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 11, 10)  # Tuesday


def _seed(session: Session) -> Seeder:
    seeder = Seeder(
        session,
        data_dir=ROOT / "seed" / "data",
        kb_dir=ROOT / "kb",
        root=ROOT,
        today=TODAY,
        tz=IST,
        password="test-password",
        weeks=8,
    )
    seeder.run()
    session.flush()
    return seeder


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
    """Seed one database per module; tests only read (idempotency test re-runs seeding)."""
    url = f"sqlite:///{(tmp_path_factory.mktemp('seed') / 'seed.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    engine = make_engine(url)
    session = make_session_factory(engine)()
    _seed(session)
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _count(session: Session, model: type) -> int:  # type: ignore[type-arg]
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_seed_counts(seeded: Session) -> None:
    session = seeded
    assert _count(session, Specialty) == 5
    assert _count(session, Doctor) == 5
    assert _count(session, Patient) == 30
    assert _count(session, StaffUser) == 8  # 5 doctors + admin + 2 front desk
    rows = session.execute(select(User.role, func.count()).group_by(User.role))
    roles = {role: n for role, n in rows}
    assert roles[Role.ADMIN] == 1
    assert roles[Role.FRONT_DESK] == 2
    assert roles[Role.DOCTOR] == 5
    assert _count(session, Slot) > 1000
    assert _count(session, KnowledgeDoc) >= 20


def test_seed_idempotent(seeded: Session) -> None:
    session = seeded
    tables = (User, Doctor, Patient, PatientGuardian, Slot, Leave, Holiday, KnowledgeDoc, AuditLog)
    before = {m.__tablename__: _count(session, m) for m in tables}
    second = _seed(session)
    after = {m.__tablename__: _count(session, m) for m in tables}
    assert second.result.inserted == {}
    assert before == after


def test_seeded_password_verifies(seeded: Session) -> None:
    session = seeded
    admin = session.scalar(select(User).where(User.email == "admin@netracare.example"))
    assert admin is not None
    assert verify_password("test-password", admin.password_hash)
    assert not verify_password("wrong", admin.password_hash)


def test_minors_have_guardians(seeded: Session) -> None:
    session = seeded
    patients = list(session.scalars(select(Patient)))
    minors = [p for p in patients if is_minor(p.dob, TODAY)]
    assert len(minors) == 5
    for m in minors:
        assert m.user_id is None
        assert session.scalar(select(func.count()).where(PatientGuardian.patient_id == m.id)) == 1
    assert _count(session, PatientGuardian) == 6  # 5 minors + 1 elderly


def test_no_show_flags(seeded: Session) -> None:
    session = seeded
    flagged = list(session.scalars(select(Patient).where(Patient.flagged)))
    assert len(flagged) == 3
    assert all(p.no_show_count >= 3 for p in flagged)


def test_no_slots_on_holidays_sundays_or_lunch(seeded: Session) -> None:
    session = seeded
    holidays = set(session.scalars(select(Holiday.day)))
    for slot in session.scalars(select(Slot)):
        local = slot.start_utc.astimezone(IST)
        assert local.date() not in holidays
        assert local.weekday() != 6
        assert not (13 <= local.hour < 14)
        assert 9 <= local.hour < 18


def test_no_open_slots_during_leave(seeded: Session) -> None:
    session = seeded
    for leave in session.scalars(select(Leave)):
        n = session.scalar(
            select(func.count()).where(
                Slot.doctor_id == leave.doctor_id,
                Slot.start_utc < leave.end_utc,
                Slot.end_utc > leave.start_utc,
                Slot.status == SlotStatus.OPEN,
            )
        )
        assert n == 0


def test_surgery_days_have_no_opd(seeded: Session) -> None:
    session = seeded
    d1 = session.scalar(select(Doctor).where(Doctor.code == "D1"))
    assert d1 is not None
    weekdays = {
        s.start_utc.astimezone(IST).weekday()
        for s in session.scalars(select(Slot).where(Slot.doctor_id == d1.id))
    }
    assert 2 not in weekdays  # Wednesday surgery day


def test_christmas_holiday_in_window(seeded: Session) -> None:
    session = seeded
    lo = datetime(2026, 12, 25, tzinfo=IST)
    n = session.scalar(
        select(func.count()).where(Slot.start_utc >= lo, Slot.start_utc < lo + timedelta(days=1))
    )
    assert n == 0


def test_price_list_v1_superseded_by_v2(seeded: Session) -> None:
    session = seeded
    docs = list(
        session.scalars(
            select(KnowledgeDoc)
            .where(KnowledgeDoc.slug == "price-list")
            .order_by(KnowledgeDoc.version)
        )
    )
    assert [d.version for d in docs] == [1, 2]
    assert docs[0].superseded_by_id == docs[1].id
    assert docs[1].superseded_by_id is None


def test_seed_writes_audit_entry(seeded: Session) -> None:
    session = seeded
    actions = list(session.scalars(select(AuditLog.action)))
    assert actions == ["seed.run"]


# --- pure helpers (no DB) --------------------------------------------------------------------


def test_kb_front_matter_valid() -> None:
    files = scan_kb(ROOT / "kb")
    assert len(files) >= 20
    for f in files:
        assert (
            "Not medical advice" in f.body
            or "estimates only" in f.body
            or f.meta["doc_type"]
            in {
                "policy",
                "general_faq",
                "insurance",
            }
        )
    slugs_versions = [(f.meta["slug"], f.meta["version"]) for f in files]
    assert len(slugs_versions) == len(set(slugs_versions))


@pytest.mark.parametrize(
    "text",
    [
        "no front matter",
        "---\nslug: x\n",
        "---\n- a list\n---\nbody",
        "---\nslug: x\ntitle: t\ndoc_type: bogus\nversion: 1\neffective_date: 2026-01-01\n---\n",
        "---\nslug: x\ntitle: t\ndoc_type: policy\nversion: 1\neffective_date: soon\n---\n",
        "---\nslug: x\n---\nbody",
    ],
)
def test_bad_front_matter_rejected(text: str) -> None:
    with pytest.raises((FrontMatterError, ValueError)):
        parse_front_matter(text)


def test_patients_deterministic_and_fake() -> None:
    a, b = make_patients(TODAY), make_patients(TODAY)
    assert a == b
    for p in a:
        assert p.phone.startswith("+91 90000 ")
        assert p.email is None or p.email.endswith("@example.com")


def test_make_patients_requires_minimum() -> None:
    with pytest.raises(ValueError):
        make_patients(TODAY, n=5)
