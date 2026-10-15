import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, RetryAfter, TimedOut

import campaign_broadcast


CAMPAIGN = {"id": 17, "city_id": 3, "category_id": 2, "description": "عرض"}
TARGETS = [
    {"id": 1, "telegram_id": 101},
    {"id": 2, "telegram_id": 102},
    {"id": 3, "telegram_id": 103},
]


def _summary(**changes):
    result = {
        "total": 3,
        "pending": 0,
        "sending": 0,
        "sent": 3,
        "rejected": 0,
        "uncertain": 0,
    }
    result.update(changes)
    return result


def _db_mocks(monkeypatch, *, claims, pending, recipients, summaries, finishes):
    monkeypatch.setattr(
        campaign_broadcast.db,
        "claim_campaign_for_broadcast",
        AsyncMock(side_effect=claims),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_broadcast.db,
        "list_pending_campaign_recipients",
        AsyncMock(side_effect=pending),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_broadcast.db,
        "claim_campaign_recipient",
        AsyncMock(side_effect=recipients),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_broadcast.db,
        "record_campaign_recipient_result",
        AsyncMock(return_value=True),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_broadcast.db,
        "campaign_delivery_summary",
        AsyncMock(side_effect=summaries),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_broadcast.db,
        "finish_campaign_broadcast",
        AsyncMock(side_effect=finishes),
        raising=False,
    )
    monkeypatch.setattr(
        campaign_broadcast.db,
        "release_campaign_without_delivery",
        AsyncMock(),
        raising=False,
    )


def test_interrupted_broadcast_resumes_only_recipients_not_yet_attempted(monkeypatch):
    pending_after_restart = TARGETS[1:]
    _db_mocks(
        monkeypatch,
        claims=[CAMPAIGN, CAMPAIGN],
        pending=[TARGETS, pending_after_restart],
        recipients=[TARGETS[0], RuntimeError("simulated worker stop"), TARGETS[1], TARGETS[2]],
        summaries=[
            _summary(pending=3, sent=0),
            _summary(pending=2, sent=1),
            _summary(pending=2, sent=1),
            _summary(pending=0, sent=3),
        ],
        finishes=[{"status": "active"}],
    )
    sender = SimpleNamespace(send_message=AsyncMock())

    first = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 2, 0, resume=False))
    resumed = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 2, 0, resume=True))

    assert first["status"] == "interrupted"
    assert resumed["status"] == "completed"
    assert [call.args[0] for call in sender.send_message.await_args_list] == [101, 102, 103]
    assert campaign_broadcast.db.claim_campaign_for_broadcast.await_args_list[1].kwargs == {"resume": True}


def test_finish_failure_can_be_retried_without_resending_confirmed_message(monkeypatch):
    _db_mocks(
        monkeypatch,
        claims=[CAMPAIGN, CAMPAIGN],
        pending=[[TARGETS[0]], []],
        recipients=[TARGETS[0]],
        summaries=[_summary(total=1, pending=1, sent=0), _summary(total=1, pending=0, sent=1),
                   _summary(total=1, pending=0, sent=1), _summary(total=1, pending=0, sent=1)],
        finishes=[RuntimeError("database unavailable during finalization"), {"status": "active"}],
    )
    sender = SimpleNamespace(send_message=AsyncMock())

    first = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 10, 0))
    resumed = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 10, 0, resume=True))

    assert first["status"] == "finalization_failed"
    assert resumed["status"] == "completed"
    sender.send_message.assert_awaited_once()


def test_retry_after_waits_then_retries_the_confirmed_rejection(monkeypatch):
    _db_mocks(
        monkeypatch,
        claims=[CAMPAIGN],
        pending=[[TARGETS[0]]],
        recipients=[TARGETS[0]],
        summaries=[_summary(total=1, pending=1, sent=0), _summary(total=1, pending=0, sent=1)],
        finishes=[{"status": "active"}],
    )
    sender = SimpleNamespace(send_message=AsyncMock(side_effect=[RetryAfter(2), None]))
    with patch.object(campaign_broadcast.asyncio, "sleep", new=AsyncMock()) as sleep:
        result = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 10, 0))

    assert result["status"] == "completed"
    assert sender.send_message.await_count == 2
    sleep.assert_awaited_once_with(2)
    campaign_broadcast.db.record_campaign_recipient_result.assert_awaited_once()
    assert campaign_broadcast.db.record_campaign_recipient_result.await_args.args[2] == "sent"


def test_telegram_acceptance_with_lost_reply_is_uncertain_and_never_auto_retried(monkeypatch):
    uncertain = _summary(total=1, pending=0, sent=0, uncertain=1)
    _db_mocks(
        monkeypatch,
        claims=[CAMPAIGN, CAMPAIGN],
        pending=[[TARGETS[0]], []],
        recipients=[TARGETS[0]],
        summaries=[_summary(total=1, pending=1, sent=0), uncertain, uncertain, uncertain],
        finishes=[{"status": "broadcast_failed"}, {"status": "broadcast_failed"}],
    )
    sender = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut("Telegram accepted the message but the reply was lost")))

    first = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 10, 0))
    resumed = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 10, 0, resume=True))

    assert first["status"] == "failed"
    assert resumed["status"] == "failed"
    sender.send_message.assert_awaited_once()
    campaign_broadcast.db.record_campaign_recipient_result.assert_awaited_once()
    assert campaign_broadcast.db.record_campaign_recipient_result.await_args.args[2] == "uncertain"


def test_confirmed_forbidden_recipient_is_rejected_not_marked_uncertain(monkeypatch):
    rejected = _summary(total=1, pending=0, sent=0, rejected=1)
    _db_mocks(
        monkeypatch,
        claims=[CAMPAIGN],
        pending=[[TARGETS[0]]],
        recipients=[TARGETS[0]],
        summaries=[_summary(total=1, pending=1, sent=0), rejected],
        finishes=[{"status": "broadcast_failed"}],
    )
    sender = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("bot was blocked")))

    result = asyncio.run(campaign_broadcast.broadcast_campaign(sender, 17, 10, 0))

    assert result["status"] == "failed"
    campaign_broadcast.db.record_campaign_recipient_result.assert_awaited_once()
    assert campaign_broadcast.db.record_campaign_recipient_result.await_args.args[2] == "rejected"
