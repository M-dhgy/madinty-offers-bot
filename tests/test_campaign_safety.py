import asyncio
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import campaign_broadcast
import db


CAMPAIGN = {"id": 17, "city_id": 3, "category_id": 2, "description": "عرض تجريبي"}
TARGETS = [
    {"id": 1, "telegram_id": 101},
    {"id": 2, "telegram_id": 102},
    {"id": 3, "telegram_id": 103},
]


def test_broadcast_claim_is_required_before_any_send():
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch.object(campaign_broadcast.db, "claim_campaign_for_broadcast", new=AsyncMock(return_value=None)) as claim,
        patch.object(campaign_broadcast.db, "find_target_users", new=AsyncMock()) as find_targets,
    ):
        result = asyncio.run(campaign_broadcast.broadcast_campaign(bot, 17, 50, 0))

    assert result["status"] == "not_ready"
    claim.assert_awaited_once_with(17, resume=False)
    bot.send_message.assert_not_awaited()


def test_broadcast_batches_recipients_and_continues_after_delivery_error():
    async def send_message(chat_id, *_args, **_kwargs):
        if chat_id == 102:
            from telegram.error import Forbidden
            raise Forbidden("blocked recipient")

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
    with (
        patch.object(campaign_broadcast.db, "claim_campaign_for_broadcast", new=AsyncMock(return_value=CAMPAIGN)),
        patch.object(campaign_broadcast.db, "campaign_delivery_summary", new=AsyncMock(side_effect=[
            {"total": 3, "pending": 3, "sending": 0, "sent": 0, "rejected": 0, "uncertain": 0},
            {"total": 3, "pending": 0, "sending": 0, "sent": 2, "rejected": 1, "uncertain": 0},
        ])),
        patch.object(campaign_broadcast.db, "list_pending_campaign_recipients", new=AsyncMock(return_value=TARGETS)),
        patch.object(campaign_broadcast.db, "claim_campaign_recipient", new=AsyncMock(side_effect=TARGETS)),
        patch.object(campaign_broadcast.db, "record_campaign_recipient_result", new=AsyncMock(return_value=True)) as record,
        patch.object(campaign_broadcast.db, "finish_campaign_broadcast", new=AsyncMock(return_value={"status": "active"})) as finish,
        patch.object(campaign_broadcast.asyncio, "sleep", new=AsyncMock()) as sleep,
    ):
        result = asyncio.run(campaign_broadcast.broadcast_campaign(bot, 17, 2, 0.25))

    assert result == {"status": "completed", "sent": 2, "failed": 1, "total": 3, "pending": 0, "uncertain": 0}
    assert bot.send_message.await_count == 3
    assert [call.args[2] for call in record.await_args_list] == ["sent", "rejected", "sent"]
    finish.assert_awaited_once_with(17, succeeded=True)
    sleep.assert_awaited_once_with(0.25)


def test_all_failed_broadcast_is_not_marked_active_or_retryable():
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("unavailable")))
    with (
        patch.object(campaign_broadcast.db, "claim_campaign_for_broadcast", new=AsyncMock(return_value=CAMPAIGN)),
        patch.object(campaign_broadcast.db, "campaign_delivery_summary", new=AsyncMock(side_effect=[
            {"total": 1, "pending": 1, "sending": 0, "sent": 0, "rejected": 0, "uncertain": 0},
            {"total": 1, "pending": 0, "sending": 0, "sent": 0, "rejected": 0, "uncertain": 1},
        ])),
        patch.object(campaign_broadcast.db, "list_pending_campaign_recipients", new=AsyncMock(return_value=TARGETS[:1])),
        patch.object(campaign_broadcast.db, "claim_campaign_recipient", new=AsyncMock(return_value=TARGETS[0])),
        patch.object(campaign_broadcast.db, "record_campaign_recipient_result", new=AsyncMock(return_value=True)) as record,
        patch.object(campaign_broadcast.db, "finish_campaign_broadcast", new=AsyncMock(return_value={"status": "broadcast_failed"})) as finish,
    ):
        result = asyncio.run(campaign_broadcast.broadcast_campaign(bot, 17, 10, 0))

    assert result == {"status": "failed", "sent": 0, "failed": 1, "total": 1, "pending": 0, "uncertain": 1}
    record.assert_awaited_once()
    assert record.await_args.args[2] == "uncertain"
    finish.assert_awaited_once_with(17, succeeded=False)


