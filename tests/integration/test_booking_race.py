"""Concurrency: many clients hit the same slot at the same moment.

Each thread has its own TestClient (and therefore its own DB connection/session). A barrier
releases them together. Exactly one booking may win; everyone else gets a clean 409.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import Appointment, AppointmentSlot
from tests.fixtures_api import PASSWORD, did, fresh, local, patient_email, pid, slot_ids_at

pytestmark = pytest.mark.db

N = 10
# Ordinary adult patients with their own accounts (P009..P029), skipping none-flagged ones.
PATIENT_KEYS = [f"P{i:03d}" for i in range(10, 10 + N)]


def _token(app: FastAPI, email: str) -> str:
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        tok: str = r.json()["access_token"]
        return tok


def _race(app: FastAPI, jobs: list[tuple[str, dict[str, object], str]]) -> list[int]:
    barrier = threading.Barrier(len(jobs))

    def run(job: tuple[str, dict[str, object], str]) -> int:
        token, body, key = job
        with TestClient(app, raise_server_exceptions=False) as c:
            barrier.wait()
            r = c.post(
                "/appointments",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
            )
            return r.status_code

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return list(pool.map(run, jobs))


def test_ten_patients_one_slot_exactly_one_wins(app: FastAPI, db: Session) -> None:
    slot = slot_ids_at(db, "D5", local(13, 10))
    jobs = []
    for i, key in enumerate(PATIENT_KEYS):
        body = {"patient_id": pid(key), "doctor_id": did("D5"), "slot_ids": slot, "type": "routine"}
        jobs.append((_token(app, patient_email(key)), body, f"race-key-{i:04d}"))
    statuses = _race(app, jobs)
    assert sorted(statuses) == [201] + [409] * (N - 1), statuses
    fresh(db)
    holds = db.scalar(
        select(func.count())
        .select_from(AppointmentSlot)
        .where(AppointmentSlot.slot_id == slot[0], AppointmentSlot.active)
    )
    assert holds == 1
    assert db.scalar(select(func.count()).select_from(Appointment)) == 1


def test_double_submit_same_key_concurrent(app: FastAPI, db: Session) -> None:
    slot = slot_ids_at(db, "D5", local(13, 10))
    token = _token(app, patient_email("P010"))
    body = {"patient_id": pid("P010"), "doctor_id": did("D5"), "slot_ids": slot, "type": "routine"}
    statuses = _race(app, [(token, body, "double-click-1")] * 5)
    assert statuses == [201] * 5, statuses  # the first books, the rest replay
    assert fresh(db).scalar(select(func.count()).select_from(Appointment)) == 1
