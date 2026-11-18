import pytest

from netracare.domain.enums import TERMINAL_STATUSES, Role
from netracare.domain.enums import AppointmentEvent as E
from netracare.domain.enums import AppointmentStatus as S
from netracare.domain.state_machine import (
    TRANSITIONS,
    InvalidTransitionError,
    TransitionForbiddenError,
    allowed_events,
    transition,
)


@pytest.mark.parametrize(("key", "value"), list(TRANSITIONS.items()))
def test_valid_transitions_table(key: tuple[S, E], value: tuple[S, frozenset[object]]) -> None:
    current, event = key
    target, allowed = value
    for actor in allowed:
        assert transition(current, event, actor) is target  # type: ignore[arg-type]


def test_illegal_transition_raises() -> None:
    with pytest.raises(InvalidTransitionError):
        transition(S.COMPLETED, E.CANCEL, Role.ADMIN)
    with pytest.raises(InvalidTransitionError):
        transition(S.REQUESTED, E.CHECK_IN, Role.FRONT_DESK)


def test_wrong_role_forbidden() -> None:
    with pytest.raises(TransitionForbiddenError):
        transition(S.PENDING_APPROVAL, E.APPROVE, Role.PATIENT)
    with pytest.raises(TransitionForbiddenError):
        transition(S.CONFIRMED, E.CHECK_IN, Role.DOCTOR)
    with pytest.raises(TransitionForbiddenError):
        transition(S.REQUESTED, E.AUTO_CONFIRM, Role.ADMIN)


def test_patient_can_cancel_and_reschedule_own() -> None:
    assert transition(S.CONFIRMED, E.CANCEL, Role.PATIENT) is S.CANCELLED
    assert transition(S.CONFIRMED, E.RESCHEDULE, Role.PATIENT) is S.RESCHEDULED


def test_doctor_can_complete() -> None:
    assert transition(S.CHECKED_IN, E.COMPLETE, Role.DOCTOR) is S.COMPLETED


@pytest.mark.parametrize("state", sorted(TERMINAL_STATUSES))
def test_terminal_states_have_no_outgoing(state: S) -> None:
    assert allowed_events(state) == []


def test_every_non_terminal_state_has_an_exit() -> None:
    for state in set(S) - TERMINAL_STATUSES:
        assert allowed_events(state), state
