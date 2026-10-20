"""AgentStoreClient against a real cd_customers database (make test-server
points PGDATABASE at cd_customers_test, migrated by the entrypoint).
Skips when Postgres isn't reachable."""
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from cd.server import settings
from cd.server.services.agent_store import AgentStoreClient, AssistantAnswer

pytestmark = pytest.mark.anyio

_ANSWER = AssistantAnswer(
    content="Sen. Example voted Yea on passage [119-s-5].",
    trace=[{"type": "tool", "name": "search_bills", "input": {"query": "guns"}}],
    model_id="test-model",
    stop_reason="answered",
    input_tokens=1200,
    output_tokens=80,
)


@pytest.fixture
async def store():
    client = AgentStoreClient(settings.PG_DSN)
    try:
        await client.connect()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"cd_customers database not reachable: {exc}")
    yield client
    await client.close()


async def _new_user(store: AgentStoreClient) -> str:
    user_id = f"test-{uuid.uuid4()}"
    await store._get_pool().execute(
        "INSERT INTO users (id, email) VALUES ($1, $2)", user_id, f"{user_id}@example.com"
    )
    return user_id


@pytest.fixture
async def users(store):
    created: list[str] = []

    async def make() -> str:
        user_id = await _new_user(store)
        created.append(user_id)
        return user_id

    yield make
    pool = store._get_pool()
    await pool.execute("DELETE FROM agent_conversations WHERE user_id = ANY($1)", created)
    await pool.execute("DELETE FROM users WHERE id = ANY($1)", created)


async def test_start_conversation_stores_the_question_and_the_answer(store, users):
    user_id = await users()

    conversation_id = await store.start_conversation(user_id, "How did they vote on guns?", _ANSWER)
    conversation = await store.get_conversation(user_id, conversation_id)

    assert conversation.title == "How did they vote on guns?"
    question, answer = conversation.turns
    assert (question.role, question.content, question.trace) == (
        "user", "How did they vote on guns?", []
    )
    assert question.model_id is None and question.input_tokens is None
    assert answer.role == "assistant"
    assert answer.content == _ANSWER.content
    assert answer.trace == _ANSWER.trace  # jsonb round-trips as a list
    assert (answer.model_id, answer.stop_reason) == ("test-model", "answered")
    assert (answer.input_tokens, answer.output_tokens) == (1200, 80)


async def test_title_collapses_whitespace_and_truncates_a_long_question(store, users):
    user_id = await users()

    conversation_id = await store.start_conversation(user_id, "why\n\n  " + "x" * 300, _ANSWER)
    title = (await store.get_conversation(user_id, conversation_id)).title

    assert title.startswith("why x")
    assert len(title) == 120 and title.endswith("…")


async def test_add_exchange_appends_turns_in_order_and_bumps_updated_at(store, users):
    user_id = await users()
    conversation_id = await store.start_conversation(user_id, "first", _ANSWER)
    before = (await store.get_conversation(user_id, conversation_id)).updated_at

    assert await store.add_exchange(user_id, conversation_id, "second", _ANSWER)
    conversation = await store.get_conversation(user_id, conversation_id)

    assert [(t.role, t.content) for t in conversation.turns] == [
        ("user", "first"), ("assistant", _ANSWER.content),
        ("user", "second"), ("assistant", _ANSWER.content),
    ]
    assert conversation.updated_at > before


async def test_another_users_conversation_is_invisible_and_unwritable(store, users):
    owner, other = await users(), await users()
    conversation_id = await store.start_conversation(owner, "mine", _ANSWER)

    assert await store.get_conversation(other, conversation_id) is None
    assert not await store.add_exchange(other, conversation_id, "sneaky", _ANSWER)
    assert len((await store.get_conversation(owner, conversation_id)).turns) == 2


async def test_unknown_conversation_is_none_and_unwritable(store, users):
    user_id = await users()

    assert await store.get_conversation(user_id, 2**62) is None
    assert not await store.add_exchange(user_id, 2**62, "hello", _ANSWER)


async def test_list_conversations_is_most_recent_activity_first_without_turns(store, users):
    user_id = await users()
    older = await store.start_conversation(user_id, "older", _ANSWER)
    newer = await store.start_conversation(user_id, "newer", _ANSWER)
    await store.add_exchange(user_id, older, "follow-up", _ANSWER)  # now the most recent

    conversations = await store.list_conversations(user_id, limit=10)

    assert [c.id for c in conversations] == [older, newer]
    assert all(c.turns == [] for c in conversations)
    assert [c.id for c in await store.list_conversations(user_id, limit=1)] == [older]


async def test_count_answers_since_counts_assistant_turns_per_user_and_globally(store, users):
    alice, bob = await users(), await users()
    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    global_before = await store.count_answers_since(since)

    conversation_id = await store.start_conversation(alice, "q1", _ANSWER)
    await store.add_exchange(alice, conversation_id, "q2", _ANSWER)
    await store.start_conversation(bob, "q3", _ANSWER)

    assert await store.count_answers_since(since, alice) == 2  # user turns not counted
    assert await store.count_answers_since(since, bob) == 1
    assert await store.count_answers_since(since) - global_before == 3
    assert await store.count_answers_since(datetime.now(timezone.utc) + timedelta(hours=1), alice) == 0
