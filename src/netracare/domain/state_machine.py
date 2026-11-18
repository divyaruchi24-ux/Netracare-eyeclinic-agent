"""Appointment status state machine.

Only checks legality of (state, event) and the actor's role. Ownership, time windows and
approval rules live in `netracare.domain.policies`.
"""

from __future__ import annotations

from typing import Final, Literal

from netracare.domain.enums import AppointmentEvent as E
from netracare.domain.enums import AppointmentStatus as S
from netracare.domain.enums import Role

Actor = Role | Literal["system"]

_STAFF: Final[frozenset[Actor]] = frozenset({Role.FRONT_DESK, Role.ADMIN})
_STAFF_AND_PATIENT: Final[frozenset[Actor]] = _STAFF | {Role.PATIENT}
_SYSTEM: Final[frozenset[Actor]] = frozenset({"system"})

TRANSITIONS: Final[dict[tuple[S, E], tuple[S, frozenset[Actor]]]] = {
    (S.REQUESTED, E.SUBMIT_FOR_APPROVAL): (S.PENDING_APPROVAL, _SYSTEM),
    (S.REQUESTED, E.AUTO_CONFIRM): (S.CONFIRMED, _SYSTEM),
    (S.PENDING_APPROVAL, E.APPROVE): (S.CONFIRMED, _STAFF),
    (S.PENDING_APPROVAL, E.REJECT): (S.REJECTED, _STAFF),
    (S.PENDING_APPROVAL, E.CANCEL): (S.CANCELLED, _STAFF_AND_PATIENT),
    (S.CONFIRMED, E.CANCEL): (S.CANCELLED, _STAFF_AND_PATIENT),
    (S.CONFIRMED, E.RESCHEDULE): (S.RESCHEDULED, _STAFF_AND_PATIENT),
    (S.CONFIRMED, E.CHECK_IN): (S.CHECKED_IN, _STAFF),
    (S.CONFIRMED, E.MARK_NO_SHOW): (S.NO_SHOW, _STAFF),
    (S.CHECKED_IN, E.COMPLETE): (S.COMPLETED, _STAFF | {Role.DOCTOR}),
}


class InvalidTransitionError(Exception):
    def __init__(self, current: S, event: E) -> None:
        super().__init__(f"cannot apply '{event}' to appointment in state '{current}'")
        self.current = current
        self.event = event


class TransitionForbiddenError(Exception):
    def __init__(self, current: S, event: E, actor: Actor) -> None:
        super().__init__(f"actor '{actor}' may not apply '{event}' in state '{current}'")
        self.current = current
        self.event = event
        self.actor = actor


def transition(current: S, event: E, actor: Actor) -> S:
    """Return the next status or raise if illegal / not permitted for this actor."""
    try:
        target, allowed = TRANSITIONS[(current, event)]
    except KeyError:
        raise InvalidTransitionError(current, event) from None
    if actor not in allowed:
        raise TransitionForbiddenError(current, event, actor)
    return target


def allowed_events(current: S) -> list[E]:
    return [event for (state, event) in TRANSITIONS if state == current]
