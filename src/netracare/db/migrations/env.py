"""Alembic environment. URL precedence: `-x url=...` > config option > Settings.database_url."""

from __future__ import annotations

from alembic import context

from netracare.config import get_settings
from netracare.db import models  # noqa: F401  (register models on metadata)
from netracare.db.base import Base
from netracare.db.session import make_engine

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    x_url = context.get_x_argument(as_dictionary=True).get("url")
    return x_url or config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    url = _url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
