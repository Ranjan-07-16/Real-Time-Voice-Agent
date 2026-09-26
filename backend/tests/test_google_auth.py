"""POST /api/auth/google, with Google replaced.

`google.oauth2.id_token.verify_oauth2_token` is patched, so nothing here reaches Google or needs a real credential or
client id. What is under test is everything this server does around that call: the checks it adds (a verified email,
an existing and enabled local user), when a session is created, what each failure answers, and that the call to Google
runs off the event loop. That google-auth itself checks the signature, expiry, audience and issuer is its own contract;
the audience is passed to it (asserted below) and a failure of any of those checks reaches us as one of the exceptions
these tests raise.
"""

import inspect
import json
import logging
import threading
from unittest import mock

import httpx
import pytest
from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy import text

from app.config import Settings
from app.db import Repository
from app.main import GOOGLE_CERTS_TIMEOUT_SECONDS, GoogleRequest, create_app
from tests.helpers import ScriptedBrain

CLIENT_ID = "test-client.apps.googleusercontent.com"
EMAIL = "op@example.com"
PASSWORD = "correct horse battery"
CREDENTIAL = "credential-that-must-never-be-logged-or-echoed"


def claims(**overrides):
    return {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "email": EMAIL, "email_verified": True, **overrides}


def without(key):
    return {name: value for name, value in claims().items() if name != key}


class World:
    def __init__(self, **settings):
        self.repo = Repository("sqlite://")
        self.settings = Settings(
            **{"_env_file": None, "database_url": "sqlite://", "auth_required": True, "google_client_id": CLIENT_ID, **settings}
        )
        self.app = create_app(ScriptedBrain(), self.settings, repo=self.repo)
        self.user = self.app.state.auth.create_user(EMAIL, PASSWORD, "operator")

    def client(self):
        # raise_app_exceptions=False: an unhandled error is answered as a 500, as a real server would.
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    async def sign_in(self, client, credential=CREDENTIAL):
        return await client.post("/api/auth/google", json={"credential": credential})

    def sessions(self) -> int:
        with self.repo.engine.connect() as conn:
            return conn.execute(text("select count(*) from auth_sessions")).scalar_one()


@pytest.fixture
def verify(monkeypatch):
    """Stands in for google's verify_oauth2_token, and remembers how it was called."""
    fake = mock.Mock(return_value=claims())
    monkeypatch.setattr(id_token, "verify_oauth2_token", fake)
    return fake


def refused(response, status, detail):
    assert response.status_code == status and response.json() == {"detail": detail}
    assert "set-cookie" not in response.headers


# --- the good path -----------------------------------------------------------------------------------------------------


async def test_an_authorized_users_valid_credential_signs_them_in(verify):
    world = World()

    async with world.client() as client:
        response = await world.sign_in(client)
        cookie = response.headers["set-cookie"].lower()

        assert response.status_code == 200 and response.json()["user"]["email"] == EMAIL
        assert cookie.startswith("va_session=") and "httponly" in cookie and "samesite=lax" in cookie
        assert (await client.get("/api/auth/me")).json()["user"]["email"] == EMAIL

    assert world.sessions() == 1
    assert CREDENTIAL not in response.text + cookie


async def test_the_email_is_matched_whatever_its_capitalisation(verify):
    verify.return_value = claims(email="  Op@Example.COM ")
    world = World()

    async with world.client() as client:
        assert (await world.sign_in(client)).status_code == 200


async def test_the_cookie_is_marked_secure_when_configured(verify):
    world = World(cookie_secure=True)

    async with world.client() as client:
        assert "secure" in (await world.sign_in(client)).headers["set-cookie"].lower()


async def test_the_credential_is_checked_against_this_servers_client_id(verify):
    world = World()

    async with world.client() as client:
        await world.sign_in(client)

    credential, request, audience = verify.call_args.args
    assert (credential, audience) == (CREDENTIAL, CLIENT_ID)
    assert isinstance(request, GoogleRequest)


async def test_the_check_runs_off_the_event_loop(verify):
    seen = []
    verify.side_effect = lambda *args: seen.append(threading.get_ident()) or claims()
    world = World()

    async with world.client() as client:
        assert (await world.sign_in(client)).status_code == 200

    assert seen and seen != [threading.get_ident()], "verification blocked the thread the event loop runs on"


# --- not configured, and credentials Google refuses -----------------------------------------------------------------------


@pytest.mark.parametrize("client_id", [None, ""])
async def test_without_a_client_id_google_sign_in_is_off_and_google_is_never_asked(verify, client_id):
    world = World(google_client_id=client_id)

    async with world.client() as client:
        refused(await world.sign_in(client), 503, "Google sign-in is not configured")

    verify.assert_not_called()
    assert world.sessions() == 0


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Could not verify token signature."),  # forged, expired, or issued for another audience
        google_exceptions.MalformedError("Wrong number of segments in token"),
        google_exceptions.InvalidValue("Token expired"),
        google_exceptions.GoogleAuthError("Wrong issuer. 'iss' should be one of the following: ..."),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_a_credential_google_refuses_is_a_401_with_no_session_and_no_detail(verify, error):
    verify.side_effect = error
    world = World()

    async with world.client() as client:
        response = await world.sign_in(client)

    refused(response, 401, "Invalid Google credential")
    assert str(error) not in response.text
    assert world.sessions() == 0


