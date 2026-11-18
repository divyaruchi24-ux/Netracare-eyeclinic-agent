"""Doctors, specialties, patients, users and knowledge-doc lookups."""

from __future__ import annotations

from sqlalchemy import select

from netracare.db.models import Doctor, KnowledgeDoc, Patient, Specialty, User
from netracare.repositories.base import Repository


class SpecialtyRepository(Repository[Specialty]):
    model = Specialty

    def by_code(self, code: str) -> Specialty | None:
        return self.session.scalar(select(Specialty).where(Specialty.code == code))


class DoctorRepository(Repository[Doctor]):
    model = Doctor

    def by_code(self, code: str) -> Doctor | None:
        return self.session.scalar(select(Doctor).where(Doctor.code == code))

    def active(self) -> list[Doctor]:
        return list(
            self.session.scalars(select(Doctor).where(Doctor.is_active).order_by(Doctor.code))
        )


class UserRepository(Repository[User]):
    model = User

    def by_email(self, email: str) -> User | None:
        return self.session.scalar(select(User).where(User.email == email.lower()))


class PatientRepository(Repository[Patient]):
    model = Patient

    def by_email(self, email: str) -> Patient | None:
        return self.session.scalar(select(Patient).where(Patient.email == email.lower()))


class KnowledgeDocRepository(Repository[KnowledgeDoc]):
    model = KnowledgeDoc

    def by_slug_version(self, slug: str, version: int) -> KnowledgeDoc | None:
        return self.session.scalar(
            select(KnowledgeDoc).where(KnowledgeDoc.slug == slug, KnowledgeDoc.version == version)
        )

    def current(self) -> list[KnowledgeDoc]:
        return list(
            self.session.scalars(
                select(KnowledgeDoc).where(KnowledgeDoc.superseded_by_id.is_(None))
            )
        )
