"""Serve a trailing-slash path the same as its slashless route.

Starlette's default for a trailing-slash miss (`/members/`) is a 307 to the
slashless URL, built from the request's own Host and scheme. Behind API
Gateway's HTTP_PROXY integration those are cd-server's
(`http://server.civicdog.com`, since the ALB terminates TLS and uvicorn
doesn't trust forwarded headers), so the redirect sent keyed callers of
`api.civicdog.com/v1/members/` to a host that 403s them without the
X-Origin-Verify header API Gateway adds. Rewriting the path in-process
answers the request directly instead, with no redirect to get wrong.

The mount root (`/v1/`) is left alone: it has no slashless route to map to
(see cd-infra's root integration).
"""

from __future__ import annotations

from starlette._utils import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send


class StripTrailingSlash:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            # get_route_path strips the /v1 mount prefix (root_path), the
            # same path Starlette's router matches against.
            route_path = get_route_path(scope)
            if route_path.endswith("/") and route_path.rstrip("/"):
                scope = dict(scope)
                scope["path"] = scope["path"].rstrip("/")
                if "raw_path" in scope and scope["raw_path"] is not None:
                    scope["raw_path"] = scope["raw_path"].rstrip(b"/")
        await self.app(scope, receive, send)
