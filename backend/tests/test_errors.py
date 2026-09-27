import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from app.config import Settings
from app.db import Repository
from app.main import create_app

UNREACHABLE_POSTGRES = "postgresql+psycopg://voice:hunter2-not-a-real-password@127.0.0.1:1/voice_agent"


def test_database_outage_is_a_503_with_no_details(caplog):
    settings = Settings(_env_file=None, database_url=UNREACHABLE_POSTGRES)

    with caplog.at_level(logging.WARNING, logger="voice_agent"):
        with TestClient(create_app(None, settings)) as client:
            response = client.get("/api/calls")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert "hunter2" not in response.text + caplog.text


def test_a_reachable_but_unmigrated_database_does_not_crash_startup(tmp_path, monkeypatch, caplog):
    """A production database can be reachable but not yet migrated (a Render deploy whose release command never
    ran `alembic upgrade head`, or a restart mid-upgrade): every table, including `call_jobs`, is missing.
    `resume_callbacks()` queries it unconditionally at startup. On PostgreSQL a missing table is psycopg's
    UndefinedTable, which SQLAlchemy raises as ProgrammingError - a different class from the OperationalError an
    outage raises above, and the only one the lifespan used to survive. Without also catching this one, the
    warning `repo.migrated()` logs a line earlier would be followed immediately by the process refusing to start.

    SQLite cannot reproduce this directly (its own "no such table" is an OperationalError too), so only that one
    call is stood in for; the database itself is a real, empty file with no tables and no alembic_version, exactly
    like a real database nobody has migrated yet."""
    repo = Repository(f"sqlite:///{tmp_path / 'unmigrated.db'}")

    def raises_like_postgres_would():
        raise ProgrammingError("SELECT call_jobs.id FROM call_jobs", {}, Exception('relation "call_jobs" does not exist'))

    monkeypatch.setattr(repo, "pending_callbacks", raises_like_postgres_would)
    settings = Settings(_env_file=None, database_url="sqlite://")  # unused: the real repo above is passed in directly

    with caplog.at_level(logging.WARNING, logger="voice_agent"):
        with TestClient(create_app(None, settings, repo=repo)) as client:
            response = client.get("/health")

    assert response.status_code == 200
    assert "has not been migrated" in caplog.text  # repo.migrated() correctly noticed
    assert "schema looks out of date" in caplog.text and "alembic upgrade head" in caplog.text


@pytest.mark.parametrize("debug, env", [(False, "development"), (True, "production")])
def test_unexpected_error_is_a_generic_500(debug, env, caplog):
    settings = Settings(_env_file=None, database_url="sqlite://", debug=debug, app_env=env)
    app = create_app(None, settings)

    @app.get("/boom/{call_id}")
    async def boom(call_id: str) -> None:
        raise RuntimeError("secret-internal-detail")

    with caplog.at_level(logging.ERROR, logger="voice_agent"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom/call-id-that-is-a-bearer-token")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret-internal-detail" not in response.text
    # The traceback is in the log, keyed by route template rather than the concrete path.
    assert "secret-internal-detail" in caplog.text
    assert "GET /boom/{call_id}" in caplog.text
    assert "call-id-that-is-a-bearer-token" not in caplog.text
