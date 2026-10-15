import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import ai
import db
import marketplace


CITY = {"id": 3, "name": "بحري", "code": "BAHRI"}
LISTING = {
    "id": 41,
    "user_id": 8,
    "title": "هاتف",
    "description": "هاتف بحالة ممتازة",
    "price": 125000,
    "condition": "مستعمل",
    "city_id": 3,
    "category": "إلكترونيات",
    "address": "السوق العربي",
    "contact": "0912345678",
    "delivery": "متاح",
    "negotiable": True,
    "image_file_ids": ["telegram-photo-id"],
    "status": "approved",
}


def fake_pool(**overrides):
    defaults = {
        "fetch": AsyncMock(return_value=[]),
        "fetchrow": AsyncMock(return_value=None),
        "fetchval": AsyncMock(return_value=None),
        "execute": AsyncMock(),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_expiration_only_targets_approved_listings_and_clears_image_references():
    fake = fake_pool()
    with patch.object(db, "pool", return_value=fake):
        asyncio.run(db.expire_listings())

    sql = fake.execute.await_args.args[0]
    assert "status='approved' AND expires_at <= now()" in sql
    assert "status='expired' AND cardinality(image_file_ids)>0" in sql
    assert "pending_review" not in sql
    assert "image_file_ids='{}'" in sql


def test_approval_starts_a_fresh_72_hour_listing_period_and_is_pending_only():
    fake = fake_pool(fetchrow=AsyncMock(return_value=LISTING))
    with patch.object(db, "pool", return_value=fake):
        asyncio.run(db.set_listing_status(41, "approved"))
        with pytest.raises(ValueError, match="approved or rejected"):
            asyncio.run(db.set_listing_status(41, "pending_review"))

    sql, *params = fake.fetchrow.await_args.args
    assert "expires_at=now()+interval '72 hours'" in sql
    assert "status='pending_review'" in sql
    assert params == ["approved", 41]
    assert fake.fetchrow.await_count == 1


def test_renewal_expires_due_listings_first_and_requires_an_image_reference():
    fake = fake_pool(fetchrow=AsyncMock(return_value=None))
    with patch.object(db, "pool", return_value=fake):
        asyncio.run(db.renew_listing(41, 8))

    assert "image_file_ids='{}'" in fake.execute.await_args.args[0]
    sql, *params = fake.fetchrow.await_args.args
    assert "status IN ('expired','approved')" in sql
    assert "cardinality(image_file_ids)>0" in sql
    assert params == [41, 8]


def test_renewal_transition_remains_owner_scoped_and_uses_the_existing_photo():
    renewed = {"id": 41, "user_id": 8, "status": "pending_review", "image_file_ids": ["fresh-photo"]}
    fake = fake_pool(fetchrow=AsyncMock(return_value=renewed))
    with patch.object(db, "pool", return_value=fake), patch.object(db, "expire_listings", new=AsyncMock()):
        result = asyncio.run(db.renew_listing(41, 8))

    assert result == renewed
    sql, *params = fake.fetchrow.await_args.args
    assert "SET status='pending_review'" in sql
    assert "WHERE id=$1 AND user_id=$2" in sql
    assert "cardinality(image_file_ids)>0" in sql
    assert params == [41, 8]


def test_sold_transition_is_limited_to_the_listing_owner():
    fake = fake_pool(fetchrow=AsyncMock(return_value={"id": 41, "status": "sold"}))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.mark_listing_sold(41, 8))

    assert result["status"] == "sold"
    sql, *params = fake.fetchrow.await_args.args
    assert "SET status='sold'" in sql
    assert "WHERE id=$1 AND user_id=$2 AND status IN ('approved','pending_review') RETURNING *" in sql
    assert params == [41, 8]


def test_listing_stats_report_views_contacts_and_unique_users():
    stats = {"views": 3, "unique_viewers": 2, "contacts": 4, "unique_contacts": 3}
    fake = fake_pool(fetchrow=AsyncMock(return_value=stats))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.listing_report(41))

    assert result == stats
    sql, listing_id = fake.fetchrow.await_args.args
    assert "COUNT(*) FILTER (WHERE event_type='VIEW') AS views" in sql
    assert "COUNT(DISTINCT user_id) FILTER (WHERE event_type='CONTACT'" in sql
    assert listing_id == 41


