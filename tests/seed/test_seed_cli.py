from collections.abc import Iterator
from pathlib import Path

import pytest

from netracare.config import get_settings
from netracare.seed.__main__ import main

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("SEED_DEFAULT_PASSWORD", "cli-test-pw")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_reset_seed_and_summary(isolated_db: None, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--root", str(ROOT), "--reset", "--today", "2026-11-10", "--weeks", "1", "--summary"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "seed complete (today=2026-11-10)" in out
    assert "doctors              5" in out
    assert "patients             30" in out

    # second run (no reset) inserts nothing
    assert main(["--root", str(ROOT), "--today", "2026-11-10", "--weeks", "1"]) == 0
    assert "nothing new" in capsys.readouterr().out
