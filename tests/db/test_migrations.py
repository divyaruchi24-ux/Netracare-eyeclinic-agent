import pytest
from alembic import command
from sqlalchemy import inspect

from netracare.db.base import Base
from netracare.db.session import make_engine
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def test_upgrade_creates_all_tables_and_downgrade_removes_them(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    engine = make_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables

    command.downgrade(cfg, "base")
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()
    engine.dispose()


def test_models_match_migration(db_url: str) -> None:
    """Autogenerate against the migrated DB must produce no diff (models == migrations)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    command.upgrade(alembic_config(db_url), "head")
    engine = make_engine(db_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []
