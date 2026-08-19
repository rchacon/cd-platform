import logging

import asyncpg
import jwt
from jwt import PyJWKClient, PyJWTError

from cd.server import settings

logger = logging.getLogger(__name__)


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
    specifically, a deliberately simple first pass. Never raises: a
    missing/invalid/expired token, or a database hiccup, must not break
    any of the 5 existing public resolvers, none of which require auth."""

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

    async def upsert_user_from_authorization_header(self, header: str | None) -> None:
        if self._jwk_client is None or not header:
            return
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return

        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
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
            return

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
            return

        try:
            await self._client.upsert_user(claims["sub"], claims["email"])
        except Exception:
            # A Postgres hiccup here must not take down an unrelated
            # getRepresentatives/getStates/etc. query -- log and move on,
            # same "don't block the request" principle as an invalid
            # token above.
            logger.exception("Failed to upsert user %s from JWT", claims["sub"])


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
