"""The conversation survives a restart: a new BookingAgent over the same SQLite file resumes."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, func, select

from netracare.agents.booking.agent import BookingAgent
from netracare.agents.booking.checkpoint import make_checkpointer
from netracare.agents.booking.state import AgentRuntime
from netracare.db.models import Appointment, User
from netracare.db.session import make_session_factory
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import Role
from tests.fixtures_api import NOW, make_settings, patient_email
from tests.helpers import MutableClock


def test_restart_between_readback_and_yes(
    api_engine: Engine, api_db_url: str, tmp_path: Path
) -> None:
    settings = make_settings(api_db_url).model_copy(
        update={"agent_checkpoint_path": str(tmp_path / "ck" / "agent.db")}
    )
    s = make_session_factory(api_engine)()
    u = s.scalar(select(User).where(User.email == patient_email("P010")))
    assert u is not None
    actor = CurrentUser(u.id, Role.PATIENT, u.email, u.email)
    clock = MutableClock(NOW)

    def say(agent: BookingAgent, text: str):  # type: ignore[no-untyped-def]
        t = agent.handle(AgentRuntime(s, clock, settings, actor, "chat-42"), text)
        s.commit()
        return t

    first = BookingAgent(make_checkpointer(settings))
    say(first, "book a check-up with Dr Kavya Nair")
    say(first, "next friday morning")
    assert say(first, "the second one").kind == "readback"
    first.checkpointer.conn.close()  # "restart": the process and its connection are gone

    second = BookingAgent(make_checkpointer(settings))
    assert second.is_active("chat-42") and second.state("chat-42")["step"] == "readback"
    before = s.scalar(select(func.count()).select_from(Appointment))
    done = say(second, "yes")
    assert done.kind == "confirmed"
    assert s.scalar(select(func.count()).select_from(Appointment)) == (before or 0) + 1

    second.purge(["chat-42"])
    assert second.state("chat-42") == {}
    s.close()
