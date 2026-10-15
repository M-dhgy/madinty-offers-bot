import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import TimedOut
from telegram.ext import ConversationHandler

import bot
import db
import marketplace


def _action_update(callback_data):
    query = SimpleNamespace(
        data=callback_data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=7001, username="owner", first_name="Owner"),
    ), query


@pytest.mark.parametrize(
    ("action", "first_status", "second_status", "expected_message"),
    [
        ("sold", "approved", "sold", "سبق"),
        ("renew", "approved", "pending_review", "لا يمكن تجديد"),
    ],
)
def test_sale_and_renew_are_single_transitions_after_reply_timeout(
    action, first_status, second_status, expected_message
):
    update, query = _action_update(f"mylisting_{action}_42")
    query.edit_message_text.side_effect = [TimedOut("reply lost"), None]
    current = {
        "id": 42,
        "user_id": 17,
        "title": "هاتف",
        "status": first_status,
        "image_file_ids": ["photo"],
        "expires_at": datetime.now(timezone.utc),
    }
    after = {**current, "status": second_status}
    context = SimpleNamespace(user_data={"db_user_id": 17})
    transition = AsyncMock(side_effect=[after, None])
    function_name = "mark_listing_sold" if action == "sold" else "renew_listing"

    with (
        patch.object(db, "get_listing", new=AsyncMock(side_effect=[current, after])),
        patch.object(db, function_name, new=transition),
    ):
        with pytest.raises(TimedOut, match="reply lost"):
            asyncio.run(bot.my_listing_action_callback(update, context))
        asyncio.run(bot.my_listing_action_callback(update, context))

    assert transition.await_count == 2
    assert query.edit_message_text.await_count == 2
    assert expected_message in query.edit_message_text.await_args.args[0]


def test_statistics_action_shows_all_counts_and_rejects_foreign_listing():
    update, query = _action_update("mylisting_stats_42")
    context = SimpleNamespace(user_data={"db_user_id": 17})
    listing = {
        "id": 42, "user_id": 17, "title": "هاتف", "status": "approved",
        "expires_at": datetime.now(timezone.utc), "image_file_ids": ["photo"],
    }
    stats = {"views": 7, "unique_viewers": 5, "contacts": 3, "unique_contacts": 2}
    with (
        patch.object(db, "get_listing", new=AsyncMock(return_value=listing)),
        patch.object(db, "listing_report", new=AsyncMock(return_value=stats)) as report,
    ):
        asyncio.run(bot.my_listing_action_callback(update, context))
    report.assert_awaited_once_with(42)
    text = query.edit_message_text.await_args.args[0]
    assert "المشاهدات: 7" in text and "مرات التواصل: 3" in text

    update, query = _action_update("mylisting_stats_42")
    foreign = {**listing, "user_id": 99}
    with (
        patch.object(db, "get_listing", new=AsyncMock(return_value=foreign)),
        patch.object(db, "listing_report", new=AsyncMock()) as report,
    ):
        asyncio.run(bot.my_listing_action_callback(update, context))
    report.assert_not_awaited()
    assert "لا يخص حسابك" in query.edit_message_text.await_args.args[0]


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        (None, "غير موجود"),
        ({"id": 42, "user_id": 99, "status": "rejected", "title": "هاتف"}, "لا يخص حسابك"),
        ({"id": 42, "user_id": 17, "status": "approved", "title": "هاتف"}, "المرفوضة فقط"),
    ],
)
def test_edit_listing_rejects_missing_foreign_or_wrong_status(listing, expected):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7001),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(args=["42"], user_data={})
    with (
        patch.object(db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 17})),
        patch.object(db, "get_listing", new=AsyncMock(return_value=listing)),
        patch.object(db, "list_cities", new=AsyncMock()) as cities,
    ):
        state = asyncio.run(marketplace.edit_listing_start(update, context))

    assert state == ConversationHandler.END
    assert expected in update.message.reply_text.await_args.args[0]
    cities.assert_not_awaited()