# --- Google, or the road to it, is down ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        google_exceptions.TransportError("Could not fetch certificates at https://provider.example/certs"),
        json.JSONDecodeError("Expecting value", "<html>Service Unavailable</html>", 0),  # certificates came back unreadable
    ],
    ids=lambda error: type(error).__name__,
)
async def test_a_provider_failure_is_a_503_not_an_authentication_failure(verify, error, caplog):
    verify.side_effect = error
    world = World()

    with caplog.at_level(logging.INFO, logger="voice_agent"):
        async with world.client() as client:
            response = await world.sign_in(client)

    refused(response, 503, "Google sign-in is temporarily unavailable")
    assert "provider.example" not in response.text
    assert world.sessions() == 0
    assert "Google sign-in unavailable" in caplog.text
    assert CREDENTIAL not in caplog.text and "provider.example" not in caplog.text, "only the exception class is logged"


async def test_an_unexpected_error_is_not_dressed_up_as_an_authentication_failure(verify, caplog):
    verify.side_effect = RuntimeError("a bug in our own code")
    world = World()

    with caplog.at_level(logging.INFO, logger="voice_agent"):
        async with world.client() as client:
            response = await world.sign_in(client)

    refused(response, 500, "Internal server error")
    assert "a bug in our own code" not in response.text
    assert world.sessions() == 0
    assert CREDENTIAL not in caplog.text


async def test_a_refused_credential_is_logged_by_class_only(verify, caplog):
    verify.side_effect = ValueError(f"Token has wrong audience, expected {CLIENT_ID}: {CREDENTIAL}")
    world = World()

    with caplog.at_level(logging.INFO, logger="voice_agent"):
        async with world.client() as client:
            await world.sign_in(client)

    assert "Google sign-in refused: ValueError" in caplog.text
    assert CREDENTIAL not in caplog.text and CLIENT_ID not in caplog.text


# --- what this server requires on top of a valid credential --------------------------------------------------------------


@pytest.mark.parametrize(
    "token_claims",
    [
        claims(email_verified=False),
        claims(email_verified="true"),  # only the boolean True counts
        claims(email_verified=None),
        without("email_verified"),
        claims(email=""),
        without("email"),
    ],
    ids=["false", "string", "none", "missing", "blank-email", "no-email"],
)
async def test_an_unverified_or_absent_email_is_refused(verify, token_claims):
    verify.return_value = token_claims
    world = World()

    async with world.client() as client:
        refused(await world.sign_in(client), 401, "Google account email is not verified")

    assert world.sessions() == 0


async def test_a_google_account_with_no_local_user_is_refused_and_no_account_is_created(verify):
    verify.return_value = claims(email="stranger@example.com")
    world = World()

    async with world.client() as client:
        refused(await world.sign_in(client), 401, "Google account is not authorized")

    assert world.repo.get_user_by_email("stranger@example.com") is None
    assert world.sessions() == 0


async def test_a_disabled_user_cannot_sign_in_with_google(verify):
    world = World()
    world.repo.update_user(world.user["id"], disabled=1)

    async with world.client() as client:
        refused(await world.sign_in(client), 401, "Google account is not authorized")

    assert world.sessions() == 0


# --- abuse ------------------------------------------------------------------------------------------------------------------


async def test_google_sign_in_shares_the_login_rate_limit_and_stops_asking_google(verify):
    verify.side_effect = ValueError("nope")
    world = World(limit_login_per_ip=2)

    async with world.client() as client:
        codes = [(await world.sign_in(client)).status_code for _ in range(3)]
        limited = await world.sign_in(client)

    assert codes == [401, 401, 429] and int(limited.headers["retry-after"]) >= 1
    assert verify.call_count == 2, "a throttled request must not cost a call to Google"


# --- the transport: a bounded wait for Google's certificates -------------------------------------------------------------------


def test_fetching_googles_certificates_has_a_short_timeout_instead_of_googles_120_seconds(monkeypatch):
    seen = {}

    def parent(self, url, method="GET", body=None, headers=None, timeout=120, **kwargs):
        seen["timeout"] = timeout
        return "response"

    monkeypatch.setattr(google_requests.Request, "__call__", parent)

    assert inspect.signature(GoogleRequest.__call__).parameters["timeout"].default == GOOGLE_CERTS_TIMEOUT_SECONDS < 120
    assert GoogleRequest()("https://provider.example/certs") == "response"
    assert seen["timeout"] == GOOGLE_CERTS_TIMEOUT_SECONDS

    GoogleRequest()("https://provider.example/certs", timeout=3)
    assert seen["timeout"] == 3, "an explicit timeout is respected"