def test_no_targets_releases_claim_without_sending():
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch.object(campaign_broadcast.db, "claim_campaign_for_broadcast", new=AsyncMock(return_value=CAMPAIGN)),
        patch.object(campaign_broadcast.db, "campaign_delivery_summary", new=AsyncMock(return_value={
            "total": 0, "pending": 0, "sending": 0, "sent": 0, "rejected": 0, "uncertain": 0
        })),
        patch.object(campaign_broadcast.db, "list_pending_campaign_recipients", new=AsyncMock(return_value=[])),
        patch.object(campaign_broadcast.db, "release_campaign_without_delivery", new=AsyncMock()) as release,
        patch.object(campaign_broadcast.db, "finish_campaign_broadcast", new=AsyncMock()) as finish,
    ):
        result = asyncio.run(campaign_broadcast.broadcast_campaign(bot, 17, 50, 1.5))

    assert result["status"] == "no_targets"
    assert result["total"] == 0
    release.assert_awaited_once_with(17)
    finish.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_invalid_batch_size_is_rejected_before_claim():
    bot = SimpleNamespace(send_message=AsyncMock())
    with patch.object(campaign_broadcast.db, "claim_campaign_for_broadcast", new=AsyncMock()) as claim:
        with pytest.raises(ValueError, match="batch_size"):
            asyncio.run(campaign_broadcast.broadcast_campaign(bot, 17, 0, 1.5))
    claim.assert_not_awaited()


def _fake_pool(**overrides):
    defaults = {
        "fetch": AsyncMock(return_value=[]),
        "fetchrow": AsyncMock(return_value=None),
        "fetchval": AsyncMock(return_value=False),
        "execute": AsyncMock(),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_target_lookup_rejects_missing_or_invalid_target_ids_without_querying():
    fake = _fake_pool()
    with patch.object(db, "pool", return_value=fake):
        assert asyncio.run(db.find_target_users(None, 2)) == []
        assert asyncio.run(db.find_target_users(3, None)) == []
        assert asyncio.run(db.find_target_users(0, 2)) == []
        assert asyncio.run(db.find_target_users(3, -1)) == []
    fake.fetch.assert_not_awaited()


def test_target_lookup_requires_both_active_city_and_category():
    fake = _fake_pool(fetch=AsyncMock(return_value=TARGETS))
    with patch.object(db, "pool", return_value=fake):
        rows = asyncio.run(db.find_target_users(3, 2))

    assert rows == TARGETS
    query, *params = fake.fetch.await_args.args
    assert "u.city_id=$1" in query
    assert "uc.category_id=$2" in query
    assert "ci.status='active'" in query
    assert "ca.status='active'" in query
    assert "u.account_type='customer'" in query and "u.status='active'" in query
    assert params == [3, 2]


def test_paid_flag_does_not_skip_merchant_approval():
    fake = _fake_pool(fetchrow=AsyncMock(return_value={"status": "pending"}))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.create_business(8, "نشاط", "متجر", 3, "09", is_paid=True))

    assert result["status"] == "pending"
    query, *params = fake.fetchrow.await_args.args
    assert "'pending'" in query
    assert params[-1] is True


def test_business_review_is_a_pending_only_transition():
    fake = _fake_pool(fetchrow=AsyncMock(return_value={"id": 5, "status": "approved"}))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.set_business_status(5, "approved"))
        with pytest.raises(ValueError, match="approved or rejected"):
            asyncio.run(db.set_business_status(5, "pending"))

    assert result["status"] == "approved"
    query, *params = fake.fetchrow.await_args.args
    assert "status='pending'" in query
    assert params == ["approved", 5]
    assert fake.fetchrow.await_count == 1


def test_campaign_creation_is_constrained_to_approved_business_and_active_matching_target():
    fake = _fake_pool(fetchrow=AsyncMock(return_value={"id": 4, "status": "pending_review"}))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.create_campaign(
            4, "raw", {"title": "t"}, "ad", 2, 3, submission_key="attempt-1"
        ))
    assert result["_created"] is True
    query, *params = fake.fetchrow.await_args_list[0].args
    assert "b.status='approved'" in query
    assert "b.city_id=ci.id" in query
    assert "ci.status='active'" in query
    assert "ca.status='active'" in query
    assert "ON CONFLICT (business_id, submission_key)" in query
    assert "DO NOTHING" in query
    assert params[0] == 4 and params[4:6] == [2, 3]
    assert params[-1] == "attempt-1"


