"""Which features a caller can use right now.

v1 knows exactly one feature, "ai_summary" (the summarizeVotingRecord
mutation), gated by two daily caps read live off ai_summaries row counts
(cd-platform#169): a per-user free-tier limit and a global cost ceiling,
both on the UTC calendar day. There's no tier table -- "free" is the only
tier and its limits are settings constants; a real tier system belongs
with the future billing/API-key work.

EntitlementsService is pure logic -- it owns no connection pool, it
composes AiSummaryService's count methods with the configured limits. The
`features` GraphQL query maps FeatureStatus -> the Feature type for
cd-webapp's UI; the mutation calls require() to enforce the same gate
server-side (never trusting the client to have hidden the button).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cd.server.services.ai_summary_service import AiSummaryService

logger = logging.getLogger(__name__)

# The one feature slug this service knows today.
AI_SUMMARY = "ai_summary"

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


class EntitlementsService:
    def __init__(
        self,
        ai_summary_service: AiSummaryService,
        per_user_daily_limit: int,
        global_daily_limit: int,
    ):
        self._ai = ai_summary_service
        self._per_user = per_user_daily_limit
        self._global = global_daily_limit

    async def ai_summary_status(
        self, user_id: str, *, now: datetime | None = None
    ) -> FeatureStatus:
        start, reset = _utc_day_bounds(now)
        # Both counts even when globally gated -- the UI still wants
        # used_today to show "N of <limit> used".
        user_used = await self._ai.count_by_user_since(user_id, start)
        global_used = await self._ai.count_global_since(start)

        reason: str | None = None
        if self._global > 0 and global_used >= self._global:
            reason = REASON_GLOBAL
        elif self._per_user > 0 and user_used >= self._per_user:
            reason = REASON_DAILY_LIMIT

        return FeatureStatus(
            name=AI_SUMMARY,
            enabled=reason is None,
            reason=reason,
            daily_limit=self._per_user if self._per_user > 0 else None,
            used_today=user_used,
            resets_at=reset,
        )

    async def features(
        self, user_id: str, *, now: datetime | None = None
    ) -> list[FeatureStatus]:
        return [await self.ai_summary_status(user_id, now=now)]

    async def require(
        self, user_id: str, feature: str, *, now: datetime | None = None
    ) -> None:
        """Raise FeatureUnavailableError unless `feature` is currently
        available to `user_id`. Only AI_SUMMARY is gated today."""
        if feature != AI_SUMMARY:
            return
        status = await self.ai_summary_status(user_id, now=now)
        if status.enabled:
            return
        if status.reason == REASON_GLOBAL:
            # An operational "we're spending at the ceiling" signal --
            # worth seeing. A per-user cap hit is routine, stays silent
            # (and schema._Schema.process_errors suppresses the resulting
            # GraphQL error's ERROR log for both).
            logger.warning(
                "ai_summary globally gated for the rest of the UTC day "
                "(global daily limit %d reached)",
                self._global,
            )
        raise FeatureUnavailableError(feature, status.reason)