def test_existing_coupon_is_returned_without_creating_a_second_code():
    existing = {
        "campaign_id": 17, "user_id": 8, "code": "KHARTOUM-ABC12",
        "status": "unused", "expires_at": None,
    }

    class _AsyncContext:
        def __init__(self, value=None):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return False

    connection = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[{"end_date": None}, existing]),
        transaction=lambda: _AsyncContext(),
    )
    fake = SimpleNamespace(acquire=lambda: _AsyncContext(connection))
    with patch.object(db, "pool", return_value=fake), patch.object(db, "_gen_code", side_effect=AssertionError("must not generate")):
        result = asyncio.run(db.create_discount_code(17, 8, "KHARTOUM"))

    assert result == existing
    assert connection.fetchrow.await_count == 2
    assert "start_date" in connection.fetchrow.await_args_list[0].args[0]
    assert connection.fetchrow.await_args_list[1].args == (
        "SELECT * FROM discount_codes WHERE campaign_id=$1 AND user_id=$2", 17, 8
    )


def test_coupon_redemption_is_one_time_and_records_its_event():
    redeemed = {"campaign_id": 17, "user_id": 8, "code": "KHARTOUM-ABC12", "status": "redeemed"}
    class _AsyncContext:
        def __init__(self, value=None):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return False

    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value=redeemed), execute=AsyncMock(),
        transaction=lambda: _AsyncContext(),
    )
    fake = SimpleNamespace(acquire=lambda: _AsyncContext(connection))
    with (
        patch.object(db, "pool", return_value=fake),
    ):
        result = asyncio.run(db.redeem_code("KHARTOUM-ABC12"))

    assert result == redeemed
    sql, *params = connection.fetchrow.await_args.args
    assert "SET status='redeemed'" in sql
    assert "status='unused'" in sql
    assert "expires_at IS NULL OR expires_at > now()" in sql
    assert "AT TIME ZONE 'UTC'" in sql
    assert params == ["KHARTOUM-ABC12"]
    connection.execute.assert_awaited_once_with(
        "INSERT INTO campaign_events (campaign_id, user_id, event_type) VALUES ($1, $2, 'REDEEMED')",
        17, 8,
    )


def test_resubmission_is_restricted_to_owner_rejected_status_and_active_city():
    fake = fake_pool(fetchrow=AsyncMock(return_value={"id": 41, "status": "pending_review"}))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.resubmit_listing(
            41, 8, "هاتف محدث", "تفاصيل", "150000", "مستعمل", 3, "إلكترونيات",
            "بحري", "0911111111", "توصيل", True, ["new-photo", "second-photo", "ignored"],
        ))

    assert result["status"] == "pending_review"
    sql, *params = fake.fetchrow.await_args.args
    assert "l.user_id=$2" in sql and "l.status='rejected'" in sql
    assert "ci.id=$7 AND ci.status='active'" in sql
    assert "image_file_ids=$13" in sql
    assert "expires_at=now()+interval '72 hours'" in sql
    assert params[0:7] == [41, 8, "هاتف محدث", "تفاصيل", 150000.0, "مستعمل", 3]
    assert params[-1] == ["new-photo", "second-photo"]


def test_new_listing_insert_requires_an_active_structured_city():
    fake = fake_pool(fetchrow=AsyncMock(return_value={"id": 52, "city_id": 3}))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.create_listing(
            8, "هاتف", "تفاصيل", "125000", "مستعمل", 3, "إلكترونيات", "بحري",
            "0912345678", "متاح", True, ["photo"], submission_key="attempt-1",
        ))

    assert result["city_id"] == 3
    sql, *params = fake.fetchrow.await_args.args
    assert "FROM cities ci WHERE ci.id=$6 AND ci.status='active'" in sql
    assert "ON CONFLICT (user_id, submission_key)" in sql
    assert params[5] == 3
    assert params[-1] == "attempt-1"


def test_public_listing_lookup_filters_by_city_id():
    fake = fake_pool(fetch=AsyncMock(return_value=[LISTING]))
    with patch.object(db, "pool", return_value=fake), patch.object(db, "expire_listings", new=AsyncMock()):
        rows = asyncio.run(db.list_public_listings(city_id=3))

    assert rows == [LISTING]
    sql, *params = fake.fetch.await_args.args
    assert "l.city_id=$1" in sql
    assert params == [3]


