from __future__ import annotations

import pytest
from pydantic import SecretStr

from orchestrator.config import Settings
from orchestrator.db import build_engine


def test_sqlite_is_refused_in_prod(settings: Settings) -> None:
    settings.app_env = "prod"
    with pytest.raises(ValueError, match="Postgres in prod"):
        build_engine(settings)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("postgresql+psycopg://u:p@db/agency", False),
        ("postgresql+psycopg://u:p@db/agency?sslmode=prefer", False),
        ("postgresql+psycopg://u:p@db/agency?sslmode=require", True),
        ("postgresql+psycopg://u:p@db/agency?sslmode=verify-full", True),
    ],
)
async def test_prod_postgres_must_be_encrypted(settings: Settings, url: str, ok: bool) -> None:
    settings.app_env = "prod"
    settings.database_url = SecretStr(url)
    if ok:
        await build_engine(settings).dispose()  # engines connect lazily
    else:
        with pytest.raises(ValueError, match="sslmode"):
            build_engine(settings)


async def test_insecure_postgres_needs_an_explicit_opt_out(settings: Settings) -> None:
    settings.database_url = SecretStr("postgresql+psycopg://u:p@db/agency")
    await build_engine(settings).dispose()  # dev: allowed
    settings.app_env = "prod"
    settings.postgres_allow_insecure = True
    await build_engine(settings).dispose()
