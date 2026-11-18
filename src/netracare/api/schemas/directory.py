from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import EmailStr, Field, StringConstraints

from netracare.api.schemas.auth import Phone
from netracare.api.schemas.common import Schema, ShortText
from netracare.db.models import Doctor, Patient
from netracare.domain.policies import age_on, is_minor

DoctorCode = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9]{1,15}$")
]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9-]{1,128}$")]
SpecialtyCode = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
]


class SpecialtyOut(Schema):
    id: str
    code: str
    name: str
    description: str


class SpecialtyRef(Schema):
    code: str
    name: str


class DoctorOut(Schema):
    id: str
    code: str
    full_name: str
    specialty: SpecialtyRef
    bio_slug: str | None
    is_active: bool

    @classmethod
    def of(cls, d: Doctor) -> DoctorOut:
        return cls(
            id=d.id,
            code=d.code,
            full_name=d.full_name,
            specialty=SpecialtyRef(code=d.specialty.code, name=d.specialty.name),
            bio_slug=d.bio_slug,
            is_active=d.is_active,
        )


class DoctorCreate(Schema):
    code: DoctorCode
    full_name: ShortText
    specialty_code: SpecialtyCode
    bio_slug: Slug | None = None


class DoctorUpdateIn(Schema):
    full_name: ShortText | None = None
    specialty_code: SpecialtyCode | None = None
    bio_slug: Slug | None = None
    is_active: bool | None = None


class DoctorChangeOut(Schema):
    doctor: DoctorOut
    affected_appointment_ids: list[str]


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = [c for c in phone if c.isdigit()]
    return "•••• " + "".join(digits[-4:]) if len(digits) >= 4 else "••••"


class GuardianOut(Schema):
    user_id: str
    email: str
    relationship: str


class PatientOut(Schema):
    id: str
    full_name: str
    dob: date
    age: int
    is_minor: bool
    phone_masked: str | None
    email: str | None
    no_show_count: int
    flagged: bool
    has_account: bool
    guardians: list[GuardianOut] = Field(default_factory=list)

    @classmethod
    def of(cls, p: Patient, today: date, guardians: list[GuardianOut] | None = None) -> PatientOut:
        return cls(
            id=p.id,
            full_name=p.full_name,
            dob=p.dob,
            age=age_on(p.dob, today),
            is_minor=is_minor(p.dob, today),
            phone_masked=mask_phone(p.phone),
            email=p.email,
            no_show_count=p.no_show_count,
            flagged=p.flagged,
            has_account=p.user_id is not None,
            guardians=guardians or [],
        )


class GuardianLinkIn(Schema):
    guardian_email: EmailStr
    relationship: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
    ]


class NewPatientIn(Schema):
    full_name: ShortText
    dob: date
    phone: Phone | None = None
    email: EmailStr | None = None
