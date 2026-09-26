"""What a production deployment leans on: CORS for a separate frontend origin, the API documentation switch, the
session cookie's attributes, the connection pool, the start-up warnings, and that .env.example stays complete.

Settings are built with `_env_file=None`, so a developer's own .env can never change what these tests see."""

import logging
import re
from pathlib import Path

import httpx
import pytest

from app.config import Settings, production_warnings
from app.db import Repository
from app.main import create_app
from tests.helpers import ScriptedBrain

FRONTEND = "https://app.example.com"
EMAIL = "op@example.com"
PASSWORD = "correct horse battery"
ENV_EXAMPLE = Path(__file__).parents[1] / ".env.example"


def settings(**overrides):
    return Settings(**{"_env_file": None, "database_url": "sqlite://", "auth_required": True, **overrides})


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def build(**overrides):
    return create_app(ScriptedBrain(), settings(**overrides), repo=Repository("sqlite://"))


# --- CORS ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
async def test_a_preflight_from_the_configured_origin_allows_every_method_the_api_serves(method):
    async with client_for(build(cors_origins=[FRONTEND])) as client:
        response = await client.options(
            "/api/agents/1",
            headers={"Origin": FRONTEND, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": "content-type"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND, "the origin is echoed, never a wildcard"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert method in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


async def test_a_method_the_api_does_not_serve_and_an_unlisted_origin_stay_refused():
    async with client_for(build(cors_origins=[FRONTEND])) as client:
        patch = await client.options(
            "/api/agents/1", headers={"Origin": FRONTEND, "Access-Control-Request-Method": "PATCH"}
        )
        stranger = await client.options(
            "/api/agents/1", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"}
        )

    assert patch.status_code == 400
    assert stranger.status_code == 400 and "access-control-allow-origin" not in stranger.headers


async def test_the_retry_after_header_is_readable_by_the_frontend_across_origins():
    async with client_for(build(cors_origins=[FRONTEND])) as client:
        response = await client.get("/api/health", headers={"Origin": FRONTEND})

    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert "retry-after" in response.headers["access-control-expose-headers"].lower()


async def test_local_development_origins_are_still_allowed_by_default():
    async with client_for(build()) as client:
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            response = await client.options(
                "/api/contacts/1", headers={"Origin": origin, "Access-Control-Request-Method": "DELETE"}
            )
            assert response.status_code == 200 and response.headers["access-control-allow-origin"] == origin


def test_the_production_origin_comes_from_the_environment_as_a_json_list(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", f'["{FRONTEND}"]')

    assert Settings(_env_file=None).cors_origins == [FRONTEND]
    assert "*" not in settings().cors_origins


# --- API documentation ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("app_env", ["development", "staging"])
async def test_the_api_documentation_is_available_outside_production(app_env):
    async with client_for(build(app_env=app_env)) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 200, path


@pytest.mark.parametrize("app_env", ["production", "Production", " PRODUCTION "])
async def test_the_api_documentation_is_off_in_production_and_the_api_still_works(app_env):
    async with client_for(build(app_env=app_env, debug=True)) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404, path

        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/api/health")).status_code == 200


def test_debug_is_ignored_in_production_however_it_is_written():
    assert build(app_env="Production", debug=True).debug is False
    assert build(app_env="development", debug=True).debug is True


# --- the session cookie ---------------------------------------------------------------------------------------------------------


def cookie_flags(header: str) -> set[str]:
    """The attributes that must match for a browser to replace or remove a cookie, and the ones that protect it."""
    return {part.strip().lower() for part in header.split(";")[1:] if not part.strip().lower().startswith(("max-age", "expires"))}


@pytest.mark.parametrize("secure", [False, True])
async def test_signing_out_clears_the_cookie_with_the_same_attributes_it_was_set_with(secure):
    app = build(cookie_secure=secure)
    app.state.auth.create_user(EMAIL, PASSWORD, "operator")

    async with client_for(app) as client:
        login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        logout = await client.post("/api/auth/logout")

    set_header, clear_header = login.headers["set-cookie"], logout.headers["set-cookie"]

    assert cookie_flags(set_header) == cookie_flags(clear_header)
    assert {"httponly", "path=/", "samesite=lax"} <= cookie_flags(set_header)
    assert ("secure" in cookie_flags(set_header)) is secure
    assert "max-age=43200" in set_header.lower(), "12 hours by default"
    assert "max-age=0" in clear_header.lower() and clear_header.startswith('va_session="";')


async def test_signup_sets_the_same_cookie_login_does():
    app = build(cookie_secure=True)
    app.state.auth.create_user(EMAIL, PASSWORD, "operator")

    async with client_for(app) as client:
        login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        signup = await client.post(
            "/api/auth/signup", json={"email": "new@example.com", "password": PASSWORD, "workspace_name": "Acme"}
        )

    assert signup.status_code == 201
    assert cookie_flags(signup.headers["set-cookie"]) == cookie_flags(login.headers["set-cookie"])
    assert "samesite=none" not in signup.headers["set-cookie"].lower(), "no cross-site cookie is offered"


# --- the database connection ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["sqlite://", "postgresql+psycopg://USER:PASSWORD@127.0.0.1:1/DBNAME"],  # constructing an engine never connects
    ids=["sqlite-memory", "postgresql"],
)
def test_pooled_connections_are_checked_before_use(url):
    assert Repository(url).engine.pool._pre_ping is True


def test_a_sqlite_file_still_works_for_local_development(tmp_path):
    repo = Repository(f"sqlite:///{tmp_path / 'dev.db'}")

    assert repo.engine.pool._pre_ping is True and repo.engine.dialect.name == "sqlite"


# --- start-up warnings -------------------------------------------------------------------------------------------------------


GOOD_PRODUCTION = {
    "app_env": "production",
    "database_url": "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME",
    "cookie_secure": True,
    "auth_required": True,
    "trusted_proxy_hops": 1,
    "public_base_url": FRONTEND,
}


def test_a_sound_production_configuration_draws_no_warning():
    assert production_warnings(settings(**GOOD_PRODUCTION)) == []


def test_development_is_never_warned_about():
    assert production_warnings(settings(app_env="development", cookie_secure=False, auth_required=False)) == []


@pytest.mark.parametrize(
    "change, names",
    [
        ({"cookie_secure": False}, ["COOKIE_SECURE"]),
        ({"auth_required": False}, ["AUTH_REQUIRED"]),
        ({"database_url": "sqlite:///./voice_agent.db"}, ["DATABASE_URL", "PostgreSQL"]),
        ({"database_url": "postgresql://USER:PASSWORD@HOST/DBNAME"}, ["DATABASE_URL", "postgresql+psycopg://"]),
        ({"database_url": "postgres://USER:PASSWORD@HOST/DBNAME"}, ["DATABASE_URL", "postgresql+psycopg://"]),
        ({"trusted_proxy_hops": 0}, ["TRUSTED_PROXY_HOPS"]),
        ({"allow_private_callbacks": True}, ["ALLOW_PRIVATE_CALLBACKS"]),
        ({"public_base_url": "http://localhost:5173"}, ["PUBLIC_BASE_URL"]),
    ],
)
def test_each_risky_production_setting_is_named_in_exactly_one_warning(change, names):
    warnings = production_warnings(settings(**{**GOOD_PRODUCTION, **change}))

    assert len(warnings) == 1 and all(name in warnings[0] for name in names)


def test_the_warnings_never_contain_the_database_password_or_url():
    password = "s3cret-database-password"
    url = f"postgres://user:{password}@db.internal.example:5432/prod"

    text = " ".join(production_warnings(settings(**{**GOOD_PRODUCTION, "database_url": url})))

    assert text and password not in text and "db.internal.example" not in text


def test_starting_in_production_logs_the_warnings_and_still_starts(caplog):
    with caplog.at_level(logging.WARNING, logger="voice_agent"):
        app = build(app_env="production")  # cookie_secure false, no proxy hops, a SQLite database: all warned about

    assert app.title
    assert "COOKIE_SECURE" in caplog.text and "TRUSTED_PROXY_HOPS" in caplog.text


def test_a_production_environment_of_placeholders_loads(monkeypatch):
    for name, value in {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME",
        "COOKIE_SECURE": "true",
        "AUTH_REQUIRED": "true",
        "SIGNUP_ENABLED": "false",
        "TRUSTED_PROXY_HOPS": "1",
        "GOOGLE_CLIENT_ID": "YOUR_VALUE_HERE.apps.googleusercontent.com",
        "PUBLIC_BASE_URL": FRONTEND,
        "CORS_ORIGINS": f'["{FRONTEND}"]',
    }.items():
        monkeypatch.setenv(name, value)

    loaded = Settings(_env_file=None)

    assert loaded.is_production and loaded.cookie_secure and loaded.trusted_proxy_hops == 1 and not loaded.signup_enabled
    assert production_warnings(loaded) == []


# --- .env.example ---------------------------------------------------------------------------------------------------------------


def documented_names(active_only: bool) -> dict[str, str]:
    """NAME -> value for the lines of .env.example (commented-out ones too, unless active_only)."""
    found = {}

    for line in ENV_EXAMPLE.read_text().splitlines():
        match = re.match(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$", line)

        if match and not (active_only and match.group(1)):
            found[match.group(2)] = match.group(3)

    return found


def test_every_setting_is_documented_in_env_example():
    names = documented_names(active_only=False)
    settings_names = {
        (field.validation_alias if isinstance(field.validation_alias, str) else name).upper()
        for name, field in Settings.model_fields.items()
    }

    assert settings_names - set(names) == set(), "a setting has no entry (active or commented) in backend/.env.example"


def test_env_example_holds_no_secret_and_loads_as_it_stands(monkeypatch):
    active = documented_names(active_only=True)

    for name, value in active.items():
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD", name):
            assert value == "", f"{name} must be empty in .env.example, not a value that looks real"

        monkeypatch.delenv(name, raising=False)  # so the file's own values are what is parsed

    loaded = Settings(_env_file=ENV_EXAMPLE)

    assert loaded.app_env == "development" and loaded.database_url.startswith("postgresql+psycopg://")
    assert loaded.google_client_id in (None, "") and not loaded.cookie_secure and loaded.auth_required
