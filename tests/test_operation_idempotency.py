import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import TimedOut

import bot
import marketplace


PREVIEW_ID = "012345abcdef"


def _listing_context():
    draft = {
        "preview_id": PREVIEW_ID,
        "title": "هاتف",
        "description": "تفاصيل",
        "price": "100",
        "condition": "جيد",
        "city_id": 3,
        "category": "عام",
        "address": "السوق",
        "contact": "0912345678",
        "delivery": "متاح",
        "negotiable": False,
        "photo_ids": ["photo"],
        "submission_key": "same-listing-operation",
    }
    return SimpleNamespace(
        user_data={
            "db_user_id": 17,
            "market_draft": draft,
            "market_preview_id": PREVIEW_ID,
        }
    )


def test_listing_commit_timeout_retry_and_duplicate_update_keep_one_key(monkeypatch):
    query = SimpleNamespace(
        data=f"market_{PREVIEW_ID}_submit",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=[TimedOut("reply lost"), None]),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    context = _listing_context()
    save = AsyncMock(side_effect=[{"id": 42}, {"id": 42}])
    monkeypatch.setattr(marketplace.db, "create_listing", save)

    with pytest.raises(TimedOut, match="reply lost"):
        asyncio.run(marketplace.sell_confirm(update, context))

    assert context.user_data["market_draft"]["submission_key"] == "same-listing-operation"
    assert asyncio.run(marketplace.sell_confirm(update, context)) == marketplace.MARKET_MENU
    # Same callback delivered again after confirmation is stale and cannot save again.
    assert asyncio.run(marketplace.sell_confirm(update, context)) == marketplace.SELL_CONFIRM

    assert save.await_count == 2
    assert [call.kwargs["submission_key"] for call in save.await_args_list] == [
        "same-listing-operation", "same-listing-operation"
    ]
    assert "market_draft" not in context.user_data
    assert query.edit_message_text.await_count == 2
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_campaign_commit_timeout_retry_notifies_admin_once(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    preview_id = "fedcba654321"
    query = SimpleNamespace(
        data=f"campaign_{preview_id}_submit",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=[TimedOut("reply lost"), None]),
        edit_message_reply_markup=AsyncMock(),
    )
    pending = {
        "preview_id": preview_id,
        "submission_key": "same-campaign-operation",
        "raw": "وصف العرض",
        "ai_data": {"category_code": "FOOD", "city_code": "KHARTOUM", "discount": 10},
        "ad_text": "عرض عربي",
    }
    context = SimpleNamespace(user_data={
        "db_user_id": 17,
        "pending_campaign": pending,
        "campaign_preview_id": preview_id,
    })
    update = SimpleNamespace(callback_query=query)
    fake_pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=[
        {"id": 2}, {"id": 3}, {"id": 2}, {"id": 3},
    ]))
    create = AsyncMock(side_effect=[
        {"id": 55, "_created": True},
        {"id": 55, "_created": False},
    ])
    notify = AsyncMock()

    with (
        patch.object(bot.db, "get_business_by_user", new=AsyncMock(return_value={"id": 9, "status": "approved"})),
        patch.object(bot.db, "pool", return_value=fake_pool),
        patch.object(bot.db, "validate_campaign_target", new=AsyncMock(return_value=True)),
        patch.object(bot.db, "create_campaign", new=create),
        patch.object(bot, "notify_admins_new_campaign", new=notify),
        patch.object(bot, "show_merchant_menu", new=AsyncMock(return_value=bot.MER_MENU)),
    ):
        with pytest.raises(TimedOut, match="reply lost"):
            asyncio.run(bot.campaign_confirm_router(update, context))
        assert context.user_data["pending_campaign"] is pending
        assert asyncio.run(bot.campaign_confirm_router(update, context)) == bot.MER_MENU
        # A replay of the already-completed Telegram callback must be rejected as stale.
        assert asyncio.run(bot.campaign_confirm_router(update, context)) == bot.MER_MENU

    assert create.await_count == 2
    assert [call.kwargs["submission_key"] for call in create.await_args_list] == [
        "same-campaign-operation", "same-campaign-operation"
    ]
    notify.assert_awaited_once_with(context, 55)
    assert "pending_campaign" not in context.user_data
    assert query.edit_message_text.await_count == 2
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_duplicate_telegram_update_does_not_duplicate_contact_side_effects():
    query = SimpleNamespace(
        data="listing_contact_42",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(
        update_id=880001,
        callback_query=query,
        effective_user=SimpleNamespace(id=7001),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    listing = {"id": 42, "user_id": 8, "title": "هاتف", "contact": "0912345678"}
    buyer = {"id": 17}
    owner = {"id": 8, "telegram_id": 8008, "username": "seller"}
    claim = AsyncMock(side_effect=[True, False])
    log_contact = AsyncMock()
    receipt = AsyncMock(return_value={"stage": "claimed"})
    set_stage = AsyncMock(return_value=True)
    complete = AsyncMock(return_value=True)

    with (
        patch.object(bot.db, "get_public_listing", new=AsyncMock(return_value=listing)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value=buyer)),
        patch.object(bot.db, "get_user_by_id", new=AsyncMock(return_value=owner)),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "get_telegram_update_receipt", new=receipt),
        patch.object(bot.db, "log_listing_contact_for_update", new=log_contact),
        patch.object(bot.db, "set_telegram_update_stage", new=set_stage),
        patch.object(bot.db, "complete_telegram_update", new=complete),
    ):
        asyncio.run(bot.listing_contact_callback(update, context))
        asyncio.run(bot.listing_contact_callback(update, context))

    assert claim.await_args_list[0].args == (880001, "listing_contact")
    assert claim.await_args_list[1].args == (880001, "listing_contact")
    log_contact.assert_awaited_once_with(42, 17, 880001)
    context.bot.send_message.assert_awaited_once()
    assert [call.args[2] for call in set_stage.await_args_list] == [
        "notification_attempting", "notification_sent"
    ]
    complete.assert_awaited_once_with(880001, "listing_contact")
    assert query.message.reply_text.await_count == 2
    assert "مسبقًا" in query.message.reply_text.await_args_list[1].args[0]


