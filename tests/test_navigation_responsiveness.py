import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.error import TimedOut
from telegram.ext import CallbackQueryHandler, ConversationHandler

import bot
import db
import marketplace


def _message_update(text):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(
        message=message,
        callback_query=None,
        channel_post=None,
        edited_channel_post=None,
        edited_message=None,
        business_message=None,
        edited_business_message=None,
        effective_message=message,
        effective_user=SimpleNamespace(id=7001, username="tester", first_name="Test"),
    )


def test_root_keyboard_filter_accepts_every_visible_navigation_button():
    for label in bot.ROOT_KEYBOARD_TEXTS:
        assert bot._root_keyboard_filter().check_update(_message_update(label)), label


def test_marketplace_button_works_from_a_plain_message_without_start():
    update = _message_update("🛒 سوق الأفراد")
    context = SimpleNamespace(user_data={"db_user_id": 17})

    state = asyncio.run(bot.text_role_router(update, context))

    assert state == marketplace.MARKET_MENU
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    }
    assert {"market_buy", "market_sell", "main_menu"}.issubset(callbacks)


def test_main_menu_button_returns_to_active_root_without_calling_start():
    update = _message_update("🏠 القائمة الرئيسية")
    context = SimpleNamespace(user_data={"db_user_id": 17, "market_preview_id": "draft"})

    state = asyncio.run(bot.text_role_router(update, context))

    assert state == bot.SELECT_ROLE
    assert context.user_data["market_preview_id"] == "draft"
    assert "القائمة الرئيسية" in update.message.reply_text.await_args.args[0]
    assert update.message.reply_text.await_args.kwargs["reply_markup"] is not None


def test_explicit_cancel_discards_only_current_section_draft_and_keeps_other_draft():
    update = _message_update("❌ إلغاء")
    context = SimpleNamespace(user_data={
        "db_user_id": 17,
        "navigation_section": "marketplace",
        "market_draft": {"title": "مسودة بيع"},
        "market_preview_id": "draft",
        "pending_campaign": {"title": "draft"},
    })

    state = asyncio.run(bot.text_role_router(update, context))

    assert state == bot.SELECT_ROLE
    assert context.user_data["db_user_id"] == 17
    assert "market_preview_id" not in context.user_data
    assert "market_draft" not in context.user_data
    assert context.user_data["pending_campaign"]["title"] == "draft"
    assert "لم يتغير أي إعلان محفوظ" in update.message.reply_text.await_args.args[0]


def test_stats_keyboard_shows_all_owned_listings_without_asking_for_id():
    rows = [{
        "id": 42,
        "title": "هاتف",
        "status": "approved",
        "expires_at": "2026-10-20",
        "image_file_ids": ["photo"],
        "views": 8,
        "unique_viewers": 5,
        "contacts": 2,
        "unique_contacts": 2,
    }]
    update = _message_update("#إحصاء#")
    context = SimpleNamespace(user_data={"db_user_id": 17}, args=[])
    with patch.object(db, "list_user_listings_page", new=AsyncMock(return_value=(rows, False, False))) as list_rows:
        state = asyncio.run(bot.text_role_router(update, context))

    assert state == bot.SELECT_ROLE
    list_rows.assert_awaited_once_with(17, mode="stats", cursor_id=None, direction="next", limit=10)
    text = update.message.reply_text.await_args.args[0]
    assert "إحصاءات إعلاناتك" in text
    assert "المشاهدات: 8" in text
    assert "بانتظار إدخال رقم" not in text


def test_renew_button_presents_owned_listings_as_direct_actions():
    rows = [{
        "id": 42,
        "title": "هاتف",
        "status": "approved",
        "expires_at": "2026-10-20",
        "image_file_ids": ["photo"],
        "views": 0,
        "unique_viewers": 0,
        "contacts": 0,
        "unique_contacts": 0,
    }]
    update = _message_update("#تجديد#")
    context = SimpleNamespace(user_data={"db_user_id": 17}, args=[])
    with patch.object(db, "list_user_listings_page", new=AsyncMock(return_value=(rows, False, False))):
        state = asyncio.run(bot.text_role_router(update, context))

    assert state == bot.SELECT_ROLE
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "mylisting_renew_42" in callbacks
    assert "main_menu" in callbacks