def test_public_listing_contact_lookup_requires_active_unexpired_ad():
    fake = fake_pool(fetchrow=AsyncMock(return_value=LISTING))
    with patch.object(db, "pool", return_value=fake):
        result = asyncio.run(db.get_public_listing(41))

    assert result == LISTING
    sql, listing_id = fake.fetchrow.await_args.args
    assert "l.status='approved'" in sql and "l.expires_at > now()" in sql
    assert listing_id == 41


def test_owner_listing_pages_use_a_stable_cursor_and_keep_owner_scope():
    rows = [{"id": listing_id} for listing_id in range(80, 90)]
    fake = fake_pool(fetch=AsyncMock(return_value=rows))
    with patch.object(db, "pool", return_value=fake), patch.object(db, "expire_listings", new=AsyncMock()):
        page, has_previous, has_next = asyncio.run(
            db.list_user_listings_page(8, cursor_id=90, direction="next", limit=10)
        )

    assert page == rows
    assert has_previous is True and has_next is False
    sql, *params = fake.fetch.await_args.args
    assert "l.user_id=$1" in sql and "l.id<$2" in sql
    assert "ORDER BY l.id DESC" in sql
    assert params == [8, 90, 11]


def test_previous_owner_page_is_returned_in_display_order():
    rows = [{"id": listing_id} for listing_id in range(81, 92)]
    fake = fake_pool(fetch=AsyncMock(return_value=rows))
    with patch.object(db, "pool", return_value=fake), patch.object(db, "expire_listings", new=AsyncMock()):
        page, has_previous, has_next = asyncio.run(
            db.list_user_listings_page(8, cursor_id=80, direction="prev", limit=10)
        )

    assert [row["id"] for row in page] == list(range(90, 80, -1))
    assert has_previous is True and has_next is True
    sql, *params = fake.fetch.await_args.args
    assert "l.id>$2" in sql and "ORDER BY l.id ASC" in sql
    assert params == [8, 80, 11]


def test_buyer_listing_card_shows_contact_number_and_structured_city():
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=900))
    context = SimpleNamespace(user_data={"db_user_id": 99}, bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()))
    row = {**LISTING, "city_name": "بحري", "image_file_ids": []}
    with patch.object(marketplace.db, "log_listing_views", new=AsyncMock()):
        asyncio.run(marketplace._send_results(update, context, [row]))

    text = context.bot.send_message.await_args.kwargs["text"] if "text" in context.bot.send_message.await_args.kwargs else context.bot.send_message.await_args.args[1]
    assert "0912345678" in text
    assert "المدينة: بحري" in text


def test_buyer_search_passes_selected_city_to_database():
    row = {**LISTING, "city_name": "بحري", "image_file_ids": []}
    update = SimpleNamespace(
        message=SimpleNamespace(text="هاتف", reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=900),
    )
    context = SimpleNamespace(
        user_data={"db_user_id": 99, "market_search_city_id": 3, "market_search_city_name": "بحري"},
        bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()),
    )
    with (
        patch.object(marketplace.db, "list_public_listings", new=AsyncMock(return_value=[row])) as list_rows,
        patch.object(marketplace.ai, "rank_listing_ids", new=AsyncMock(return_value=[41])),
        patch.object(marketplace.db, "log_listing_views", new=AsyncMock()),
    ):
        asyncio.run(marketplace.buy_query(update, context))

    list_rows.assert_awaited_once_with(city_id=3)
    assert "0912345678" in context.bot.send_message.await_args.args[1]


def _food_and_vehicle_listings():
    return [
        {
            "id": 51,
            "title": "تويوتا كورولا",
            "description": "سيارة مستعملة بحالة جيدة",
            "category": "مركبات",
            "price": 1000,
            "negotiable": False,
            "address": "بحري",
            "contact": "0911111111",
            "delivery": "غير متاح",
            "image_file_ids": [],
        },
        {
            "id": 52,
            "title": "وجبة كسرة ودجاج",
            "description": "أكل سوداني طازج",
            "category": "مأكولات",
            "price": 500,
            "negotiable": True,
            "address": "الخرطوم",
            "contact": "0922222222",
            "delivery": "متاح",
            "image_file_ids": [],
        },
    ]


def test_local_food_search_excludes_vehicle_and_keeps_food_listing():
    assert ai.rank_listing_ids_locally("مأكولات", _food_and_vehicle_listings()) == [52]


