"""
tests/conftest.py
----------------------------------------------------------------
Shared pytest fixtures for the Razorpay AI Revenue Recovery test suite.

All fixtures use an in-memory SQLite database — no production Postgres
connection is made at any point during the test run.

Fixtures
--------
engine   : SQLAlchemy engine backed by sqlite:///:memory:
session  : A fresh, isolated SQLAlchemy Session per test (rollback + close
           in teardown).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, event, String, TypeDecorator
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Compatibility shim — replace the PostgreSQL-specific UUID column type so
# that SQLAlchemy can create the same schema in SQLite (in-memory).
# We do this *before* importing db.models so the replacement is in effect
# when Base.metadata is built.
# ---------------------------------------------------------------------------

import sqlalchemy.dialects.postgresql as _pg


class _SQLiteUUID(TypeDecorator):
    """Stores UUIDs as VARCHAR(36) strings in SQLite."""

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


# Monkey-patch *only* for the test session so that mapped_column(UUID(...))
# resolves to our SQLite-compatible shim.
_pg.UUID = lambda as_uuid=True: _SQLiteUUID()  # type: ignore[assignment]

# Now import models (after the patch is in place)
from db.models import Base  # noqa: E402  (import after sys-level patch)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def engine():
    """Create a fresh in-memory SQLite engine and build all ORM tables.

    Yields:
        sqlalchemy.engine.Engine — an engine connected to ``sqlite:///:memory:``.
    """
    eng = create_engine("sqlite:///:memory:", echo=False, future=True)

    # SQLite does not enforce CHECK constraints by default; enable FK support
    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(scope="function")
def session(engine):
    """Yield a SQLAlchemy Session that is rolled back and closed after each test.

    Isolation strategy: each test gets a fresh transaction that is *never*
    committed to the engine — it is always rolled back in teardown, keeping
    tests completely isolated from one another.

    Yields:
        sqlalchemy.orm.Session — a per-test, auto-rolling-back session.
    """
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()
