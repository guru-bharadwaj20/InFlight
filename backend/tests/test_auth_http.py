"""HTTP-level auth/authz coverage.

The rest of the CI suite (concurrency_sim, chaos_test, event_replay, ...)
exercises app/jobs.py and friends directly, bypassing HTTP entirely. Nothing
before this file touched the newest and most security-sensitive layer --
signup/login, token validation, and the ownership checks every conversation
route depends on -- over the wire, the way a real client actually calls it.

Requires USE_FAKE_LLM=true, a reachable Postgres (via DATABASE_URL) and Redis
(via REDIS_URL) with migrations applied, same as the rest of the backend CI
job. Run with: pytest backend/tests -q
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app


def _unique_email() -> str:
    return f"http-test-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _signup(client: AsyncClient, **overrides) -> dict:
    payload = {
        "name": "Test User",
        "email": _unique_email(),
        "password": "correct-horse-battery",
        **overrides,
    }
    response = await client.post("/auth/signup", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_signup_then_me_roundtrip(client: AsyncClient):
    signup = await _signup(client)
    token = signup["token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == signup["user"]["email"]


async def test_signup_duplicate_email_is_rejected(client: AsyncClient):
    email = _unique_email()
    first = await client.post(
        "/auth/signup",
        json={"name": "First", "email": email, "password": "correct-horse-battery"},
    )
    assert first.status_code == 201

    second = await client.post(
        "/auth/signup",
        json={"name": "Second", "email": email, "password": "another-password"},
    )
    assert second.status_code == 409


async def test_login_wrong_password_rejected_with_generic_message(client: AsyncClient):
    signup = await _signup(client)
    email = signup["user"]["email"]

    wrong = await client.post("/auth/login", json={"email": email, "password": "not-it"})
    assert wrong.status_code == 401

    nonexistent = await client.post(
        "/auth/login", json={"email": _unique_email(), "password": "not-it"}
    )
    assert nonexistent.status_code == 401
    # Same message for "no such account" and "wrong password" -- the response
    # must not leak which emails have accounts.
    assert wrong.json()["detail"] == nonexistent.json()["detail"]


async def test_login_roundtrip(client: AsyncClient):
    email = _unique_email()
    password = "correct-horse-battery"
    await client.post(
        "/auth/signup", json={"name": "Login Test", "email": email, "password": password}
    )

    login = await client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    assert login.json()["user"]["email"] == email


async def test_protected_routes_reject_missing_or_garbage_token(client: AsyncClient):
    no_auth = await client.get("/auth/me")
    assert no_auth.status_code == 401

    garbage = await client.get(
        "/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert garbage.status_code == 401

    no_auth_conv = await client.post("/conversations", json={"title": "nope"})
    assert no_auth_conv.status_code == 401


async def test_conversation_ownership_is_not_visible_across_users(client: AsyncClient):
    owner = await _signup(client)
    owner_headers = {"Authorization": f"Bearer {owner['token']}"}

    created = await client.post(
        "/conversations", json={"title": "owner's chat"}, headers=owner_headers
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    intruder = await _signup(client)
    intruder_headers = {"Authorization": f"Bearer {intruder['token']}"}

    # A 404, not a 403 -- the route must not confirm the conversation exists
    # to a user who doesn't own it.
    stolen_read = await client.get(
        f"/conversations/{conversation_id}", headers=intruder_headers
    )
    assert stolen_read.status_code == 404

    stolen_write = await client.patch(
        f"/conversations/{conversation_id}",
        json={"title": "hijacked"},
        headers=intruder_headers,
    )
    assert stolen_write.status_code == 404

    stolen_delete = await client.delete(
        f"/conversations/{conversation_id}", headers=intruder_headers
    )
    assert stolen_delete.status_code == 404

    # The rightful owner can still reach it.
    owner_read = await client.get(
        f"/conversations/{conversation_id}", headers=owner_headers
    )
    assert owner_read.status_code == 200


def test_websocket_requires_a_valid_single_use_ticket():
    # Sync, using the plain TestClient rather than the async fixture -- WS
    # tests over httpx's ASGITransport aren't supported, and this is the
    # standard way to exercise a websocket route without a real socket.
    with TestClient(app) as sync_client:
        signup = sync_client.post(
            "/auth/signup",
            json={
                "name": "WS Test",
                "email": _unique_email(),
                "password": "correct-horse-battery",
            },
        )
        assert signup.status_code == 201
        token = signup.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        created = sync_client.post(
            "/conversations", json={"title": "ws test"}, headers=auth
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]

        # No ticket at all.
        with pytest.raises(Exception):
            with sync_client.websocket_connect(f"/ws/conversations/{conversation_id}"):
                pass

        # Garbage ticket.
        with pytest.raises(Exception):
            with sync_client.websocket_connect(
                f"/ws/conversations/{conversation_id}?ticket=not-a-real-ticket"
            ):
                pass

        # The session token is no longer accepted in the URL. That is the whole
        # point of the change: a week-long credential must not travel anywhere
        # that gets logged.
        with pytest.raises(Exception):
            with sync_client.websocket_connect(
                f"/ws/conversations/{conversation_id}?ticket={token}"
            ):
                pass

        # Minting a ticket needs authentication.
        assert sync_client.post("/auth/ws-ticket").status_code == 401

        # A *different* user's ticket must not reach this conversation's stream.
        intruder = sync_client.post(
            "/auth/signup",
            json={
                "name": "WS Intruder",
                "email": _unique_email(),
                "password": "correct-horse-battery",
            },
        )
        intruder_ticket = sync_client.post(
            "/auth/ws-ticket",
            headers={"Authorization": f"Bearer {intruder.json()['token']}"},
        ).json()["ticket"]
        with pytest.raises(Exception):
            with sync_client.websocket_connect(
                f"/ws/conversations/{conversation_id}?ticket={intruder_ticket}"
            ):
                pass

        # The rightful owner connects fine.
        ticket = sync_client.post("/auth/ws-ticket", headers=auth).json()["ticket"]
        with sync_client.websocket_connect(
            f"/ws/conversations/{conversation_id}?ticket={ticket}"
        ):
            pass

        # And that ticket is spent -- replaying it is rejected, so a ticket
        # captured from a log or the back button cannot reopen the stream.
        with pytest.raises(Exception):
            with sync_client.websocket_connect(
                f"/ws/conversations/{conversation_id}?ticket={ticket}"
            ):
                pass
