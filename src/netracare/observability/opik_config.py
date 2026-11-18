"""Opik connection details shared by tracing and the eval runner."""

from __future__ import annotations

from functools import lru_cache

from netracare.config import Settings
from netracare.log import get_logger

log = get_logger(__name__)
COMET_BASE_URL = "https://www.comet.com/"


@lru_cache(maxsize=4)
def _default_workspace(api_key: str, base_url: str) -> str | None:
    """Ask Opik Cloud for the API key's default workspace (what `opik configure` does)."""
    try:
        import httpx
        from opik import url_helpers

        with httpx.Client(timeout=10) as client:
            r = client.get(
                url_helpers.get_account_details_url(base_url), headers={"Authorization": api_key}
            )
        if r.status_code != 200:
            log.warning("opik.workspace_lookup_failed", status=r.status_code)
            return None
        name = r.json().get("defaultWorkspaceName")
        return str(name) if name else None
    except Exception as exc:
        log.warning("opik.workspace_lookup_failed", error=type(exc).__name__)
        return None


def resolve_workspace(settings: Settings) -> str | None:
    if settings.opik_workspace:
        return settings.opik_workspace
    if settings.opik_url_override or settings.opik_api_key is None:
        return None  # self-hosted Opik has no workspaces
    return _default_workspace(settings.opik_api_key.get_secret_value(), COMET_BASE_URL)


def apply_opik_env(settings: Settings) -> bool:
    """Export Opik settings as the SDK's own env vars (the SDK reliably reads these; the
    `Opik(api_key=...)` argument alone is not enough on Opik Cloud). Returns False if no key."""
    import os

    if settings.opik_api_key is None or not settings.opik_api_key.get_secret_value():
        return False
    os.environ["OPIK_API_KEY"] = settings.opik_api_key.get_secret_value()
    os.environ["OPIK_PROJECT_NAME"] = settings.opik_project_name
    workspace = resolve_workspace(settings)
    if workspace:
        os.environ["OPIK_WORKSPACE"] = workspace
    if settings.opik_url_override:
        os.environ["OPIK_URL_OVERRIDE"] = settings.opik_url_override
    return True
