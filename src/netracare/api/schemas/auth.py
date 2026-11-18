from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import EmailStr, Field, StringConstraints

from netracare.api.schemas.common import Schema, ShortText
from netracare.domain.enums import Role

Password = Annotated[str, Field(min_length=10, max_length=128)]
Phone = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9][0-9 -]{7,18}[0-9]$")
]


class RegisterRequest(Schema):
    email: EmailStr
    password: Password
    full_name: ShortText
    dob: date
    phone: Phone | None = None


class LoginRequest(Schema):
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=255)]
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(Schema):
    refresh_token: Annotated[str, Field(min_length=10, max_length=256)]


class TokenPairOut(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int


class MeResponse(Schema):
    user_id: str
    email: str
    role: Role
    display_name: str
    doctor_id: str | None = None
    patient_ids: list[str] = Field(default_factory=list)
