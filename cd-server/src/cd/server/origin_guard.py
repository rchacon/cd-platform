"""Keeps /v1 reachable only through API Gateway.

API Gateway enforces cd-api's X-Api-Key and usage plan, then forwards to
this service's ALB -- which also answers on cd-server's own hostname. Without
this guard, `https://server.<domain>/v1/...` would serve the REST API with
no key at all. API Gateway's integration adds a static X-Origin-Verify header
(overwriting any client-sent value), and only requests carrying the right
value get through.

Fails closed: outside local dev, an unset CD_API_ORIGIN_SECRET rejects every
/v1 request rather than letting them all through -- so cd-server can ship
before cd-infra configures the secret without ever exposing /v1 keyless.
"""

from __future__ import annotations

import hmac
import logging

from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from cd.api import jsonapi
from cd.api.problem import problem_response

logger = logging.getLogger(__name__)

HEADER = b"x-origin-verify"


class OriginGuard:
    def __init__(self, app: ASGIApp, secret: str, environment: str) -> None:
        self.app = app
        self.secret = secret.encode()
        # Local dev (make start-server) has no API Gateway in front at all.
        self.enforce = bool(secret) or environment != "local"
        if not secret and environment != "local":
            logger.warning(
                "CD_API_ORIGIN_SECRET is unset outside local dev -- every /v1 "
                "request will be rejected with 403 until it's configured"
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only lifespan passes through unchecked. Websockets are guarded
        # too, even though cd.api serves none today, so adding one later
        # can't quietly expose it keyless on server.<domain>.
        if (
            scope["type"] not in ("http", "websocket")
            or not self.enforce
            or self._verified(scope)
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # 1008 = policy violation. Closing before accept makes the
            # server answer the handshake with a 403.
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        # Same error-shape split cd.api's own handlers make, so a JSON:API
        # client parsing errors[] on a non-2xx gets a clean 403, not a
        # problem+json body its contract says it never receives.
        if jsonapi.is_jsonapi_path(scope):
            response = jsonapi.error_response(403, "Forbidden.")
        else:
            response = problem_response(status=403, detail="Forbidden.")
        await response(scope, receive, send)

    def _verified(self, scope: Scope) -> bool:
        if not self.secret:
            return False
        for name, value in scope["headers"]:
            if name == HEADER:
                return hmac.compare_digest(value, self.secret)
        return False
