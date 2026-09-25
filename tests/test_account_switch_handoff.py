"""Switching accounts without a second login: hand-off tickets between archives."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.web import dependencies as deps
from src.web import routes_account_switch as switch

SECRET = "s" * 64


def _claims(**overrides):
    base = {"username": "alaa", "role": "super_admin", "allowed_profile_ids": None, "account": "acc2"}
    return {**base, **overrides}


@pytest.fixture(autouse=True)
def _fresh_nonces():
    switch._used_nonces.clear()
    yield
    switch._used_nonces.clear()


class TestTicket:
    def test_valid_ticket_is_accepted_once(self):
        ticket = switch.mint_ticket(SECRET, _claims())
        assert switch.verify_ticket(SECRET, ticket, "acc2")["username"] == "alaa"
        assert switch.verify_ticket(SECRET, ticket, "acc2") is None  # single use

    def test_forged_signature_is_refused(self):
        ticket = switch.mint_ticket("other-secret", _claims())
        assert switch.verify_ticket(SECRET, ticket, "acc2") is None

    def test_tampered_body_is_refused(self):
        body, sig = switch.mint_ticket(SECRET, _claims(role="admin")).split(".")
        forged = switch._b64(json.dumps({**json.loads(switch._unb64(body)), "role": "super_admin"}).encode())
        assert switch.verify_ticket(SECRET, f"{forged}.{sig}", "acc2") is None

    def test_expired_ticket_is_refused(self):
        ticket = switch.mint_ticket(SECRET, _claims(), now=time.time() - 120)
        assert switch.verify_ticket(SECRET, ticket, "acc2") is None

    def test_ticket_for_another_account_is_refused(self):
        ticket = switch.mint_ticket(SECRET, _claims(account="main"))
        assert switch.verify_ticket(SECRET, ticket, "acc2") is None

    @pytest.mark.parametrize("role", ["viewer", "token", "nobody"])
    def test_roles_below_admin_are_refused(self, role):
        ticket = switch.mint_ticket(SECRET, _claims(role=role))
        assert switch.verify_ticket(SECRET, ticket, "acc2") is None

    @pytest.mark.parametrize("secret,key", [("", "acc2"), (SECRET, "")])
    def test_unconfigured_viewer_refuses_everything(self, secret, key):
        ticket = switch.mint_ticket(SECRET, _claims())
        assert switch.verify_ticket(secret, ticket, key) is None

    @pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", None, 42])
    def test_malformed_input_is_refused(self, garbage):
        assert switch.verify_ticket(SECRET, garbage, "acc2") is None

    def test_malformed_profile_scope_is_refused(self):
        ticket = switch.mint_ticket(SECRET, _claims(role="admin", allowed_profile_ids="team-beta"))
        assert switch.verify_ticket(SECRET, ticket, "acc2") is None


def test_profile_account_key():
    assert switch.profile_account_key({"url": "/?account=acc2"}) == "acc2"
    assert switch.profile_account_key({"url": ""}) is None
    assert switch.profile_account_key({}) is None


PROFILES = [
    {"id": "team-alpha", "url": "/?account=main"},
    {"id": "team-beta", "url": "/?account=acc2"},
]


class _Request:
    def __init__(self, body):
        self._body = body
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers = {}
        self.url = SimpleNamespace(scheme="https")

    async def json(self):
        return self._body


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(switch, "HANDOFF_SECRET", SECRET)
    db = SimpleNamespace(list_backup_profiles=AsyncMock(return_value=PROFILES), create_audit_log=AsyncMock())
    monkeypatch.setattr(deps, "db", db, raising=False)
    return db


def _user(role, profiles=None):
    return deps.UserContext(username="u", role=role, allowed_profile_ids=profiles)


class TestIssue:
    @pytest.mark.asyncio
    async def test_super_admin_gets_a_ticket_for_the_profiles_account(self, configured):
        result = await switch.issue_handoff(_Request({"profile_id": "team-beta"}), user=_user("super_admin"))
        assert result["account"] == "acc2"
        assert switch.verify_ticket(SECRET, result["ticket"], "acc2")["role"] == "super_admin"

    @pytest.mark.asyncio
    async def test_super_admin_may_name_the_account_where_no_profiles_are_stored(self, configured):
        configured.list_backup_profiles.return_value = []
        result = await switch.issue_handoff(
            _Request({"profile_id": "team-alpha", "account": "main"}), user=_user("super_admin")
        )
        assert result["account"] == "main"

    @pytest.mark.asyncio
    async def test_admin_needs_the_profile_assigned(self, configured):
        with pytest.raises(HTTPException) as exc:
            await switch.issue_handoff(_Request({"profile_id": "team-beta"}), user=_user("admin", ["team-alpha"]))
        assert exc.value.status_code == 403
        ok = await switch.issue_handoff(_Request({"profile_id": "team-beta"}), user=_user("admin", ["team-beta"]))
        claims = switch.verify_ticket(SECRET, ok["ticket"], "acc2")
        assert claims["role"] == "admin" and claims["allowed_profile_ids"] == ["team-beta"]

    @pytest.mark.asyncio
    async def test_admin_cannot_name_an_account_directly(self, configured):
        configured.list_backup_profiles.return_value = []
        with pytest.raises(HTTPException) as exc:
            await switch.issue_handoff(_Request({"profile_id": "x", "account": "acc2"}), user=_user("admin"))
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["viewer", "token"])
    async def test_viewers_and_share_tokens_cannot_switch(self, configured, role):
        with pytest.raises(HTTPException) as exc:
            await switch.issue_handoff(_Request({"profile_id": "team-beta"}), user=_user(role))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_disabled_without_a_secret(self, configured, monkeypatch):
        monkeypatch.setattr(switch, "HANDOFF_SECRET", "")
        with pytest.raises(HTTPException) as exc:
            await switch.issue_handoff(_Request({"profile_id": "team-beta"}), user=_user("super_admin"))
        assert exc.value.status_code == 403


class TestAccept:
    @pytest.mark.asyncio
    async def test_accepted_ticket_opens_a_session_with_exactly_its_claims(self, configured, monkeypatch):
        monkeypatch.setattr(switch, "ACCOUNT_KEY", "acc2")
        created = {}

        async def fake_create(username, role, allowed_chat_ids=None, **kwargs):
            created.update(username=username, role=role, chats=allowed_chat_ids, **kwargs)
            return "session-token"

        monkeypatch.setattr(switch, "_create_session", fake_create)
        ticket = switch.mint_ticket(SECRET, _claims(role="admin", allowed_profile_ids=["team-beta"]))

        response = await switch.accept_handoff(_Request({"ticket": ticket}))

        assert response.status_code == 200
        assert created == {"username": "alaa", "role": "admin", "chats": None, "allowed_profile_ids": ["team-beta"]}
        assert deps.AUTH_COOKIE_NAME in response.headers["set-cookie"]

    @pytest.mark.asyncio
    async def test_rejected_ticket_is_401(self, configured, monkeypatch):
        monkeypatch.setattr(switch, "ACCOUNT_KEY", "main")
        ticket = switch.mint_ticket(SECRET, _claims())  # addressed to acc2
        with pytest.raises(HTTPException) as exc:
            await switch.accept_handoff(_Request({"ticket": ticket}))
        assert exc.value.status_code == 401


class TestSessionPersistence:
    def test_admin_scope_survives_a_reload(self):
        row = {
            "username": "u",
            "role": "admin",
            "allowed_chat_ids": None,
            "allowed_profile_ids": '["team-beta"]',
            "no_download": 0,
            "source_token_id": None,
            "created_at": 1.0,
            "last_accessed": 1.0,
        }
        assert deps.session_from_row(row).allowed_profile_ids == ["team-beta"]

    def test_corrupt_scope_denies(self):
        row = {
            "username": "u",
            "role": "admin",
            "allowed_chat_ids": None,
            "allowed_profile_ids": "{not json",
            "no_download": 0,
            "source_token_id": None,
            "created_at": 1.0,
            "last_accessed": 1.0,
        }
        assert deps.session_from_row(row) is None