def test_expired_public_listing_is_rejected_before_contact_side_effect():
    query = SimpleNamespace(
        data="listing_contact_42",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(
        update_id=880002,
        callback_query=query,
        effective_user=SimpleNamespace(id=7001),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with (
        patch.object(bot.db, "get_public_listing", new=AsyncMock(return_value=None)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "claim_telegram_update", new=AsyncMock()) as claim,
        patch.object(bot.db, "log_listing_event", new=AsyncMock()) as log_contact,
    ):
        asyncio.run(bot.listing_contact_callback(update, context))

    claim.assert_not_awaited()
    log_contact.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    assert "غير متاح" in query.message.reply_text.await_args.args[0]


def _contact_update(update_id):
    query = SimpleNamespace(
        data="listing_contact_42",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(
        update_id=update_id,
        callback_query=query,
        effective_user=SimpleNamespace(id=7001),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    return update, context, query


def test_contact_update_retries_after_failure_before_event_stage():
    update, context, _query = _contact_update(880003)
    listing = {"id": 42, "user_id": 8, "title": "هاتف", "contact": "0912345678"}
    owner = {"id": 8, "telegram_id": 8008, "username": "seller"}
    owner_lookup = AsyncMock(side_effect=[RuntimeError("simulated DB failure"), owner])
    claim = AsyncMock(side_effect=[True, True])
    release = AsyncMock(return_value=True)
    event = AsyncMock(return_value=True)

    with (
        patch.object(bot.db, "get_public_listing", new=AsyncMock(return_value=listing)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "get_telegram_update_receipt", new=AsyncMock(return_value={"stage": "claimed"})),
        patch.object(bot.db, "get_user_by_id", new=owner_lookup),
        patch.object(bot.db, "release_telegram_update", new=release),
        patch.object(bot.db, "log_listing_contact_for_update", new=event),
        patch.object(bot.db, "set_telegram_update_stage", new=AsyncMock(return_value=True)),
        patch.object(bot.db, "complete_telegram_update", new=AsyncMock(return_value=True)),
    ):
        with pytest.raises(RuntimeError, match="simulated DB failure"):
            asyncio.run(bot.listing_contact_callback(update, context))
        asyncio.run(bot.listing_contact_callback(update, context))

    release.assert_awaited_once_with(880003, "listing_contact")
    event.assert_awaited_once_with(42, 17, 880003)
    context.bot.send_message.assert_awaited_once()


def test_lost_owner_notification_reply_marks_update_uncertain_and_skips_redelivery():
    update, context, query = _contact_update(880004)
    listing = {"id": 42, "user_id": 8, "title": "هاتف", "contact": "0912345678"}
    owner = {"id": 8, "telegram_id": 8008, "username": "seller"}
    claim = AsyncMock(side_effect=[True, False])
    mark_uncertain = AsyncMock(return_value=True)
    event = AsyncMock(return_value=True)

    with (
        patch.object(bot.db, "get_public_listing", new=AsyncMock(return_value=listing)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "get_telegram_update_receipt", new=AsyncMock(return_value={"stage": "claimed"})),
        patch.object(bot.db, "get_user_by_id", new=AsyncMock(return_value=owner)),
        patch.object(bot.db, "log_listing_contact_for_update", new=event),
        patch.object(bot.db, "set_telegram_update_stage", new=AsyncMock(return_value=True)),
        patch.object(bot.db, "mark_telegram_update_uncertain", new=mark_uncertain),
    ):
        context.bot.send_message.side_effect = TimedOut("notification may have been delivered")
        asyncio.run(bot.listing_contact_callback(update, context))
        asyncio.run(bot.listing_contact_callback(update, context))

    mark_uncertain.assert_awaited_once_with(880004, "listing_contact")
    event.assert_awaited_once_with(42, 17, 880004)
    context.bot.send_message.assert_awaited_once()
    assert "مسبقًا" in query.message.reply_text.await_args_list[1].args[0]


def test_contact_update_resumes_after_event_was_logged_but_notice_stage_failed():
    update, context, _query = _contact_update(880005)
    listing = {"id": 42, "user_id": 8, "title": "هاتف", "contact": "0912345678"}
    owner = {"id": 8, "telegram_id": 8008, "username": "seller"}
    claim = AsyncMock(side_effect=[True, True])
    event = AsyncMock(side_effect=[True, False])
    set_stage = AsyncMock(side_effect=[False, True, True])
    release = AsyncMock(return_value=True)

    with (
        patch.object(bot.db, "get_public_listing", new=AsyncMock(return_value=listing)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "get_telegram_update_receipt", new=AsyncMock(side_effect=[
            {"stage": "claimed"}, {"stage": "event_logged"}
        ])),
        patch.object(bot.db, "get_user_by_id", new=AsyncMock(return_value=owner)),
        patch.object(bot.db, "log_listing_contact_for_update", new=event),
        patch.object(bot.db, "set_telegram_update_stage", new=set_stage),
        patch.object(bot.db, "release_telegram_update", new=release),
        patch.object(bot.db, "complete_telegram_update", new=AsyncMock(return_value=True)),
    ):
        with pytest.raises(RuntimeError, match="no longer processing"):
            asyncio.run(bot.listing_contact_callback(update, context))
        asyncio.run(bot.listing_contact_callback(update, context))

    release.assert_awaited_once_with(880005, "listing_contact")
    assert event.await_count == 2
    context.bot.send_message.assert_awaited_once()


def test_business_contact_deduplicates_replayed_update_but_returns_business_details():
    query = SimpleNamespace(
        data="business_contact_9",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(
        update_id=880006,
        callback_query=query,
        effective_user=SimpleNamespace(id=7001),
    )
    context = SimpleNamespace(bot=SimpleNamespace())
    business = {"id": 9, "business_name": "متجر", "phone": "0900123456"}
    claim = AsyncMock(side_effect=[True, False])
    event = AsyncMock(return_value=True)

    with (
        patch.object(bot.db, "get_business", new=AsyncMock(return_value=business)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "log_business_contact_for_update", new=event),
        patch.object(bot.db, "complete_telegram_update", new=AsyncMock(return_value=True)),
    ):
        asyncio.run(bot.business_contact_callback(update, context))
        asyncio.run(bot.business_contact_callback(update, context))

    event.assert_awaited_once_with(9, 17, 880006)
    assert query.message.reply_text.await_count == 2
    assert "0900123456" in query.message.reply_text.await_args_list[0].args[0]
    assert "مسبقًا" in query.message.reply_text.await_args_list[1].args[0]


def test_business_contact_distinct_update_ids_remain_distinct_without_new_rate_limit():
    business = {"id": 9, "business_name": "متجر", "phone": "0900123456"}
    event = AsyncMock(return_value=True)
    claim = AsyncMock(return_value=True)
    complete = AsyncMock(return_value=True)

    async def contact(update_id):
        query = SimpleNamespace(
            data="business_contact_9",
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(
            update_id=update_id,
            callback_query=query,
            effective_user=SimpleNamespace(id=7001),
        )
        context = SimpleNamespace(bot=SimpleNamespace())
        await bot.business_contact_callback(update, context)
        return query

    with (
        patch.object(bot.db, "get_business", new=AsyncMock(return_value=business)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "log_business_contact_for_update", new=event),
        patch.object(bot.db, "complete_telegram_update", new=complete),
    ):
        first = asyncio.run(contact(880007))
        second = asyncio.run(contact(880008))

    assert [call.args[2] for call in event.await_args_list] == [880007, 880008]
    assert event.await_count == 2
    assert complete.await_count == 2
    assert "0900123456" in first.message.reply_text.await_args.args[0]
    assert "0900123456" in second.message.reply_text.await_args.args[0]


def test_listing_details_reply_failure_resumes_without_repeating_event_or_owner_notice():
    update, context, query = _contact_update(880009)
    listing = {"id": 42, "user_id": 8, "title": "هاتف", "contact": "0912345678"}
    owner = {"id": 8, "telegram_id": 8008, "username": "seller"}
    claim = AsyncMock(side_effect=[True, True])
    receipt = AsyncMock(side_effect=[
        {"stage": "claimed"}, {"stage": "notification_sent"}
    ])
    event = AsyncMock(return_value=True)
    set_stage = AsyncMock(return_value=True)
    complete = AsyncMock(return_value=True)
    release = AsyncMock(return_value=True)
    query.message.reply_text = AsyncMock(side_effect=[TimedOut("details reply lost"), None])

    with (
        patch.object(bot.db, "get_public_listing", new=AsyncMock(return_value=listing)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "get_user_by_id", new=AsyncMock(return_value=owner)),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "get_telegram_update_receipt", new=receipt),
        patch.object(bot.db, "log_listing_contact_for_update", new=event),
        patch.object(bot.db, "set_telegram_update_stage", new=set_stage),
        patch.object(bot.db, "complete_telegram_update", new=complete),
        patch.object(bot.db, "release_telegram_update", new=release),
    ):
        with pytest.raises(TimedOut, match="details reply lost"):
            asyncio.run(bot.listing_contact_callback(update, context))
        complete.assert_not_awaited()
        asyncio.run(bot.listing_contact_callback(update, context))

    event.assert_awaited_once_with(42, 17, 880009)
    context.bot.send_message.assert_awaited_once()
    release.assert_awaited_once_with(880009, "listing_contact")
    complete.assert_awaited_once_with(880009, "listing_contact")
    assert query.message.reply_text.await_count == 2
    assert "0912345678" in query.message.reply_text.await_args_list[1].args[0]


def _coupon_update(update_id):
    query = SimpleNamespace(
        data="getcode_17",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(
        update_id=update_id,
        callback_query=query,
        effective_user=SimpleNamespace(id=7001),
    )
    context = SimpleNamespace(user_data={"db_user_id": 17})
    return update, context, query


def _patch_coupon_callback(claim, create_code, reply_event, complete):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(bot, "_ensure_db_user", new=AsyncMock()))
    stack.enter_context(patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})))
    stack.enter_context(patch.object(bot.db, "get_campaign", new=AsyncMock(return_value={
        "id": 17, "status": "active", "city_id": 3,
    })))
    stack.enter_context(patch.object(bot.db, "pool", return_value=SimpleNamespace(
        fetchrow=AsyncMock(return_value={"code": "KHARTOUM"})
    )))
    stack.enter_context(patch.object(bot.db, "claim_telegram_update", new=claim))
    stack.enter_context(patch.object(bot.db, "get_telegram_update_receipt", new=AsyncMock(return_value={"stage": "claimed"})))
    stack.enter_context(patch.object(bot.db, "log_campaign_event_for_update", new=reply_event))
    stack.enter_context(patch.object(bot.db, "create_discount_code", new=create_code))
    stack.enter_context(patch.object(bot.db, "complete_telegram_update", new=complete))
    return stack


def test_duplicate_coupon_update_does_not_retrieve_code_or_reply_again():
    update, context, query = _coupon_update(880010)
    claim = AsyncMock(side_effect=[True, False])
    create_code = AsyncMock(return_value={"code": "KHART-ABCDE"})
    reply_event = AsyncMock(return_value=True)
    complete = AsyncMock(return_value=True)

    with _patch_coupon_callback(claim, create_code, reply_event, complete):
        asyncio.run(bot.get_discount_code(update, context))
        asyncio.run(bot.get_discount_code(update, context))

    claim.assert_awaited_with(880010, "get_discount_code")
    create_code.assert_awaited_once_with(17, 17, "KHARTOUM")
    reply_event.assert_awaited_once_with(17, 17, "CLICKED", 880010, "get_discount_code")
    complete.assert_awaited_once_with(880010, "get_discount_code")
    query.message.reply_text.assert_awaited_once()


def test_coupon_update_claim_database_error_propagates_without_coupon_reply():
    update, context, query = _coupon_update(880011)
    claim = AsyncMock(side_effect=RuntimeError("database unavailable"))
    create_code = AsyncMock(return_value={"code": "KHART-ABCDE"})
    reply_event = AsyncMock(return_value=True)
    complete = AsyncMock(return_value=True)

    with _patch_coupon_callback(claim, create_code, reply_event, complete):
        with pytest.raises(RuntimeError, match="database unavailable"):
            asyncio.run(bot.get_discount_code(update, context))

    claim.assert_awaited_once_with(880011, "get_discount_code")
    create_code.assert_not_awaited()
    reply_event.assert_not_awaited()
    complete.assert_not_awaited()
    query.message.reply_text.assert_not_awaited()


def test_ambiguous_owner_notice_does_not_block_details_retry_or_get_retried():
    update, context, query = _contact_update(880013)
    listing = {"id": 42, "user_id": 8, "title": "هاتف", "contact": "0912345678"}
    owner = {"id": 8, "telegram_id": 8008, "username": "seller"}
    claim = AsyncMock(side_effect=[True, True])
    receipt = AsyncMock(side_effect=[
        {"stage": "claimed"}, {"stage": "notification_attempting"}
    ])
    event = AsyncMock(return_value=True)
    set_stage = AsyncMock(return_value=True)
    mark_uncertain = AsyncMock(return_value=True)
    release = AsyncMock(return_value=True)
    complete = AsyncMock(return_value=True)
    context.bot.send_message.side_effect = TimedOut("owner notice may have arrived")
    query.message.reply_text = AsyncMock(side_effect=[TimedOut("details reply lost"), None])

    with (
        patch.object(bot.db, "get_public_listing", new=AsyncMock(return_value=listing)),
        patch.object(bot.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(bot.db, "get_user_by_id", new=AsyncMock(return_value=owner)),
        patch.object(bot.db, "claim_telegram_update", new=claim),
        patch.object(bot.db, "get_telegram_update_receipt", new=receipt),
        patch.object(bot.db, "log_listing_contact_for_update", new=event),
        patch.object(bot.db, "set_telegram_update_stage", new=set_stage),
        patch.object(bot.db, "mark_telegram_update_uncertain", new=mark_uncertain),
        patch.object(bot.db, "release_telegram_update", new=release),
        patch.object(bot.db, "complete_telegram_update", new=complete),
    ):
        with pytest.raises(TimedOut, match="details reply lost"):
            asyncio.run(bot.listing_contact_callback(update, context))
        mark_uncertain.assert_not_awaited()
        asyncio.run(bot.listing_contact_callback(update, context))

    event.assert_awaited_once_with(42, 17, 880013)
    context.bot.send_message.assert_awaited_once()
    release.assert_awaited_once_with(880013, "listing_contact")
    mark_uncertain.assert_awaited_once_with(880013, "listing_contact")
    complete.assert_not_awaited()
    assert query.message.reply_text.await_count == 2
