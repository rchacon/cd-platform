import logging

import asyncpg
import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError, PyJWTError

from cd.server import settings

logger = logging.getLogger(__name__)


class InvalidTokenError(Exception):
    """Raised by upsert_user_from_authorization_header when a bearer token
    was supplied but fails to verify against Cognito -- a missing header
    stays a silent no-op (see that method's docstring), but a token that's
    actually present must be valid. app.py's context_getter catches this
    and turns it into an HTTP 401, rejecting the request before it ever
    reaches a resolver."""


class UsersClient:
    """Thin wrapper around the cd_customers connection pool -- owns the
    raw upsert SQL, no JWT/claims knowledge. Unlike HttpApiClient's
    connection pool (opened synchronously in __init__), asyncpg's
    create_pool() is a coroutine, so this can't fully connect at
    construction time the way HttpApiClient does -- connect()/close() are
    called explicitly from app.py's lifespan startup/shutdown instead,
    via UsersService's own connect()/aclose() below."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def upsert_user(self, id: str, email: str) -> None:
        assert self._pool is not None, "UsersClient.connect() was never called"
        await self._pool.execute(
            """
            INSERT INTO users (id, email, created_at, last_seen)
            VALUES ($1, $2, now(), now())
            ON CONFLICT (id) DO UPDATE
                SET email = EXCLUDED.email, last_seen = now()
            """,
            id,
            email,
        )


class UsersService:
    """What app.py's GraphQL context_getter actually depends on. Verifies
    a raw Authorization header against Cognito's JWKS and upserts the
    resulting user via UsersClient -- called unconditionally on every
    GraphQL request (see app.py), not throttled or gated to "new" users
    specifically, a deliberately simple first pass.
    upsert_user_from_authorization_header() returns the verified Cognito
    `sub` on success (so a resolver can gate on/tag content with it via
    GraphQL context), or None for a missing header, verification disabled
    entirely, or a JWKS-connectivity hiccup -- all silent no-ops, since
    most resolvers don't require auth. A database hiccup during the
    upsert itself is also a silent no-op, but still returns the sub: the
    token verified fine, so the caller IS authenticated even though their
    users row didn't get touched this time -- "authenticated" and "upsert
    succeeded" are separate concerns. But a bearer token that IS present
    must actually verify: raises InvalidTokenError otherwise, so app.py
    can reject the request outright rather than silently downgrading a
    bad token to an anonymous one."""

    def __init__(
        self,
        client: UsersClient,
        jwk_client: PyJWKClient | None,
        issuer: str,
        audiences: list[str],
    ):
        self._client = client
        # None means JWT verification is disabled entirely (no Cognito
        # config -- see get_users_service() below).
        self._jwk_client = jwk_client
        self._issuer = issuer
        self._audiences = audiences

    async def connect(self) -> None:
        await self._client.connect()

    async def aclose(self) -> None:
        await self._client.close()

    async def upsert_user_from_authorization_header(self, header: str | None) -> str | None:
        if self._jwk_client is None or not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            # Not a bearer-JWT attempt at all (missing/empty/other scheme)
            # -- nothing was "provided" in the sense that requires
            # validity, so this stays anonymous rather than rejected.
            return None

        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
        except PyJWKClientConnectionError as e:
            # A network/DNS hiccup reaching Cognito's own JWKS endpoint is
            # not the token's fault -- same "don't block the request over
            # an unrelated infra hiccup" precedent as the DB upsert
            # failure below, not the "reject an actually-bad token" path.
            logger.warning("Could not reach Cognito's JWKS endpoint: %s", e)
            return None
        except PyJWTError as e:
            logger.warning("Rejected invalid JWT on GraphQL request: %s", e)
            raise InvalidTokenError(str(e)) from e

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audiences,
                options={"require": ["sub", "email"]},
            )
        except PyJWTError as e:
            logger.warning("Rejected invalid JWT on GraphQL request: %s", e)
            raise InvalidTokenError(str(e)) from e

        # Cognito access tokens (as opposed to ID tokens) typically lack
        # aud/email entirely, so they'd usually already fail jwt.decode's
        # own audience check above -- this is an explicit, readable
        # second gate rather than relying on that as an implicit side
        # effect.
        if claims.get("token_use") != "id":
            logger.warning(
                "Rejected non-ID-token JWT (token_use=%r) on GraphQL request",
                claims.get("token_use"),
            )
            raise InvalidTokenError(
                f"expected an ID token, got token_use={claims.get('token_use')!r}"
            )

        try:
            await self._client.upsert_user(claims["sub"], claims["email"])
        except Exception:
            # A Postgres hiccup here must not take down an unrelated
            # getRepresentatives/getStates/etc. query -- log and move on,
            # same "don't block the request" principle as an invalid
            # token above. The token itself verified fine, so the caller
            # IS authenticated even though this particular upsert failed
            # -- still return the verified sub below rather than None;
            # "authenticated" and "upsert succeeded" are separate
            # concerns, and a caller of this method needs the former.
            logger.exception("Failed to upsert user %s from JWT", claims["sub"])

        return claims["sub"]


def get_users_service() -> UsersService:
    client = UsersClient(settings.PG_DSN)

    if not settings.COGNITO_USER_POOL_ID or not settings.COGNITO_REGION:
        if settings.ENVIRONMENT != "local":
            # Called at import time (schema.py's module-level
            # users_service = get_users_service()), so a misconfigured
            # non-local deploy fails immediately at startup -- same
            # "fail fast" precedent as get_cd_api_service().
            raise RuntimeError(
                "COGNITO_USER_POOL_ID and COGNITO_REGION must both be set "
                'when CD_SERVER_ENVIRONMENT is not "local".'
            )
        logger.warning(
            "COGNITO_USER_POOL_ID/COGNITO_REGION not set -- JWT "
            "verification disabled, no user will ever be upserted. Fine "
            "for local representative-lookup dev; set both (and "
            "COGNITO_CLIENT_IDS) to exercise the real auth path locally."
        )
        return UsersService(client, jwk_client=None, issuer="", audiences=[])

    if not settings.COGNITO_CLIENT_IDS:
        raise RuntimeError(
            "COGNITO_CLIENT_IDS must be set when COGNITO_USER_POOL_ID/"
            "COGNITO_REGION are set."
        )

    issuer = (
        f"https://cognito-idp.{settings.COGNITO_REGION}.amazonaws.com/"
        f"{settings.COGNITO_USER_POOL_ID}"
    )
    jwk_client = PyJWKClient(f"{issuer}/.well-known/jwks.json")
    return UsersService(client, jwk_client, issuer, settings.COGNITO_CLIENT_IDS)