def test_command_my_listings_uses_aggregated_rows_instead_of_n_plus_one_reports():
    rows = [{
        "id": 42,
        "title": "هاتف",
        "status": "approved",
        "expires_at": "2026-10-20",
        "image_file_ids": ["photo"],
        "views": 8,
        "unique_viewers": 5,
        "contacts": 2,
        "unique_contacts": 2,
    }]
    update = _message_update("/mylistings")
    context = SimpleNamespace(user_data={"db_user_id": 17})
    with (
        patch.object(db, "list_user_listings_page", new=AsyncMock(return_value=(rows, False, False))) as list_rows,
        patch.object(db, "listing_report", new=AsyncMock()) as report,
    ):
        asyncio.run(bot.cmd_my_listings(update, context))

    list_rows.assert_awaited_once_with(17, mode="list", cursor_id=None, direction="next", limit=10)
    report.assert_not_awaited()


def test_market_menu_continues_after_telegram_callback_ack_timeout():
    query = SimpleNamespace(
        answer=AsyncMock(side_effect=TimedOut("slow Telegram response")),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7001))
    context = SimpleNamespace(user_data={"db_user_id": 17})

    state = asyncio.run(marketplace.market_menu(update, context))

    assert state == marketplace.MARKET_MENU
    query.edit_message_text.assert_awaited_once()
    context.user_data["navigation_section"] = "marketplace"


def test_listing_view_events_lock_each_user_listing_pair_and_use_utc_day():
    class AsyncContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    connection = SimpleNamespace(
        fetchval=AsyncMock(side_effect=[None, datetime.now(timezone.utc)] * 3),
        execute=AsyncMock(),
        transaction=lambda: AsyncContext(),
    )

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *_args):
            return False

    with patch.object(db, "pool", return_value=SimpleNamespace(acquire=lambda: Acquire())):
        asyncio.run(db.log_listing_views([2, 2, 3, 4], 17))

    assert connection.fetchval.await_count == 6
    lock_calls = connection.fetchval.await_args_list[::2]
    utc_day_calls = connection.fetchval.await_args_list[1::2]
    assert [call.args[1:] for call in lock_calls] == [(2, 17), (3, 17), (4, 17)]
    assert all(call.args[0] == "SELECT date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'" for call in utc_day_calls)
    assert all(call.args[0] == "SELECT pg_advisory_xact_lock($1::integer, $2::integer)" for call in lock_calls)
    assert connection.execute.await_count == 3
    for call in connection.execute.await_args_list:
        assert "NOT EXISTS" in call.args[0]
        assert "created_at >= $3" in call.args[0]
        assert "created_at < $3 + interval '1 day'" in call.args[0]


