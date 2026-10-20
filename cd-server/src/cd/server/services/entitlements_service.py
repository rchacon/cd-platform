"""Which features a caller can use right now.

Two features, each gated by two daily caps on the UTC calendar day: a
per-user free-tier limit and a global cost ceiling.
- "ai_summary" (the summarizeVotingRecord mutation, cd-platform#169)
  counts ai_summaries rows.
- "research_agent" (the agent mutations, cd-platform#164) counts the
  agent's answers in agent_turns. There's no tier table -- "free" is the only
tier and its limits are settings constants; a real tier system belongs
with the future billing/API-key work.

EntitlementsService is pure logic -- it owns no connection pool, it
composes each feature's count methods with the configured limits. The
`features` GraphQL query maps FeatureStatus -> the Feature type for
cd-webapp's UI; the mutation calls require() to enforce the same gate
server-side (never trusting the client to have hidden the button).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from cd.server.services.ai_summary_service import AiSummaryService

if TYPE_CHECKING:
    from cd.server.services.agent_service import AgentService

logger = logging.getLogger(__name__)

# The feature slugs, and the ai_summaries `kind` ai_summary maps to -- the caps count only rows of this kind, so a future
# summary kind (e.g. "bill_evolution", anticipated in migration 0002's
# docstring) doesn't consume this feature's allowance.
AI_SUMMARY = "ai_summary"
RESEARCH_AGENT = "research_agent"
_AI_SUMMARY_KIND = "voting_record"

# reason slugs on a disabled FeatureStatus / FeatureUnavailableError --
# the caller's own daily allowance is spent vs. the global ceiling is hit
# (nobody gets it until the next UTC day).
REASON_DAILY_LIMIT = "daily_limit_reached"
REASON_GLOBAL = "globally_unavailable"


@dataclass(frozen=True)
class FeatureStatus:
    """One feature's availability for one caller, right now."""

    name: str
    enabled: bool
    reason: str | None  # None when enabled; a REASON_* slug otherwise
    daily_limit: int | None  # the per-user cap, or None when that cap is disabled
    used_today: int  # this caller's generations so far this UTC day
    resets_at: datetime  # next UTC midnight -- when used_today rolls back to 0


class FeatureUnavailableError(Exception):
    """Raised by an entitlement-gated resolver when the caller can't use a
    feature right now -- their own daily limit, or the global ceiling.
    Thin GraphQL field error, same role as schema.NotAuthenticatedError;
    `reason` carries the slug so a client can tell the two apart."""

    def __init__(self, feature: str, reason: str):
        self.feature = feature
        self.reason = reason
        super().__init__(f"{feature} unavailable: {reason}")


def _utc_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start of this UTC day, start of the next). `now` is injectable for
    tests; defaults to the real current time."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


# The UTC date this process last WARN-logged each feature's global
# ceiling -- once the ceiling is hit, every subsequent gated call for the
# rest of the day would otherwise emit an identical line. In-process only
# (each task logs it once/day per feature, a handful of lines total),
# reset implicitly by the date rolling over. Module-level rather than
# per-instance so it still dedupes if the service is ever reconstructed.
_last_global_warning_dates: dict[str, date] = {}


@dataclass(frozen=True)
class _Gate:
    """One feature's two caps and how to count against them."""

    name: str
    count_user: Callable[[str, datetime], Awaitable[int]]
    count_global: Callable[[datetime], Awaitable[int]]
    per_user: int
    global_limit: int


class EntitlementsService:
    def __init__(
        self,
        ai_summary_service: AiSummaryService,
        per_user_daily_limit: int,
        global_daily_limit: int,
        *,
        agent_service: AgentService | None = None,
        agent_per_user_daily_limit: int = 0,
        agent_global_daily_limit: int = 0,
    ):
        self._ai = ai_summary_service
        # Scoped to the voting_record kind so another summary kind's rows
        # can't eat this feature's allowance.
        self._gates = {
            AI_SUMMARY: _Gate(
                AI_SUMMARY,
                lambda user_id, since: ai_summary_service.count_by_user_since(
                    user_id, since, _AI_SUMMARY_KIND
                ),
                lambda since: ai_summary_service.count_global_since(since, _AI_SUMMARY_KIND),
                per_user_daily_limit,
                global_daily_limit,
            )
        }
        if agent_service is not None:
            self._gates[RESEARCH_AGENT] = _Gate(
                RESEARCH_AGENT,
                agent_service.count_by_user_since,
                agent_service.count_global_since,
                agent_per_user_daily_limit,
                agent_global_daily_limit,
            )

    async def _status(
        self, gate: _Gate, user_id: str, now: datetime | None
    ) -> FeatureStatus:
        start, reset = _utc_day_bounds(now)
        # Both counts even when globally gated -- the UI still wants
        # used_today to show "N of <limit> used". The two have no data
        # dependency -> one round-trip of latency, not two (this runs on
        # every `features` query and every gated call).
        # return_exceptions=True so one failing count doesn't leave the
        # other running as an orphan whose exception is never retrieved --
        # same guard, same reason as generate_voting_record_summary()'s
        # own gather.
        user_used, global_used = await asyncio.gather(
            gate.count_user(user_id, start),
            gate.count_global(start),
            return_exceptions=True,
        )
        for result in (user_used, global_used):
            if isinstance(result, BaseException):
                raise result

        reason: str | None = None
        if gate.global_limit > 0 and global_used >= gate.global_limit:
            reason = REASON_GLOBAL
        elif gate.per_user > 0 and user_used >= gate.per_user:
            reason = REASON_DAILY_LIMIT

        return FeatureStatus(
            name=gate.name,
            enabled=reason is None,
            reason=reason,
            daily_limit=gate.per_user if gate.per_user > 0 else None,
            used_today=user_used,
            resets_at=reset,
        )

    async def ai_summary_status(
        self, user_id: str, *, now: datetime | None = None
    ) -> FeatureStatus:
        return await self._status(self._gates[AI_SUMMARY], user_id, now)

    async def features(
        self, user_id: str, *, now: datetime | None = None
    ) -> list[FeatureStatus]:
        return [await self._status(gate, user_id, now) for gate in self._gates.values()]

    async def require(
        self, user_id: str, feature: str, *, now: datetime | None = None
    ) -> None:
        """Raise FeatureUnavailableError unless `feature` is currently
        available to `user_id`. A feature with no gate is never blocked."""
        gate = self._gates.get(feature)
        if gate is None:
            return
        status = await self._status(gate, user_id, now)
        if status.enabled:
            return
        if status.reason == REASON_GLOBAL:
            # An operational "we're spending at the ceiling" signal --
            # worth seeing, but once per UTC day, not on every gated call
            # for the rest of the day. A per-user cap hit is routine,
            # stays silent (and schema._Schema.process_errors suppresses
            # the resulting GraphQL error's ERROR log for both).
            today = _utc_day_bounds(now)[0].date()
            if _last_global_warning_dates.get(feature) != today:
                _last_global_warning_dates[feature] = today
                logger.warning(
                    "%s globally gated for the rest of the UTC day "
                    "(global daily limit %d reached)",
                    feature,
                    gate.global_limit,
                )
        raise FeatureUnavailableError(feature, status.reason)