def test_failed_ai_ranker_falls_back_to_only_relevant_ids():
    failing_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("AI unavailable")))
        )
    )
    with patch.object(ai, "client", return_value=failing_client):
        result = asyncio.run(ai.rank_listing_ids("مأكولات", _food_and_vehicle_listings()))

    assert result == [52]


def test_successful_ai_ranker_cannot_reintroduce_irrelevant_or_unknown_ids():
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ids":[51,52,999]}'))]
    )
    working_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=response))
        )
    )
    with patch.object(ai, "client", return_value=working_client):
        result = asyncio.run(ai.rank_listing_ids("مأكولات", _food_and_vehicle_listings()))

    assert result == [52]


def test_buyer_search_rejects_unrelated_ai_result_and_shows_no_match():
    listings = [_food_and_vehicle_listings()[0]]
    update = SimpleNamespace(
        message=SimpleNamespace(text="مأكولات", reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=900),
    )
    context = SimpleNamespace(
        user_data={"db_user_id": 99, "market_search_city_id": None, "market_search_city_name": "كل المدن"},
        bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()),
    )
    with (
        patch.object(marketplace.db, "list_public_listings", new=AsyncMock(return_value=listings)),
        patch.object(marketplace.ai, "rank_listing_ids", new=AsyncMock(return_value=[51])),
        patch.object(marketplace.db, "log_listing_views", new=AsyncMock()) as log_views,
    ):
        asyncio.run(marketplace.buy_query(update, context))

    assert context.bot.send_message.await_count == 0
    log_views.assert_not_awaited()
    reply = update.message.reply_text.await_args
    assert "لم نجد إعلانًا مطابقًا" in reply.args[0]
    callbacks = {
        button.callback_data
        for row in reply.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert {"market_new_search", "market_buy", "main_menu"}.issubset(callbacks)


@pytest.mark.parametrize("failure", [asyncio.TimeoutError, RuntimeError])
def test_buyer_search_ranking_failure_fallback_shows_only_relevant_results(failure):
    listings = _food_and_vehicle_listings()
    update = SimpleNamespace(
        message=SimpleNamespace(text="مأكولات", reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=900),
    )
    context = SimpleNamespace(
        user_data={"db_user_id": 99, "market_search_city_id": None, "market_search_city_name": "كل المدن"},
        bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()),
    )
    with (
        patch.object(marketplace.db, "list_public_listings", new=AsyncMock(return_value=listings)),
        patch.object(marketplace.ai, "rank_listing_ids", new=AsyncMock(side_effect=failure("ranking failed"))),
        patch.object(marketplace.db, "log_listing_views", new=AsyncMock()),
    ):
        asyncio.run(marketplace.buy_query(update, context))

    shown_texts = [call.args[1] for call in context.bot.send_message.await_args_list]
    assert len(shown_texts) == 1
    assert "وجبة كسرة ودجاج" in shown_texts[0]
    assert "تويوتا كورولا" not in shown_texts[0]


def test_empty_buyer_search_keeps_new_search_and_city_change_actions():
    update = SimpleNamespace(
        message=SimpleNamespace(text="دراجة", reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=900),
    )
    context = SimpleNamespace(
        user_data={"db_user_id": 99, "market_search_city_id": 3, "market_search_city_name": "بحري"},
        bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()),
    )
    with (
        patch.object(marketplace.db, "list_public_listings", new=AsyncMock(return_value=[])),
        patch.object(marketplace.ai, "rank_listing_ids", new=AsyncMock(return_value=[])),
    ):
        state = asyncio.run(marketplace.buy_query(update, context))

    assert state == marketplace.MARKET_MENU
    reply = update.message.reply_text.await_args
    callbacks = {
        button.callback_data
        for row in reply.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert {"market_new_search", "market_buy"}.issubset(callbacks)
    assert "main_menu" in callbacks


def test_more_results_keeps_pagination_and_search_actions_available():
    row = {**LISTING, "id": 6, "city_name": "بحري", "image_file_ids": []}
    query = SimpleNamespace(
        data="market_more_012345abcdef",
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=900))
    context = SimpleNamespace(
        user_data={
            "db_user_id": 99,
            "market_results": list(range(1, 12)),
            "market_offset": 5,
            "market_search_city_id": 3,
            "market_search_id": "012345abcdef",
        },
        bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()),
    )
    with (
        patch.object(marketplace.db, "list_public_listings", new=AsyncMock(return_value=[row])),
        patch.object(marketplace.db, "log_listing_views", new=AsyncMock()),
    ):
        state = asyncio.run(marketplace.more_results(update, context))

    assert state == marketplace.MARKET_MENU
    assert context.user_data["market_offset"] == 10
    callbacks = {
        button.callback_data
        for row_buttons in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row_buttons
    }
    assert {"market_more_012345abcdef", "market_new_search", "market_buy"}.issubset(callbacks)
    assert "main_menu" in callbacks