def test_campaign_review_and_broadcast_claim_are_conditional_and_atomic():
    fake = _fake_pool(fetchrow=AsyncMock(return_value=None))
    with patch.object(db, "pool", return_value=fake):
        assert asyncio.run(db.review_campaign(17, "approve")) is None
        review_sql = fake.fetchrow.await_args_list[0].args[0]
        assert "c.status='pending_review'" in review_sql
        assert "b.status='approved'" in review_sql
        assert "ci.status='active'" in review_sql and "ca.status='active'" in review_sql

    class AsyncContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    approved = {
        "id": 17, "status": "approved", "city_id": 3, "category_id": 2,
        "broadcast_started_at": None,
    }
    claimed = {**approved, "status": "sending"}
    connection = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[approved, claimed]),
        fetchval=AsyncMock(return_value=False),
        execute=AsyncMock(),
        transaction=lambda: AsyncContext(),
    )

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *_args):
            return False

    with patch.object(db, "pool", return_value=SimpleNamespace(acquire=lambda: Acquire())):
        result = asyncio.run(db.claim_campaign_for_broadcast(17))

    assert result["status"] == "sending"
    select_sql = connection.fetchrow.await_args_list[0].args[0]
    claim_sql = connection.fetchrow.await_args_list[1].args[0]
    assert "FOR UPDATE" in select_sql
    assert "c.status=$2" in claim_sql
    assert "SET status='sending'" in claim_sql
    assert "b.status='approved'" in claim_sql
    assert "AT TIME ZONE 'UTC'" in claim_sql
    connection.execute.assert_awaited_once()


def test_target_validation_rejects_null_and_boolean_ids():
    fake = _fake_pool(fetchval=AsyncMock(return_value=True))
    with patch.object(db, "pool", return_value=fake):
        assert not asyncio.run(db.validate_campaign_target(4, None, 2))
        assert not asyncio.run(db.validate_campaign_target(4, True, 2))
        assert asyncio.run(db.validate_campaign_target(4, 3, 2))

    query, *params = fake.fetchval.await_args.args
    assert "b.status='approved'" in query and "b.city_id=ci.id" in query
    assert "ci.status='active'" in query and "ca.status='active'" in query
    assert params == [4, 3, 2]


def test_marketplace_uses_the_single_dedicated_conversation(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        import bot
        from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler

        application = bot.build_application()

    conversation = next(
        handler
        for group in application.handlers.values()
        for handler in group
        if isinstance(handler, ConversationHandler)
    )
    assert bot.marketplace.MARKET_MENU in conversation.states
    assert any(
        isinstance(handler, CallbackQueryHandler)
        and handler.pattern.match("customer_market")
        for handler in conversation.entry_points
    )
    assert any(
        isinstance(handler, CommandHandler) and handler.commands == {"editlisting"}
        for handler in conversation.entry_points
    )
    assert bot.marketplace.EDIT_LISTING_CITY in conversation.states
    assert bot.marketplace.EDIT_LISTING_MESSAGE in conversation.states
    assert bot.marketplace.EDIT_LISTING_CONFIRM in conversation.states
    assert not any(11 <= state <= 23 for state in conversation.states if isinstance(state, int))
    assert not hasattr(bot, "listing_start")
    assert conversation.per_message is False
    assert any("If 'per_message=False'" in str(item.message) for item in captured)


def test_merchant_launch_notice_makes_free_period_and_future_fees_clear(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    import bot

    notice = bot.MERCHANT_LAUNCH_PRICING_NOTICE
    assert "مجاني" in notice
    assert "محدودة" in notice
    assert "مسبقًا" in notice


def test_campaign_approval_callback_notifies_campaign_owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    import bot

    campaign = {"id": 17, "business_id": 4, "title": "عرض تجريبي"}
    query = SimpleNamespace(
        data="admin_approve_17",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    with (
        patch.object(bot, "is_admin", return_value=True),
        patch.object(bot.db, "review_campaign", new=AsyncMock(return_value=campaign)),
        patch.object(bot.db, "get_business", new=AsyncMock(return_value={"owner_telegram_id": 765})),
    ):
        asyncio.run(bot.admin_campaign_action(update, context))

    assert context.bot.send_message.await_args.args[0] == 765
    assert "تمت الموافقة" in context.bot.send_message.await_args.args[1]
    assert "#17" in context.bot.send_message.await_args.args[1]


def test_campaign_rejection_command_notifies_merchant_of_decision(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    import bot

    campaign = {"id": 17, "business_id": 4, "title": "عرض تجريبي"}
    update = SimpleNamespace(
        message=SimpleNamespace(text="/reject_17", reply_text=AsyncMock())
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with (
        patch.object(bot, "is_admin", return_value=True),
        patch.object(bot.db, "review_campaign", new=AsyncMock(return_value=campaign)),
        patch.object(bot, "notify_merchant_campaign_decision", new=AsyncMock()) as notify,
    ):
        asyncio.run(bot.cmd_approve_reject(update, context))

    notify.assert_awaited_once_with(context, campaign, "rejected")
