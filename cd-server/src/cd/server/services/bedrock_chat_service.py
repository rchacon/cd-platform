"""Bedrock Converse API -- chat-style generation via Anthropic Claude,
called by AiSummaryService to produce a stored summary. Calls go
through langchain-aws's ChatBedrockConverse, the chat model the research
agent (cd-platform#164) is built on, rather than raw boto3 -- so both features share
one Bedrock code path, and a follow-up can stream the summary with
astream().

Sibling to cd-lib's bedrock.py (Titan embeddings, shared by cd-api and
cd-etl) but deliberately NOT added there: this has exactly one consumer
today (cd-server) -- stays local until a second real consumer exists,
same "cd-lib is for code that's actually shared, not a dumping ground"
precedent as is_valid_district staying in cd-api rather than cd-lib.
Only cd.lib.bedrock.build_bedrock_client() (the generic bedrock-runtime
client constructor) is reused as-is.
"""
from __future__ import annotations

import logging
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cd.lib import bedrock
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from cd.server import settings

logger = logging.getLogger(__name__)

# Claude completions run longer than Titan's embed() calls (cd-api's own
# GET /bills bounds its embedding call at a 5s read timeout, sized for a
# single short embedding, not a multi-paragraph generation) -- bounded
# here so a broken/slow path to Bedrock can't hang a worker indefinitely.
# Re-tune against real observed p99 latency once this has real traffic.
_BEDROCK_CLIENT = bedrock.build_bedrock_client(
    Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2, "mode": "standard"})
)

# Low temperature: this is meant to be a faithful, consistent restatement
# of structured input data (see AiSummaryService's system prompt), not
# creative writing -- consistency matters more than variety here.
_TEMPERATURE = 0.2
_MAX_TOKENS = 1024


class BedrockConverseError(Exception):
    """Raised on any Bedrock Converse failure -- a ClientError/
    BotoCoreError from the call itself, or a response missing the
    expected output.message.content[0].text shape. One consistent error
    type for AiSummaryService/schema.py to let propagate, same role as
    cd_api_service.ApiClientError."""


class BedrockChatClient:
    """Thin wrapper around one Bedrock Converse call -- no prompt
    composition, no storage, just "system+user text in, generated text
    out". Scoped like ApiClient/UsersClient: owns the external system's
    request/response shape, nothing else. ChatBedrockConverse does the
    request/response translation; this adds the checks a stored summary
    needs on top of it."""

    def __init__(self, model: ChatBedrockConverse):
        self._model = model
        self.model_id = model.model_id

    async def converse(self, system_prompt: str, user_prompt: str) -> str:
        # ainvoke still runs boto3's blocking converse() in a worker
        # thread (langchain-core's default for a model with no native
        # async API, which boto3 doesn't have) -- the same thing the
        # asyncio.to_thread() call here used to do.
        try:
            message = await self._model.ainvoke(
                [SystemMessage(system_prompt), HumanMessage(user_prompt)]
            )
        except (ClientError, BotoCoreError) as e:
            raise BedrockConverseError(f"Bedrock Converse call failed: {e}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            # ChatBedrockConverse's response parsing raises these for a
            # response missing output.message.content, or carrying a
            # content block it doesn't recognise.
            raise BedrockConverseError(f"Malformed Bedrock Converse response: {e}") from e

        # A generation that hit the maxTokens ceiling is cut off
        # mid-sentence -- AiSummaryService persists whatever comes back as
        # a finished summary, so a truncated one has to fail loudly here
        # rather than land in the DB looking complete. "end_turn"/
        # "stop_sequence" are the healthy stops; "max_tokens" is the one
        # that means "there was more to say" (raise _MAX_TOKENS, or move
        # to a model/prompt that fits, if this starts firing).
        if message.response_metadata.get("stopReason") == "max_tokens":
            raise BedrockConverseError(
                f"Bedrock Converse response truncated at maxTokens={_MAX_TOKENS} "
                "(stopReason=max_tokens) -- refusing to store a partial summary"
            )

        # An all-whitespace/empty completion (a content filter firing, a
        # model returning nothing) is not a usable summary -- same "don't
        # persist it as success" reasoning as the truncation check above.
        text = message.text
        if not text.strip():
            raise BedrockConverseError("Bedrock Converse returned an empty completion")
        return text


def build_chat_model(client: Any, model_id: str) -> ChatBedrockConverse:
    return ChatBedrockConverse(
        client=client,
        model_id=model_id,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS,
    )


def get_bedrock_chat_client() -> BedrockChatClient:
    if not settings.BEDROCK_CHAT_MODEL_ID:
        if settings.ENVIRONMENT != "local":
            # Fails at import time (schema.py's module-level singleton
            # construction, once a follow-up PR wires this up), same
            # "misconfigured non-local deploy fails immediately at
            # startup" precedent as get_cd_api_service()/get_users_service().
            raise RuntimeError(
                'BEDROCK_CHAT_MODEL_ID must be set when CD_SERVER_ENVIRONMENT '
                'is not "local".'
            )
        logger.warning(
            "BEDROCK_CHAT_MODEL_ID not set -- summarizeVotingRecord will fail "
            "if invoked. Fine for local dev not exercising this feature; set "
            "it (and configure real Bedrock access, e.g. the local-bedrock "
            "AWS profile) to test it locally."
        )
    return BedrockChatClient(build_chat_model(_BEDROCK_CLIENT, settings.BEDROCK_CHAT_MODEL_ID))
