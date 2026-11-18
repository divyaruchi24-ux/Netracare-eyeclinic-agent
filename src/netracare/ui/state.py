"""Per-session UI state: the API client and per-form idempotency keys."""

from __future__ import annotations

import os

import streamlit as st

from netracare.ui.api_client import ApiClient, new_idempotency_key

CLIENT_KEY = "api_client"


def api_base_url() -> str:
    return os.environ.get("API_BASE_URL", "http://localhost:8000")


def get_client() -> ApiClient:
    """The signed-in user's API client (one per browser session; tests inject their own)."""
    if CLIENT_KEY not in st.session_state:
        st.session_state[CLIENT_KEY] = ApiClient(api_base_url())
    client: ApiClient = st.session_state[CLIENT_KEY]
    return client


def form_key(name: str) -> str:
    """A stable Idempotency-Key for one form instance: double-clicks and retries reuse it."""
    slot = f"idem_{name}"
    if slot not in st.session_state:
        st.session_state[slot] = new_idempotency_key()
    key: str = st.session_state[slot]
    return key


def reset_form_key(name: str) -> None:
    st.session_state.pop(f"idem_{name}", None)
