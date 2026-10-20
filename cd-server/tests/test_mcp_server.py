import os
import sys

import psycopg2
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from cd.api.db import PG_DSN
from cd.server.mcp_server import build_server
from cd.server.services.agent_tools import TOOL_NAMES, AgentTools

pytestmark = pytest.mark.anyio


class _StubBillSearch:
    async def votes_by_bill(self, bioguide_id, bill_keys):
        return {}


def _server():
    return build_server(AgentTools(None, _StubBillSearch()))


async def test_every_agent_tool_is_listed_read_only_with_its_docstring():
    tools = {t.name: t for t in await _server().list_tools()}

    assert set(tools) == set(TOOL_NAMES)
    for name, tool in tools.items():
        assert tool.annotations.read_only_hint is True
        assert tool.description == " ".join(getattr(AgentTools, name).__doc__.split())
    # Parameter descriptions come from the methods' Annotated Field()s.
    bill_ids = tools["get_member_votes"].input_schema["properties"]["bill_ids"]
    assert bill_ids["minItems"] == 1 and "search_bills" in bill_ids["description"]


async def test_a_tool_call_returns_structured_content():
    result = await _server().call_tool(
        "get_member_votes", {"member_id": "P000197", "bill_ids": ["119-hr-1"]}
    )

    assert result.is_error is False
    assert result.structured_content == {
        "member_id": "P000197",
        "bills": [{"id": "119-hr-1", "votes": []}],
    }


async def test_invalid_arguments_are_rejected_before_the_tool_runs():
    with pytest.raises(ToolError, match="at least 1 item"):
        await _server().call_tool("get_member_votes", {"member_id": "P000197", "bill_ids": []})


@pytest.fixture
def congressional_db():
    try:
        psycopg2.connect(connect_timeout=3, **PG_DSN).close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"congressional_app database not reachable: {exc}")


async def test_the_stdio_server_answers_a_real_client(congressional_db):
    # End to end over stdio: a separate process, with cd.api in-process
    # against the test database. Also proves nothing but protocol
    # messages reaches stdout.
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "cd.server.mcp_server"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool("get_member", {"member_id": "Z999999"})

    assert {t.name for t in listed.tools} == set(TOOL_NAMES)
    assert result.is_error is False
    assert "404" in result.structured_content["error"]  # no such member in the test data