def test_rejected_listing_can_be_edited_and_resubmitted_end_to_end():
    async def scenario():
        context = SimpleNamespace(args=["41"], user_data={}, bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()))
        start_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1234),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        with (
            patch.object(marketplace.db, "get_user_by_telegram_id", new=AsyncMock(return_value={"id": 8})),
            patch.object(marketplace.db, "get_listing", new=AsyncMock(return_value={**LISTING, "status": "rejected"})),
            patch.object(marketplace.db, "list_cities", new=AsyncMock(return_value=[CITY])),
        ):
            state = await marketplace.edit_listing_start(start_update, context)
        assert state == marketplace.EDIT_LISTING_CITY
        assert context.user_data["db_user_id"] == 8

        city_query = SimpleNamespace(
            data="market_editcity_3", answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        with patch.object(marketplace.db, "list_cities", new=AsyncMock(return_value=[CITY])):
            state = await marketplace.edit_listing_city_chosen(SimpleNamespace(callback_query=city_query), context)
        assert state == marketplace.EDIT_LISTING_MESSAGE
        assert context.user_data["edit_listing_city_id"] == 3

        message = SimpleNamespace(
            photo=[SimpleNamespace(file_id="fresh-photo")],
            caption="هاتف محدث في بحري، تواصل 0911111111",
            reply_text=AsyncMock(),
        )
        with (
            patch.object(marketplace.ai, "extract_listing_data", new=AsyncMock(return_value={
                "title": "هاتف محدث", "description": "تفاصيل محدثة", "price": "150000",
                "condition": "مستعمل", "address": "بحري", "contact": "0911111111",
                "delivery": "متاح", "negotiable": True, "category": "إلكترونيات", "issues": [],
            })),
            patch.object(marketplace.ai, "review_listing", new=AsyncMock(return_value={"title": "هاتف محدث", "description": "تفاصيل محدثة", "issues": []})),
            patch.object(marketplace.db, "list_cities", new=AsyncMock(return_value=[CITY])),
        ):
            state = await marketplace.sell_message(SimpleNamespace(message=message), context)
        assert state == marketplace.EDIT_LISTING_CONFIRM
        assert context.user_data["market_draft"]["city_id"] == 3
        assert "مدة الإعلان 72 ساعة تبدأ بعد الاعتماد" in message.reply_text.await_args.args[0]

        submit_query = SimpleNamespace(
            data=f"listing_{context.user_data['market_preview_id']}_submit",
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        with patch.object(marketplace.db, "resubmit_listing", new=AsyncMock(return_value={"id": 41})) as resubmit:
            state = await marketplace.sell_confirm(SimpleNamespace(callback_query=submit_query), context)
        assert state == marketplace.MARKET_MENU
        args = resubmit.await_args.args
        assert args[0:3] == (41, 8, "هاتف محدث")
        assert args[6] == 3
        assert args[-1] == ["fresh-photo"]
        assert "أُعيد إرسال الإعلان #41" in submit_query.edit_message_text.await_args.args[0]

    asyncio.run(scenario())


def test_listing_text_is_sent_even_when_telegram_cannot_send_its_photo():
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=900))
    context = SimpleNamespace(
        user_data={"db_user_id": 99},
        bot=SimpleNamespace(
            send_photo=AsyncMock(side_effect=RuntimeError("invalid file reference")),
            send_message=AsyncMock(),
        ),
    )
    row = {**LISTING, "image_file_ids": ["expired-telegram-id"]}
    with patch.object(marketplace.db, "log_listing_views", new=AsyncMock()):
        asyncio.run(marketplace._send_results(update, context, [row]))

    context.bot.send_photo.assert_awaited_once_with(900, "expired-telegram-id")
    assert context.bot.send_message.await_count == 1
    assert "هاتف" in context.bot.send_message.await_args.args[1]
