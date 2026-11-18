"""UI tests drive the real Streamlit pages (AppTest) against the real seeded API.

The page code's `ApiClient` is given FastAPI's TestClient as its transport, so every UI
action goes through routing, auth, services and the DB exactly as in production.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from netracare.ui.api_client import ApiClient
from tests.fixtures_api import PASSWORD

APP = Path(__file__).resolve().parents[2] / "src" / "netracare" / "ui" / "app.py"


@pytest.fixture
def api_for(client: TestClient) -> Callable[[str | None], ApiClient]:
    def make(email: str | None) -> ApiClient:
        api = ApiClient("http://testserver", http=client, timeout=None)
        if email:
            api.login(email, PASSWORD)
        return api

    return make


@pytest.fixture
def ui(api_for: Callable[[str | None], ApiClient]) -> Callable[[str | None], AppTest]:
    def start(email: str | None) -> AppTest:
        at = AppTest.from_file(str(APP), default_timeout=30)
        at.session_state["api_client"] = api_for(email)
        return at.run()

    return start