def test_new_sale_prompts_before_replacing_existing_unsent_draft():
    update, query = _action_update("market_sell")
    draft = {"preview_id": "012345abcdef", "title": "هاتف قديم", "submission_key": "keep-this"}
    context = SimpleNamespace(user_data={"market_draft": draft, "market_preview_id": "012345abcdef"})
    with patch.object(db, "list_cities", new=AsyncMock()) as cities:
        state = asyncio.run(marketplace.sell_start(update, context))

    assert state == marketplace.MARKET_MENU
    cities.assert_not_awaited()
    assert context.user_data["market_draft"] is draft
    assert "لم أستبدلها" in query.edit_message_text.await_args.args[0]
    callbacks = {
        button.callback_data
        for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "market_sell_discard_012345abcdef" in callbacks
    assert "resume_listing_flow" in callbacks


def test_confirmed_market_draft_discard_starts_new_sale_only_after_confirmation():
    update, query = _action_update("market_sell_discard_012345abcdef")
    draft = {"preview_id": "012345abcdef", "submission_key": "same-old-key"}
    context = SimpleNamespace(
        user_data={"market_draft": draft, "market_preview_id": "012345abcdef"},
        bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()),
    )
    with patch.object(db, "list_cities", new=AsyncMock(return_value=[{"id": 3, "name": "بحري"}])):
        state = asyncio.run(marketplace.sell_discard_and_start(update, context))

    assert state == marketplace.SELL_CITY
    assert "market_draft" not in context.user_data
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is not None


