# Research agent: design notes

The research agent (cd-platform#164) answers questions about the current
Congress, such as "how have my senators voted on gun bills?" or "compare
Rep. X and Rep. Y on SNAP". It looks things up with tools and cites the
member and bill ids behind every claim. This note records the design
choices that aren't obvious from the code.

Code:

- `src/cd/server/services/agent_tools.py`: the tools
- `src/cd/server/services/agent_service.py`: the graph and turn handling
- `src/cd/server/services/agent_store.py`: storage

## The loop is a LangGraph `StateGraph`, built by hand

```
agent ──(tool calls)──▶ tools ──▶ agent
  │  └─(step budget spent)──▶ step_limit ──▶ END
  └─(final answer)──▶ check_citations ──▶ END
                            │
                            └─(unknown citation, first time)──▶ agent
```

**Why LangGraph rather than a hand-written loop.** A plain
`while response.tool_calls:` loop over Bedrock Converse would be about 50
lines, and for the first version it would be enough. The loop stops being
simple once a turn needs more than "call tools until done":

- a verification step that can send the model back with a correction
- a step budget that ends the turn cleanly
- concurrent tool calls whose results all go back in the same turn

Each of these is a node or an edge in the graph. The graph is
declared in one place, and each node can be tested in isolation. LangChain's chat-model interface
also gives a standard way to bind and call tools, so swapping Bedrock for
another provider is a one-line model change.

**Why not `create_react_agent`.** The prebuilt agent is the same
model-and-tools loop with no hook for the citation check. Adding one means
either a post-processing step outside the graph, which can't send the
model back to fix its answer, or replacing the prebuilt agent anyway.
Building the graph by hand also keeps every routing rule visible. For
example, a citation retry is only allowed while there are steps left in
the budget.

**Why not ToolNode.** The `tools` node is about 30 lines of
`asyncio.gather` over the model's tool calls. Writing it directly lets
each tool call record its own latency in the trace. It also turns any
exception, from bad arguments or a bug in a tool, into an error result the
model can see and recover from, rather than an exception that fails the
turn.

## Grounding: citations are checked against tool results

The system prompt asks for every fact to be cited as `[S000033]` or
`[119-hr-2616]`. `check_citations` compares the cited ids with every `id`
that a tool returned during this turn, plus those returned in earlier turns
of the conversation.

- If an id isn't among them, the model is told which ones and gets one
  retry.
- If the retry still cites an unknown id, the answer is kept but stored
  with `stop_reason = 'citations_flagged'`, so the UI can show a warning
  and the evals can count it.

This catches the most common way a tool-using model hallucinates: citing a
plausible-looking bill that it never looked up. It doesn't prove that a
claim matches its source. That is what the faithfulness grading in the
evals is for.

## Evals

`make eval-agent` runs about 25 golden questions (`evals/cases.py`) against
real Bedrock and the dev `congressional_app` data. It writes a scorecard to
`evals/results/`. The cases cover:

- member lookup, including ambiguous and nonexistent names
- voting records, for one member and for a comparison of two
- what the data can't answer, such as Senate votes or bill text
- out-of-scope questions and prompt injection
- neutrality
- multi-turn follow-ups

Each answer is graded two ways:

- **Deterministically, from the trace.**
  - The turn finished (not flagged or step-limited).
  - The required tools were called and no forbidden ones were.
  - The pinned ids were cited. Only stable bioguide ids are pinned,
    because which bills match a topic changes as data syncs.
- **By an LLM judge**, with structured output:
  - whether every claim is supported by the tool results the judge is
    shown (faithfulness)
  - each case's own rubric, e.g. "says Senate votes aren't available" or
    "doesn't speculate about motives"

The judge defaults to the agent's own model, and `--judge-model` picks
another.

The evals call cd.api in-process through httpx's ASGI transport, so
nothing needs to be running except Postgres. They don't run in CI. CI's
database has only the schema and no synced data, and a run costs real
Bedrock calls. `tests/test_eval_grading.py` unit-tests the grading itself.

## Guardrails

- **Step budget.** At most 8 model calls per turn
  (`DEFAULT_MAX_STEPS`). A comparison of two members takes about five.
  When the budget runs out, the turn ends with a fixed message and
  `stop_reason = 'step_limit'`. LangGraph's `recursion_limit` is set above
  the budget as a backstop.
- **Scope.** The system prompt limits answers to Congress, its members,
  bills and votes. It also tells the model to describe what members did,
  not why, and to say when the data can't answer: only House votes are
  recorded so far.
- **Input size.** Questions are limited to 1,000 characters. Only the
  last 20 turns of a conversation are replayed, and CRS summaries are cut
  to 600 characters in tool results.
- **Cost.** Every assistant turn stores its summed token usage. Daily caps
  on answers, per user and global, go through the same
  `EntitlementsService` as `summarizeVotingRecord`'s.

## Storage: cd-server's own tables, not a LangGraph checkpointer

LangGraph's `AsyncPostgresSaver` can persist graph state between turns.
It isn't used here, for three reasons:

1. **A second Postgres driver.** It runs on psycopg 3, and cd-server's
   `cd_customers` code uses asyncpg throughout.
2. **Schema ownership.** It creates and migrates its own tables through
   `setup()`, outside Alembic, which manages every other table in this
   database.
3. **It stores the wrong thing for History.** A checkpoint is the full
   message state after each node, which is opaque to SQL. What History
   needs is the question, the answer and a readable trace of what the agent
   did.

Instead, `agent_turns` stores each question and answer, with the answer's
trace (every model and tool call, with inputs, outputs, latency and
tokens) as JSONB. A follow-up turn replays the earlier questions and
answers as plain messages. It doesn't replay their tool calls: the answers
already carry what was found, and replaying tool results would multiply
input tokens. The ids those tool calls returned are still passed along, so
they stay citable.

## Tools are plain async methods

`AgentTools` has no LangChain imports. `agent_service.build_tools()` wraps
its methods as LangChain `StructuredTool`s. The MCP server exposes the same
methods.

Each tool's model-facing description is its docstring, and each parameter
is described with `Annotated[..., Field(description=...)]`, which both
LangChain and MCP read. A tool's description is what the model decides
from, so the docstrings are written for the model: what the tool returns,
when to use it, and what its results don't cover.

Lookups that fail return `{"error": ...}` rather than raising, so the
model can try another approach. An example is an unknown id, or asking
for a senator's votes when only House votes exist.

Tools:

- **`find_members`**: by name across every state, or a state's
  delegation. This needs cd-api's `filter[name]` (#184).
- **`get_member`**: one member by id.
- **`search_bills`**: semantic topic search. Its results say whether each
  bill matched exactly (on policy area or subject) or only by similarity.
- **`get_member_votes`**: one member's roll-call votes on given bills.

Planned:

- Senate votes, after #8.
- `search_bill_text` passage search, after #131. It will return passage
  ids to cite.
