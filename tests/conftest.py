import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

ADMIN_DSN = os.environ.get(
    "TEST_POSTGRES_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)


def _dsn_for(database: str) -> str:
    parts = urlsplit(ADMIN_DSN)
    return urlunsplit(parts._replace(path=f"/{database}"))


@pytest.fixture
def pg_db_factory():
    """Yields a callable that creates a fresh, uniquely-named Postgres database on each
    call and returns its connection string. All databases created during the test are
    dropped afterward. Use this directly (instead of `pg_url`) when a single test needs
    more than one independent database.
    """
    created = []

    def factory() -> str:
        name = f"test_{uuid.uuid4().hex}"
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
            admin_conn.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        return _dsn_for(name)

    yield factory

    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin_conn:
        for name in created:
            try:
                admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception as exc:
                print(f"warning: failed to drop test database {name!r}: {exc}")


@pytest.fixture
def pg_url(pg_db_factory):
    """A single fresh database's connection string — the common case of one database
    per test."""
    return pg_db_factory()