def test_new_sale_preserves_an_edit_in_progress_before_preview():
    update, query = _action_update("market_sell")
    context = SimpleNamespace(user_data={
        "resubmitting_listing_id": 42,
        "resubmitting_listing_title": "دراجة",
    })
    with patch.object(db, "list_cities", new=AsyncMock()) as cities:
        state = asyncio.run(marketplace.sell_start(update, context))

    assert state == marketplace.MARKET_MENU
    assert context.user_data["resubmitting_listing_id"] == 42
    cities.assert_not_awaited()
    callbacks = {
        button.callback_data
        for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "resume_listing_flow" in callbacks
    assert any(value.startswith("market_sell_discard_") for value in callbacks)


def test_new_campaign_prompts_before_replacing_existing_preview(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    update, query = _action_update("new_campaign")
    pending = {
        "preview_id": "fedcba654321",
        "ai_data": {"title": "الحملة الحالية"},
        "submission_key": "keep-this-campaign",
    }
    context = SimpleNamespace(user_data={
        "db_user_id": 17,
        "pending_campaign": pending,
        "campaign_preview_id": "fedcba654321",
    })
    with (
        patch.object(db, "get_business_by_user", new=AsyncMock(return_value={"id": 9, "status": "approved"})),
        patch.object(bot, "_clear_campaign_preview", new=AsyncMock()) as clear,
    ):
        state = asyncio.run(bot.merchant_menu_router(update, context))

    assert state == bot.MER_MENU
    clear.assert_not_awaited()
    assert context.user_data["pending_campaign"] is pending
    assert "لم أستبدلها" in query.edit_message_text.await_args.args[0]
    callbacks = {
        button.callback_data
        for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "resume_campaign" in callbacks
    assert "new_campaign_discard_fedcba654321" in callbacks


def test_confirmed_campaign_preview_discard_starts_new_campaign(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    update, query = _action_update("new_campaign_discard_fedcba654321")
    context = SimpleNamespace(user_data={
        "db_user_id": 17,
        "pending_campaign": {"preview_id": "fedcba654321"},
        "campaign_preview_id": "fedcba654321",
    })
    with (
        patch.object(db, "get_business_by_user", new=AsyncMock(return_value={"id": 9, "status": "approved"})),
        patch.object(bot, "_clear_campaign_preview", new=AsyncMock()),
    ):
        state = asyncio.run(bot.merchant_menu_router(update, context))

    assert state == bot.CAMPAIGN_DESC
    assert "pending_campaign" not in context.user_data
    assert "أُلغيت المعاينة غير المرسلة فقط" in query.edit_message_text.await_args.args[0]


def test_cancel_in_one_section_preserves_draft_in_another_section():
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    campaign = {"preview_id": "fedcba654321", "submission_key": "campaign-key"}
    context = SimpleNamespace(user_data={
        "db_user_id": 17,
        "navigation_section": "marketplace",
        "market_draft": {"preview_id": "012345abcdef"},
        "pending_campaign": campaign,
        "campaign_preview_id": "fedcba654321",
    })
    state = asyncio.run(bot.cancel(update, context))

    assert state == bot.SELECT_ROLE
    assert "market_draft" not in context.user_data
    assert context.user_data["pending_campaign"] is campaign
    assert context.user_data["db_user_id"] == 17
    assert "pending_campaign" in context.user_data


def test_cancel_campaign_preserves_unrelated_market_draft():
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    market_draft = {"preview_id": "012345abcdef", "submission_key": "listing-key"}
    context = SimpleNamespace(user_data={
        "db_user_id": 17,
        "navigation_section": "merchant",
        "market_draft": market_draft,
        "pending_campaign": {"preview_id": "fedcba654321"},
        "campaign_preview_id": "fedcba654321",
    })

    asyncio.run(bot.cancel(update, context))

    assert "pending_campaign" not in context.user_data
    assert context.user_data["market_draft"] is market_draft
    assert context.user_data["market_draft"]["submission_key"] == "listing-key"


def test_owner_page_uses_one_aggregate_select_after_one_expiry_sweep():
    fake_pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    with (
        patch.object(db, "pool", return_value=fake_pool),
        patch.object(db, "expire_listings", new=AsyncMock()) as expire,
    ):
        rows, has_previous, has_next = asyncio.run(
            db.list_user_listings_page(17, mode="stats", limit=10)
        )

    assert (rows, has_previous, has_next) == ([], False, False)
    expire.assert_awaited_once()
    fake_pool.fetch.assert_awaited_once()
    sql = fake_pool.fetch.await_args.args[0]
    assert "LEFT JOIN listing_events" in sql
    assert "COUNT(DISTINCT e.user_id)" in sql


def test_admin_approval_then_publish_shows_final_result(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    campaign = {"id": 17, "business_id": 4, "title": "عرض تجريبي"}
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    approve_update, approve_query = _action_update("admin_approve_17")
    broadcast_update, broadcast_query = _action_update("admin_send_17")
    result = {"status": "completed", "sent": 9, "total": 9, "failed": 0}

    with (
        patch.object(bot, "is_admin", return_value=True),
        patch.object(db, "review_campaign", new=AsyncMock(return_value=campaign)),
        patch.object(bot, "notify_merchant_campaign_decision", new=AsyncMock()) as notify,
        patch.object(bot.campaign_broadcast, "broadcast_campaign", new=AsyncMock(return_value=result)) as broadcast,
    ):
        asyncio.run(bot.admin_campaign_action(approve_update, context))
        assert "تم اعتماد الحملة #17" in approve_query.edit_message_text.await_args.args[0]
        callbacks = {
            button.callback_data
            for row in approve_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        }
        assert "admin_send_17" in callbacks
        notify.assert_awaited_once_with(context, campaign, "approved")

        asyncio.run(bot.admin_send_callback(broadcast_update, context))

    broadcast.assert_awaited_once()
    assert "وصل الإرسال إلى 9 من 9" in broadcast_query.edit_message_text.await_args.args[0]


def test_missing_campaign_action_and_failed_public_listing_return_clear_messages(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    update, query = _action_update("admin_approve_17")
    with (
        patch.object(bot, "is_admin", return_value=True),
        patch.object(db, "review_campaign", new=AsyncMock(return_value=None)),
    ):
        asyncio.run(bot.admin_campaign_action(update, SimpleNamespace()))
    assert "لم تعد بانتظار المراجعة" in query.edit_message_text.await_args.args[0]

    update, query = _action_update("admin_send_17")
    result = {"status": "not_ready"}
    with (
        patch.object(bot, "is_admin", return_value=True),
        patch.object(bot.campaign_broadcast, "broadcast_campaign", new=AsyncMock(return_value=result)),
    ):
        asyncio.run(bot.admin_send_callback(update, SimpleNamespace(bot=SimpleNamespace())))
    assert "لم يبدأ الإرسال" in query.edit_message_text.await_args.args[0]
