"""The authenticated caller, as seen by services (HTTP-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass

from netracare.domain.enums import Role

STAFF_ROLES: frozenset[Role] = frozenset({Role.FRONT_DESK, Role.ADMIN})


@dataclass(frozen=True)
class CurrentUser:
    id: str
    role: Role
    email: str
    display_name: str
    doctor_id: str | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES
