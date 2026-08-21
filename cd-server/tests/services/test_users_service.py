import asyncio
import types

import jwt
import pytest
from jwt import PyJWKClientConnectionError

from cd.server import settings
from cd.server.services.users_service import (
    InvalidTokenError,
    UsersClient,
    UsersService,
    get_users_service,
)


class _FakeUsersClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.connected = False
        self.closed = False
        self._raise: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def upsert_user(self, id: str, email: str) -> None:
        if self._raise is not None:
            raise self._raise
        self.calls.append((id, email))


class _FakeJwkClient:
    def __init__(self, signing_key: str = "fake-signing-key"):
        self.signing_key = signing_key
        self.calls: list[str] = []

    def get_signing_key_from_jwt(self, token: str):
        self.calls.append(token)
        return types.SimpleNamespace(key=self.signing_key)


_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_examplepool"
_AUDIENCES = ["client-1", "client-2"]


def test_no_upsert_when_jwk_client_is_none():
    client = _FakeUsersClient()
    service = UsersService(client, jwk_client=None, issuer=_ISSUER, audiences=_AUDIENCES)

    asyncio.run(service.upsert_user_from_authorization_header("Bearer whatever"))

    assert client.calls == []


@pytest.mark.parametrize("header", [None, "", "Basic dXNlcjpwYXNz", "Bearer"])
def test_no_upsert_when_header_missing_or_malformed(header):
    client = _FakeUsersClient()
    jwk_client = _FakeJwkClient()
    service = UsersService(client, jwk_client, issuer=_ISSUER, audiences=_AUDIENCES)

    asyncio.run(service.upsert_user_from_authorization_header(header))

    assert client.calls == []
    assert jwk_client.calls == []


def test_valid_id_token_upserts_sub_and_email(monkeypatch):
    client = _FakeUsersClient()
    jwk_client = _FakeJwkClient()
    service = UsersService(client, jwk_client, issuer=_ISSUER, audiences=_AUDIENCES)

    def fake_decode(token, key, algorithms, issuer, audience, options):
        assert token == "a.b.c"
        assert key == "fake-signing-key"
        assert algorithms == ["RS256"]
        assert issuer == _ISSUER
        assert audience == _AUDIENCES
        return {"sub": "abc-123", "email": "person@example.com", "token_use": "id"}

    monkeypatch.setattr(jwt, "decode", fake_decode)

    asyncio.run(service.upsert_user_from_authorization_header("Bearer a.b.c"))

    assert jwk_client.calls == ["a.b.c"]
    assert client.calls == [("abc-123", "person@example.com")]


def test_invalid_token_raises_and_does_not_upsert(monkeypatch):
    client = _FakeUsersClient()
    jwk_client = _FakeJwkClient()
    service = UsersService(client, jwk_client, issuer=_ISSUER, audiences=_AUDIENCES)

    def fake_decode(*args, **kwargs):
        raise jwt.InvalidTokenError("bad signature")

    monkeypatch.setattr(jwt, "decode", fake_decode)

    with pytest.raises(InvalidTokenError, match="bad signature"):
        asyncio.run(service.upsert_user_from_authorization_header("Bearer a.b.c"))

    assert client.calls == []


def test_jwks_connection_failure_does_not_raise_or_upsert():
    client = _FakeUsersClient()

    class _UnreachableJwkClient(_FakeJwkClient):
        def get_signing_key_from_jwt(self, token: str):
            raise PyJWKClientConnectionError("could not reach jwks endpoint")

    service = UsersService(
        client, _UnreachableJwkClient(), issuer=_ISSUER, audiences=_AUDIENCES
    )

    # A JWKS-fetch hiccup is Cognito's own connectivity, not the token's
    # fault -- must degrade to anonymous like a missing header, not raise
    # InvalidTokenError/401 the way an actually-bad signature does.
    asyncio.run(service.upsert_user_from_authorization_header("Bearer a.b.c"))

    assert client.calls == []


def test_access_token_rejected_by_token_use(monkeypatch):
    client = _FakeUsersClient()
    jwk_client = _FakeJwkClient()
    service = UsersService(client, jwk_client, issuer=_ISSUER, audiences=_AUDIENCES)

    def fake_decode(*args, **kwargs):
        return {"sub": "abc-123", "email": "person@example.com", "token_use": "access"}

    monkeypatch.setattr(jwt, "decode", fake_decode)

    with pytest.raises(InvalidTokenError, match="token_use='access'"):
        asyncio.run(service.upsert_user_from_authorization_header("Bearer a.b.c"))

    assert client.calls == []


def test_db_failure_during_upsert_does_not_raise(monkeypatch):
    client = _FakeUsersClient()
    client._raise = RuntimeError("connection refused")
    jwk_client = _FakeJwkClient()
    service = UsersService(client, jwk_client, issuer=_ISSUER, audiences=_AUDIENCES)

    def fake_decode(*args, **kwargs):
        return {"sub": "abc-123", "email": "person@example.com", "token_use": "id"}

    monkeypatch.setattr(jwt, "decode", fake_decode)

    # Should not raise.
    asyncio.run(service.upsert_user_from_authorization_header("Bearer a.b.c"))


def test_upsert_user_runs_expected_sql():
    class _FakePool:
        def __init__(self):
            self.calls: list[tuple] = []

        async def execute(self, query, *args):
            self.calls.append((query, args))

    client = UsersClient("postgresql://ignored")
    client._pool = _FakePool()

    asyncio.run(client.upsert_user("abc-123", "person@example.com"))

    query, args = client._pool.calls[0]
    assert "INSERT INTO users" in query
    assert "ON CONFLICT (id) DO UPDATE" in query
    assert args == ("abc-123", "person@example.com")


def test_get_users_service_disables_verification_when_cognito_unset_and_local(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", "")
    monkeypatch.setattr(settings, "COGNITO_REGION", "")

    service = get_users_service()

    assert service._jwk_client is None


def test_get_users_service_raises_when_cognito_unset_and_not_local(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", "")
    monkeypatch.setattr(settings, "COGNITO_REGION", "")

    with pytest.raises(RuntimeError, match="COGNITO_USER_POOL_ID"):
        get_users_service()


def test_get_users_service_raises_when_client_ids_missing(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", "us-west-2_examplepool")
    monkeypatch.setattr(settings, "COGNITO_REGION", "us-west-2")
    monkeypatch.setattr(settings, "COGNITO_CLIENT_IDS", [])

    with pytest.raises(RuntimeError, match="COGNITO_CLIENT_IDS"):
        get_users_service()


def test_get_users_service_builds_expected_issuer_url(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "COGNITO_USER_POOL_ID", "us-west-2_examplepool")
    monkeypatch.setattr(settings, "COGNITO_REGION", "us-west-2")
    monkeypatch.setattr(settings, "COGNITO_CLIENT_IDS", ["client-1"])

    service = get_users_service()

    assert service._issuer == "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_examplepool"
    assert service._audiences == ["client-1"]
    assert service._jwk_client is not None
