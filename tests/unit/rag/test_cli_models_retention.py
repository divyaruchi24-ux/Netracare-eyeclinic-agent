"""CLI entry points: model download and chat retention purge."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings


def test_models_download_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import netracare.rag.models as mod
    from netracare.rag.embeddings import HashingEmbedder, OverlapReranker

    monkeypatch.setattr(mod, "FastEmbedder", lambda model, cache: HashingEmbedder(dim=384))
    monkeypatch.setattr(mod, "FastReranker", lambda model, cache: OverlapReranker())
    assert mod.main(["download"]) == 0
    out = capsys.readouterr().out
    assert "models ready" in out and "dim 384" in out


def test_retention_cli(
    monkeypatch: pytest.MonkeyPatch,
    rag_factory: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import netracare.config as config
    from netracare.services import chat_retention

    url = str(rag_factory.kw["bind"].url)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(_env_file=None, database_url=url),  # type: ignore[call-arg]
    )
    assert chat_retention.main(["purge", "--dry-run"]) == 0
    assert "would delete 0 conversations older than 90 days" in capsys.readouterr().out
    assert chat_retention.main(["purge", "--days", "30"]) == 0
    assert "deleted 0 conversations older than 30 days" in capsys.readouterr().out