def test_root_reply_keyboard_and_listing_callbacks_reenter_from_stale_state(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    app = bot.build_application()
    conv = next(h for group in app.handlers.values() for h in group if isinstance(h, ConversationHandler))
    user = User(7001, "Test", False)
    bot_user = User(999, "Bot", True)
    chat = Chat(7001, Chat.PRIVATE)
    conv._conversations[(7001, 7001)] = bot.CAMPAIGN_DESC

    message = Message(1, datetime.now(timezone.utc), chat, from_user=user, text="📋 إعلاناتي")
    message_route = conv.check_update(Update(1, message=message))
    assert message_route[2].callback is bot.text_role_router

    callback_message = Message(2, datetime.now(timezone.utc), chat, from_user=bot_user, text="قائمة الإعلانات")
    query = CallbackQuery("q1", user, "chat-instance", message=callback_message, data="mylisting_stats_42")
    callback_route = conv.check_update(Update(2, callback_query=query))
    assert isinstance(callback_route[2], CallbackQueryHandler)
    assert callback_route[2].pattern.match("mylisting_stats_42")
    assert conv.allow_reentry is True


def test_marketplace_back_and_root_callbacks_reenter_from_every_active_flow_state(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    app = bot.build_application()
    conv = next(h for group in app.handlers.values() for h in group if isinstance(h, ConversationHandler))
    user = User(7001, "Test", False)
    bot_user = User(999, "Bot", True)
    chat = Chat(7001, Chat.PRIVATE)
    states = (
        marketplace.MARKET_MENU,
        marketplace.SELL_CITY,
        marketplace.SELL_MESSAGE,
        marketplace.SELL_CONFIRM,
        marketplace.BUY_CITY,
        marketplace.BUY_QUERY,
        marketplace.EDIT_LISTING_CITY,
        marketplace.EDIT_LISTING_MESSAGE,
        marketplace.EDIT_LISTING_CONFIRM,
    )
    callbacks = {
        "customer_market": marketplace.market_menu,
        "main_menu": bot.main_menu_callback,
    }
    update_id = 10
    for active_state in states:
        conv._conversations[(7001, 7001)] = active_state
        for callback_data, expected_callback in callbacks.items():
            message = Message(update_id, datetime.now(timezone.utc), chat, from_user=bot_user, text="navigation")
            query = CallbackQuery(
                f"q{update_id}", user, "chat-instance", message=message, data=callback_data
            )
            route = conv.check_update(Update(update_id, callback_query=query))
            assert route is not None, (active_state, callback_data)
            assert route[2].callback is expected_callback, (active_state, callback_data)
            update_id += 1


def test_reply_keyboard_back_returns_to_market_menu_from_marketplace_child_flow():
    update = _message_update("↩️ رجوع")
    context = SimpleNamespace(user_data={"db_user_id": 17, "navigation_parent": "market_menu"})

    state = asyncio.run(bot.text_role_router(update, context))

    assert state == marketplace.MARKET_MENU
    assert "سوق الأفراد" in update.message.reply_text.await_args.args[0]
    callbacks = {
        button.callback_data
        for row in update.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert {"market_buy", "market_sell", "main_menu"}.issubset(callbacks)


def test_campaign_preview_can_be_resumed_without_losing_its_identity():
    preview = SimpleNamespace(message_id=77, chat=SimpleNamespace(id=7001))
    query = SimpleNamespace(
        data="resume_campaign",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(return_value=preview),
    )
    pending = {
        "preview_id": "abcdef123456",
        "ad_text": "عرض عربي",
        "ai_data": {"category_code": "FOOD", "city_code": "KHARTOUM", "discount": 20},
    }
    context = SimpleNamespace(
        user_data={"pending_campaign": pending, "campaign_preview_id": "abcdef123456"}
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7001),
    )

    state = asyncio.run(bot.resume_campaign_preview(update, context))

    assert state == bot.CAMPAIGN_CONFIRM
    assert context.user_data["pending_campaign"] is pending
    assert context.user_data["navigation_parent"] == "merchant_menu"
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    callbacks = {button.callback_data for row in markup.inline_keyboard for button in row}
    assert "campaign_abcdef123456_submit" in callbacks
    assert "campaign_abcdef123456_rewrite" in callbacks
    assert "merchant_menu" in callbacks and "main_menu" in callbacks
    preview_text = query.edit_message_text.await_args.args[0]
    assert "مطاعم وكافيهات" in preview_text and "الخرطوم" in preview_text
    assert "FOOD" not in preview_text and "KHARTOUM" not in preview_text


def test_ai_requests_default_to_short_timeout_without_automatic_retries(monkeypatch):
    import ai

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("AI_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(ai, "AsyncOpenAI", Mock())
    ai._client = None
    try:
        ai.client()
        ai.AsyncOpenAI.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.groq.com/openai/v1",
            timeout=20.0,
            max_retries=0,
        )
    finally:
        ai._client = None


def test_market_listing_preview_resumes_with_same_id_and_submission_key():
    preview = SimpleNamespace(message_id=88, chat=SimpleNamespace(id=7001))
    query = SimpleNamespace(
        data="resume_listing_flow",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(return_value=preview),
    )
    draft = {
        "title": "هاتف",
        "description": "تفاصيل",
        "price": "100",
        "negotiable": True,
        "city_id": 3,
        "city_name": "بحري",
        "address": "السوق",
        "contact": "0912345678",
        "delivery": "متاح",
        "photo_ids": ["photo"],
        "preview_id": "012345abcdef",
        "submission_key": "stable-submission-key",
    }
    context = SimpleNamespace(
        user_data={"market_draft": draft, "market_preview_id": "012345abcdef"}
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7001),
    )

    state = asyncio.run(marketplace.resume_listing_flow(update, context))

    assert state == marketplace.SELL_CONFIRM
    assert draft["submission_key"] == "stable-submission-key"
    assert context.user_data["navigation_parent"] == "market_menu"
    callbacks = {
        button.callback_data
        for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "market_012345abcdef_submit" in callbacks
    assert "market_012345abcdef_cancel" in callbacks
    labels = {button.text for row in bot._user_reply_keyboard(context).keyboard for button in row}
    assert "📄 متابعة مسودة الإعلان" in labels


def test_market_submission_passes_stable_idempotency_key_to_database():
    query = SimpleNamespace(
        data="market_012345abcdef_submit",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    draft = {
        "preview_id": "012345abcdef",
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
        "submission_key": "stable-submission-key",
    }
    context = SimpleNamespace(
        user_data={"db_user_id": 17, "market_draft": draft, "market_preview_id": "012345abcdef"}
    )
    with patch.object(db, "create_listing", new=AsyncMock(return_value={"id": 42})) as create:
        state = asyncio.run(marketplace.sell_confirm(SimpleNamespace(callback_query=query), context))

    assert state == marketplace.MARKET_MENU
    assert create.await_args.kwargs["submission_key"] == "stable-submission-key"
    assert "market_draft" not in context.user_data


def _listing_page_rows(ids):
    return [
        {
            "id": listing_id,
            "title": f"إعلان {listing_id}",
            "status": "approved",
            "expires_at": datetime.now(timezone.utc),
            "image_file_ids": ["photo"],
            "views": 0,
            "unique_viewers": 0,
            "contacts": 0,
            "unique_contacts": 0,
        }
        for listing_id in ids
    ]


def test_owner_listings_use_non_overlapping_keyset_pages_and_previous_button():
    context = SimpleNamespace(user_data={"db_user_id": 17})
    first_page = _listing_page_rows(range(100, 90, -1))
    second_page = _listing_page_rows(range(90, 80, -1))
    initial = _message_update("unused")
    with patch.object(
        db, "list_user_listings_page", new=AsyncMock(return_value=(first_page, False, True))
    ) as list_page:
        asyncio.run(bot._show_user_listings(initial, context))

    list_page.assert_awaited_once_with(
        17, mode="list", cursor_id=None, direction="next", limit=10
    )
    first_markup = initial.message.reply_text.await_args.kwargs["reply_markup"]
    first_callbacks = {
        button.callback_data for row in first_markup.inline_keyboard for button in row
    }
    assert "mylistings_page_list_next_91" in first_callbacks
    assert not any(value.startswith("mylistings_page_list_prev_") for value in first_callbacks)

    query = SimpleNamespace(
        data="mylistings_page_list_next_91",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=7001, username="tester", first_name="Test"),
    )
    with patch.object(
        db, "list_user_listings_page", new=AsyncMock(return_value=(second_page, True, False))
    ) as list_page:
        state = asyncio.run(bot.my_listings_page_callback(update, context))

    assert state == bot.SELECT_ROLE
    list_page.assert_awaited_once_with(
        17, mode="list", cursor_id=91, direction="next", limit=10
    )
    second_markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    second_callbacks = {
        button.callback_data for row in second_markup.inline_keyboard for button in row
    }
    assert "mylistings_page_list_prev_90" in second_callbacks
    first_ids = {row["id"] for row in first_page}
    second_ids = {row["id"] for row in second_page}
    assert first_ids.isdisjoint(second_ids)


def test_empty_filtered_page_after_status_change_offers_a_safe_restart():
    context = SimpleNamespace(user_data={"db_user_id": 17})
    query = SimpleNamespace(
        data="mylistings_page_renew_next_70",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=7001, username="tester", first_name="Test"),
    )
    with patch.object(
        db, "list_user_listings_page", new=AsyncMock(return_value=([], True, False))
    ):
        state = asyncio.run(bot.my_listings_page_callback(update, context))

    assert state == bot.SELECT_ROLE
    assert "تغيرت حالتها" in query.edit_message_text.await_args.args[0]
    callbacks = {
        button.callback_data
        for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "mylistings" in callbacks
