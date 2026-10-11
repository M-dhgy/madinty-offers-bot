import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import marketplace


def _callbacks(markup):
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    }


@pytest.mark.parametrize(
    "callback_data",
    ["market_submit", "market_aaaaaaaaaaaa_submit"],
)
def test_stale_or_legacy_listing_preview_cannot_submit_current_draft(callback_data):
    preview_id = "bbbbbbbbbbbb"
    query = SimpleNamespace(
        data=callback_data,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    context = SimpleNamespace(
        user_data={
            "market_draft": {"preview_id": preview_id},
            "market_preview_id": preview_id,
        }
    )
    with patch.object(marketplace.db, "create_listing", new=AsyncMock()) as create:
        state = asyncio.run(marketplace.sell_confirm(SimpleNamespace(callback_query=query), context))

    assert state == marketplace.SELL_CONFIRM
    create.assert_not_awaited()
    assert context.user_data["market_preview_id"] == preview_id
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_stale_search_pagination_cannot_reuse_a_new_search_result_set():
    query = SimpleNamespace(
        data="market_more_aaaaaaaaaaaa",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    context = SimpleNamespace(
        user_data={"market_search_id": "bbbbbbbbbbbb", "market_results": [1, 2, 3]},
        bot=SimpleNamespace(list_public_listings=AsyncMock()),
    )
    with patch.object(marketplace.db, "list_public_listings", new=AsyncMock()) as list_rows:
        state = asyncio.run(marketplace.more_results(
            SimpleNamespace(callback_query=query), context
        ))

    assert state == marketplace.MARKET_MENU
    list_rows.assert_not_awaited()
    assert context.user_data["market_results"] == [1, 2, 3]
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_legacy_campaign_button_cannot_apply_to_the_current_draft(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    import bot

    query = SimpleNamespace(
        data="submit_campaign",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    context = SimpleNamespace(
        user_data={
            "pending_campaign": {"preview_id": "bbbbbbbbbbbb", "ai_data": {}},
            "campaign_preview_id": "bbbbbbbbbbbb",
        }
    )
    with patch.object(bot.db, "create_campaign", new=AsyncMock()) as create:
        state = asyncio.run(bot.campaign_confirm_router(
            SimpleNamespace(callback_query=query), context
        ))

    assert state == bot.CAMPAIGN_CONFIRM
    create.assert_not_awaited()
    assert context.user_data["campaign_preview_id"] == "bbbbbbbbbbbb"
    assert query.answer.await_args.kwargs["show_alert"] is True


def test_campaign_preview_ids_are_unique_and_old_keyboard_is_invalidated(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    import bot

    campaign_data = {
        "issues": [],
        "category_code": "FOOD",
        "city_code": "KHARTOUM",
        "discount": 10,
        "target": "الجميع",
        "title": "عرض تجريبي",
    }
    sent_messages = [
        None,
        SimpleNamespace(message_id=71),
        None,
        SimpleNamespace(message_id=72),
    ]
    message = SimpleNamespace(
        text="خصم تجريبي",
        reply_text=AsyncMock(side_effect=sent_messages),
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=900),
    )
    context = SimpleNamespace(
        user_data={"db_user_id": 8},
        bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()),
    )
    fake_pool = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[{"id": 2}, {"id": 3}, {"id": 2}, {"id": 3}])
    )
    with (
        patch.object(bot.db, "get_business_by_user", new=AsyncMock(return_value={"id": 4, "status": "approved"})),
        patch.object(bot.db, "pool", return_value=fake_pool),
        patch.object(bot.db, "validate_campaign_target", new=AsyncMock(return_value=True)),
        patch.object(bot.ai, "extract_campaign_data", new=AsyncMock(return_value=campaign_data)),
        patch.object(bot.ai, "generate_ad_copy", new=AsyncMock(return_value="نص الحملة")),
    ):
        first_state = asyncio.run(bot.campaign_description_received(update, context))
        first_id = context.user_data["campaign_preview_id"]
        first_markup = message.reply_text.await_args_list[1].kwargs["reply_markup"]
        second_state = asyncio.run(bot.campaign_description_received(update, context))
        second_id = context.user_data["campaign_preview_id"]
        second_markup = message.reply_text.await_args_list[3].kwargs["reply_markup"]

    assert first_state == second_state == bot.CAMPAIGN_CONFIRM
    assert first_id != second_id
    assert _callbacks(first_markup) == {
        f"campaign_{first_id}_submit", f"campaign_{first_id}_rewrite"
    }
    assert _callbacks(second_markup) == {
        f"campaign_{second_id}_submit", f"campaign_{second_id}_rewrite"
    }
    context.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=900, message_id=71, reply_markup=None
    )

    stale_query = SimpleNamespace(
        data=f"campaign_{first_id}_submit",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    with patch.object(bot.db, "create_campaign", new=AsyncMock()) as create:
        state = asyncio.run(bot.campaign_confirm_router(
            SimpleNamespace(callback_query=stale_query), context
        ))
    assert state == bot.CAMPAIGN_CONFIRM
    create.assert_not_awaited()
    assert context.user_data["campaign_preview_id"] == second_id


def test_admin_listing_review_continues_when_telegram_rejects_photo(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    import bot

    row = {
        "id": 45,
        "title": "هاتف للاختبار",
        "price": 100,
        "negotiable": False,
        "city_name": "بحري",
        "address": "السوق العربي",
        "contact": "0912345678",
        "delivery": "لا",
        "description": "تفاصيل الإعلان",
        "image_file_ids": ["expired-telegram-photo"],
    }
    review_message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=review_message,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=765),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(send_photo=AsyncMock(side_effect=RuntimeError("bad file id")))
    )
    with (
        patch.object(bot, "is_admin", return_value=True),
        patch.object(bot.db, "list_pending_listings", new=AsyncMock(return_value=[row])),
    ):
        asyncio.run(bot.admin_listings_callback(update, context))

    context.bot.send_photo.assert_awaited_once_with(765, "expired-telegram-photo")
    review_message.reply_text.assert_awaited_once()
    args = review_message.reply_text.await_args
    assert "هاتف للاختبار" in args.args[0]
    assert "0912345678" in args.args[0]
    callbacks = _callbacks(args.kwargs["reply_markup"])
    assert callbacks == {"listing_approve_45", "listing_reject_45"}
