"""An MCP server exposing the research agent's tools (cd-platform#164),
so other MCP clients (Claude Desktop, Claude Code) can look up members,
bills and votes in cd-platform's data directly.

These are the same AgentTools methods the LangGraph agent calls, with the
same docstrings and parameter descriptions as their tool descriptions.
The tool layer is written once and works with either client. Every tool
is read-only.

    python -m cd.server.mcp_server     # stdio transport

cd.api runs in-process (AsgiApiClient), so this needs a reachable
congressional_app database and, for search_bills, AWS credentials for
Titan embeddings -- but no running cd-server. See the README's "MCP
server" section for client configuration. Everything is logged to
stderr: stdout carries the protocol.
"""
from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from cd.api import db as api_db
from cd.server.services.agent_tools import TOOL_NAMES, AgentTools
from cd.server.services.bill_search_service import BillSearchService
from cd.server.services.cd_api_service import AsgiApiClient, CdApiService

logger = logging.getLogger(__name__)

# The agent's own guidance, minus the parts that only make sense for its
# chat loop: the client brings its own model and conversation.
INSTRUCTIONS = """\
Tools for the current U.S. Congress: find members by name or state, look up
a member, search bills by topic, and get a member's roll-call votes on
specific bills. Results carry ids (bioguide ids such as S000033, bill ids
such as 119-hr-2616); cite them so answers can be checked.

Only House roll-call votes are recorded so far, and only bills that have
had a House vote are searchable. In search results, a 'summary' match is
semantic similarity, so check that the bill is really on topic. A
procedural vote, such as on a motion to recommit, is not a vote on the
bill itself.\
"""

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def build_server(tools: AgentTools, lifespan=None) -> MCPServer:
    server = MCPServer("cd-platform-congress", instructions=INSTRUCTIONS, lifespan=lifespan)
    for name in TOOL_NAMES:
        method = getattr(tools, name)
        server.add_tool(
            method,
            name=name,
            description=" ".join(method.__doc__.split()),
            annotations=_READ_ONLY,
        )
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    cd_api = CdApiService(AsgiApiClient())

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[None]:
        await api_db.connect()
        try:
            yield
        finally:
            await cd_api.aclose()
            await api_db.close()

    build_server(AgentTools(cd_api, BillSearchService(cd_api)), lifespan).run("stdio")


if __name__ == "__main__":
    main()
