"""Bedrock Converse API -- chat-style generation via Anthropic Claude,
called by AiSummaryService to produce a stored summary.

Sibling to cd-lib's bedrock.py (Titan embeddings, shared by cd-api and
cd-etl) but deliberately NOT added there: this has exactly one consumer
today (cd-server) -- stays local until a second real consumer exists,
same "cd-lib is for code that's actually shared, not a dumping ground"
precedent as is_valid_district staying in cd-api rather than cd-lib.
Only cd.lib.bedrock.build_bedrock_client() (the generic bedrock-runtime
client constructor) is reused as-is.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cd.lib import bedrock

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
    request/response shape, nothing else."""

    def __init__(self, client: Any, model_id: str):
        self._client = client
        self.model_id = model_id

    async def converse(self, system_prompt: str, user_prompt: str) -> str:
        # boto3 has no async API at all -- same asyncio.to_thread()
        # treatment as LambdaApiClient.get()'s invoke() call in
        # cd_api_service.py.
        try:
            response = await asyncio.to_thread(
                self._client.converse,
                modelId=self.model_id,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                inferenceConfig={"maxTokens": _MAX_TOKENS, "temperature": _TEMPERATURE},
            )
        except (ClientError, BotoCoreError) as e:
            raise BedrockConverseError(f"Bedrock Converse call failed: {e}") from e

        # A generation that hit the maxTokens ceiling is cut off
        # mid-sentence -- AiSummaryService persists whatever comes back as
        # a finished summary, so a truncated one has to fail loudly here
        # rather than land in the DB looking complete. "end_turn"/
        # "stop_sequence" are the healthy stops; "max_tokens" is the one
        # that means "there was more to say" (raise _MAX_TOKENS, or move
        # to a model/prompt that fits, if this starts firing).
        if response.get("stopReason") == "max_tokens":
            raise BedrockConverseError(
                f"Bedrock Converse response truncated at maxTokens={_MAX_TOKENS} "
                "(stopReason=max_tokens) -- refusing to store a partial summary"
            )

        try:
            text = response["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as e:
            raise BedrockConverseError(f"Malformed Bedrock Converse response: {e}") from e

        # An all-whitespace/empty completion (a content filter firing, a
        # model returning nothing) is not a usable summary -- same "don't
        # persist it as success" reasoning as the truncation check above.
        if not text.strip():
            raise BedrockConverseError("Bedrock Converse returned an empty completion")
        return text


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
    return BedrockChatClient(_BEDROCK_CLIENT, settings.BEDROCK_CHAT_MODEL_ID)
