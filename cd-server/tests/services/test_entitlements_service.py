import asyncio
import logging
from datetime import datetime, timezone

import pytest

from cd.server.services.entitlements_service import (
    AI_SUMMARY,
    REASON_DAILY_LIMIT,
    REASON_GLOBAL,
    EntitlementsService,
    FeatureUnavailableError,
    _utc_day_bounds,
)

# A fixed "now" so resets_at / the day window are deterministic.
_NOW = datetime(2026, 9, 6, 14, 30, tzinfo=timezone.utc)
_DAY_START = datetime(2026, 9, 6, tzinfo=timezone.utc)
_NEXT_DAY = datetime(2026, 9, 7, tzinfo=timezone.utc)


class _FakeAiSummaryService:
    def __init__(self, user_count=0, global_count=0):
        self._user_count = user_count
        self._global_count = global_count
        self.calls: list[tuple] = []

    async def count_by_user_since(self, user_id, since):
        self.calls.append(("user", user_id, since))
        return self._user_count

    async def count_global_since(self, since):
        self.calls.append(("global", since))
        return self._global_count


def _service(user_count=0, global_count=0, per_user=10, global_limit=100):
    return EntitlementsService(
        _FakeAiSummaryService(user_count=user_count, global_count=global_count),
        per_user_daily_limit=per_user,
        global_daily_limit=global_limit,
    )


def test_utc_day_bounds_are_this_utc_midnight_and_the_next():
    start, nxt = _utc_day_bounds(_NOW)
    assert start == _DAY_START
    assert nxt == _NEXT_DAY


def test_status_enabled_when_under_both_limits():
    status = asyncio.run(_service(user_count=3, global_count=40).ai_summary_status("u1", now=_NOW))

    assert status.name == AI_SUMMARY
    assert status.enabled is True
    assert status.reason is None
    assert status.daily_limit == 10
    assert status.used_today == 3  # the per-user count, not the global one
    assert status.resets_at == _NEXT_DAY


@pytest.mark.parametrize("user_count", [10, 11, 999])
def test_status_disabled_at_or_over_the_per_user_limit(user_count):
    status = asyncio.run(
        _service(user_count=user_count, global_count=0).ai_summary_status("u1", now=_NOW)
    )
    assert status.enabled is False
    assert status.reason == REASON_DAILY_LIMIT
    assert status.used_today == user_count


@pytest.mark.parametrize("global_count", [100, 101, 5000])
def test_status_disabled_at_or_over_the_global_limit(global_count):
    status = asyncio.run(
        _service(user_count=0, global_count=global_count).ai_summary_status("u1", now=_NOW)
    )
    assert status.enabled is False
    assert status.reason == REASON_GLOBAL


def test_global_limit_wins_over_a_per_user_hit():
    # Both caps exceeded -> the global reason is reported (it's the one
    # that affects everyone / is the real cost signal).
    status = asyncio.run(
        _service(user_count=50, global_count=200).ai_summary_status("u1", now=_NOW)
    )
    assert status.reason == REASON_GLOBAL


def test_a_non_positive_per_user_limit_disables_that_cap():
    status = asyncio.run(
        _service(user_count=10_000, global_count=0, per_user=0).ai_summary_status("u1", now=_NOW)
    )
    assert status.enabled is True
    assert status.daily_limit is None  # no cap -> no number to show
    assert status.used_today == 10_000


def test_a_non_positive_global_limit_disables_that_cap():
    status = asyncio.run(
        _service(user_count=0, global_count=10_000, global_limit=-1).ai_summary_status(
            "u1", now=_NOW
        )
    )
    assert status.enabled is True
    assert status.reason is None


def test_features_returns_the_ai_summary_status():
    features = asyncio.run(_service(user_count=1).features("u1", now=_NOW))
    assert [f.name for f in features] == [AI_SUMMARY]
    assert features[0].enabled is True


def test_require_is_a_noop_when_enabled():
    asyncio.run(_service(user_count=1, global_count=1).require("u1", AI_SUMMARY, now=_NOW))


def test_require_raises_with_the_per_user_reason():
    with pytest.raises(FeatureUnavailableError) as excinfo:
        asyncio.run(_service(user_count=10).require("u1", AI_SUMMARY, now=_NOW))
    assert excinfo.value.reason == REASON_DAILY_LIMIT
    assert excinfo.value.feature == AI_SUMMARY


def test_require_raises_with_the_global_reason_and_warn_logs(caplog):
    with caplog.at_level(logging.WARNING, logger="cd.server.services.entitlements_service"):
        with pytest.raises(FeatureUnavailableError) as excinfo:
            asyncio.run(_service(global_count=100).require("u1", AI_SUMMARY, now=_NOW))
    assert excinfo.value.reason == REASON_GLOBAL
    assert "globally gated" in caplog.text


def test_require_does_not_warn_on_a_per_user_hit(caplog):
    with caplog.at_level(logging.WARNING, logger="cd.server.services.entitlements_service"):
        with pytest.raises(FeatureUnavailableError):
            asyncio.run(_service(user_count=10).require("u1", AI_SUMMARY, now=_NOW))
    assert caplog.records == []


def test_require_ignores_an_unknown_feature():
    # Only ai_summary is gated today -- require() no-ops for anything else
    # rather than raising KeyError.
    asyncio.run(_service(user_count=10, global_count=100).require("u1", "some_other_feature"))
