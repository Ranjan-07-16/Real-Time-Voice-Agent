"""Realtime LiveKit milestone: POST /api/livekit/token resolves and authorizes an Agent.

    browser -> POST /api/livekit/token?agent_id=X -> operator session (existing auth, unchanged) ->
    service.get_agent(agent_id, organization_id) (the same organization-scoped lookup
    GET /api/agents/{id} already uses - see tests/test_agents_api.py) -> agent brief -> the room's
    LiveKit metadata. An agent_id the browser supplies is never trusted on its own; only one the
    caller's own organization actually owns ever reaches a token.

LiveKit itself is never contacted: LIVEKIT_URL/API_KEY/API_SECRET are set to fixed fake test values
(monkeypatch only - never backend/.env), and the issued JWT is decoded locally with
livekit_api.TokenVerifier using that same fake secret - no network call, no real credential. The
worker-side half (parsing this same room metadata) is covered separately in
backend/livekit_agent/testing/test_realtime_agent.py.

load_livekit() also calls load_dotenv(backend/.env, override=False) on every call (see
livekit_agent/config.py) - harmless in production (a developer's local .env configuring their own
process), but a real cross-test leak here: it mutates the shared process os.environ for the rest of
the pytest session, with override=False only protecting the three LIVEKIT_* names this fixture
already sets. The fixture below patches load_dotenv() itself to a no-op, so this test file never
reads (or leaks the contents of) the real backend/.env into the shared pytest process environment."""

import json

import pytest
from livekit import api as livekit_api

from livekit_agent import config as livekit_config
from tests.test_agents_api import AGENT_PAYLOAD, OrgWorld

FAKE_LIVEKIT = {
    "LIVEKIT_URL": "wss://fake.livekit.test",
    "LIVEKIT_API_KEY": "fake-test-key",
    "LIVEKIT_API_SECRET": "fake-test-secret-at-least-32-bytes-long!!",
}


@pytest.fixture(autouse=True)
def fake_livekit_config(monkeypatch):
    for name, value in FAKE_LIVEKIT.items():
        monkeypatch.setenv(name, value)
    # load_livekit()'s default env_file (livekit_agent.config._ENV_FILE) is bound at function
    # definition time, so patching that name here would not reach the already-defined default -
    # patching the load_dotenv() call itself is what actually makes _load_env_file's call a no-op
    # (see its "load_dotenv is not None" guard), regardless of which env_file path it was given.
    monkeypatch.setattr(livekit_config, "load_dotenv", None)


def decode(token: str):
    return livekit_api.TokenVerifier(FAKE_LIVEKIT["LIVEKIT_API_KEY"], FAKE_LIVEKIT["LIVEKIT_API_SECRET"]).verify(token)


# --- B: unauthorized agent access is rejected -------------------------------------------------------


async def test_an_unauthenticated_caller_is_refused():
    world = OrgWorld()

    async with world.client() as client:
        assert (await client.post("/api/livekit/token")).status_code == 401


async def test_an_unknown_agent_id_is_rejected():
    world = OrgWorld()

    async with world.client() as client:
        await world.login(client)
        assert (await client.post("/api/livekit/token?agent_id=999999")).status_code == 404


async def test_an_agent_belonging_to_another_organization_is_rejected():
    world = OrgWorld()

    async with world.client() as client:
        await world.login(client)
        agent_id = (await client.post("/api/agents", json=AGENT_PAYLOAD)).json()["id"]

    async with world.client() as other_client:
        await world.login(other_client, email="other@example.com")
        response = await other_client.post(f"/api/livekit/token?agent_id={agent_id}")
        assert response.status_code == 404


# --- F: no agent selected keeps the existing default/test behavior -----------------------------------


async def test_no_agent_selected_keeps_the_existing_default_behavior():
    world = OrgWorld()

    async with world.client() as client:
        await world.login(client)
        response = await client.post("/api/livekit/token")
        assert response.status_code == 200

        body = response.json()
        assert body["agent_id"] is None and body["agent_name"] is None

        claims = decode(body["token"])
        assert claims.room_config is None or not claims.room_config.metadata


# --- A/D/E: a selected, owned agent's configuration reaches the token's room metadata -----------------


async def test_a_selected_agent_s_brief_reaches_the_room_metadata():
    world = OrgWorld()

    async with world.client() as client:
        await world.login(client)
        agent_id = (await client.post("/api/agents", json=AGENT_PAYLOAD)).json()["id"]

        response = await client.post(f"/api/livekit/token?agent_id={agent_id}")
        assert response.status_code == 200

        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["agent_name"] == AGENT_PAYLOAD["name"]

        metadata = json.loads(decode(body["token"]).room_config.metadata)
        assert metadata["agent_id"] == agent_id
        assert metadata["agent_name"] == AGENT_PAYLOAD["name"]
        assert AGENT_PAYLOAD["purpose"] in metadata["brief"]
        assert AGENT_PAYLOAD["name"] in metadata["brief"]


async def test_two_different_agents_produce_different_room_metadata():
    world = OrgWorld()

    async with world.client() as client:
        await world.login(client)
        first_id = (await client.post("/api/agents", json=AGENT_PAYLOAD)).json()["id"]
        second_payload = {**AGENT_PAYLOAD, "name": "Support Assistant", "purpose": "Answer product questions."}
        second_id = (await client.post("/api/agents", json=second_payload)).json()["id"]

        first_token = (await client.post(f"/api/livekit/token?agent_id={first_id}")).json()["token"]
        second_token = (await client.post(f"/api/livekit/token?agent_id={second_id}")).json()["token"]

        first_brief = json.loads(decode(first_token).room_config.metadata)["brief"]
        second_brief = json.loads(decode(second_token).room_config.metadata)["brief"]

        assert first_brief != second_brief
        assert "Collections Assistant" in first_brief and "Collections Assistant" not in second_brief
        assert "Support Assistant" in second_brief and "Support Assistant" not in first_brief


async def test_the_room_metadata_carries_only_the_expected_fields():
    """Keep it minimal: no raw agent row, no secrets, no fields beyond what the worker needs."""
    world = OrgWorld()

    async with world.client() as client:
        await world.login(client)
        agent_id = (await client.post("/api/agents", json=AGENT_PAYLOAD)).json()["id"]
        token = (await client.post(f"/api/livekit/token?agent_id={agent_id}")).json()["token"]

        metadata = json.loads(decode(token).room_config.metadata)
        assert set(metadata.keys()) == {"agent_id", "agent_name", "voice", "brief"}
