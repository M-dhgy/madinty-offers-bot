import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

import bot
import db


def _fake_pool(**overrides):
    values = {
        "fetch": AsyncMock(return_value=[]),
        "fetchrow": AsyncMock(return_value={"id": 9, "status": "pending_review"}),
        "fetchval": AsyncMock(return_value=False),
        "execute": AsyncMock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_create_campaign_persists_valid_start_and_end_dates():
    fake = _fake_pool()
    start = date(2027, 2, 3)
    end = date(2027, 2, 11)
    with patch.object(db, "pool", return_value=fake):
        campaign = asyncio.run(db.create_campaign(
            4,
            "raw",
            {"title": "عرض", "start": "2027-02-03", "end": "2027-02-11"},
            "ad",
            2,
            3,
            submission_key="date-case",
        ))

    assert campaign["id"] == 9
    query, *params = fake.fetchrow.await_args.args
    assert "start_date" in query and "end_date" in query
    assert params[-3:-1] == [start, end]
    assert params[-1] == "date-case"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2027-02-30", None),
        ("not-a-date", "2027-03-01"),
        ("2027-03-02", "2027-03-01"),
        ("2027-01-01T12:00:00", None),
    ],
)
def test_create_campaign_rejects_invalid_or_reversed_dates_before_database_write(start, end):
    fake = _fake_pool()
    with patch.object(db, "pool", return_value=fake):
        with pytest.raises(ValueError, match="campaign date"):
            asyncio.run(db.create_campaign(
                4,
                "raw",
                {"title": "عرض", "start": start, "end": end},
                "ad",
                2,
                3,
            ))

    fake.fetchrow.assert_not_awaited()


def test_campaign_discovery_uses_inclusive_utc_date_window():
    fake = _fake_pool()
    with patch.object(db, "pool", return_value=fake):
        asyncio.run(db.list_active_campaigns_for_user(7))

    discovery_sql = fake.fetch.await_args.args[0]
    assert "start_date" in discovery_sql and "end_date" in discovery_sql
    assert "AT TIME ZONE 'UTC'" in discovery_sql


@pytest.mark.parametrize(
    ("start", "end"),
    [("", ""), ("null", "None"), ("   ", None), ("NULL", " none ")],
)
def test_campaign_date_validator_normalizes_absent_values(start, end):
    assert db.validate_campaign_dates(start, end) == (None, None)


def test_campaign_preview_never_displays_null_like_dates():
    pending = {
        "ai_data": {"start": "null", "end": "None", "discount": 15},
        "ad_text": "عرض تجريبي",
    }

    preview = bot._campaign_preview_text(pending)

    assert "دون تاريخ انتهاء محدد" in preview
    assert "null" not in preview.lower()
    assert "none" not in preview.lower()


def test_campaign_preview_formats_valid_and_undated_campaigns():
    dated = bot._campaign_preview_text({
        "ai_data": {"start": "2027-02-03", "end": "2027-02-11", "discount": 10},
        "ad_text": "عرض مؤرخ",
    })
    undated = bot._campaign_preview_text({
        "ai_data": {"start": None, "end": None, "discount": 10},
        "ad_text": "عرض بلا تاريخ",
    })

    assert "2027-02-03 إلى 2027-02-11 (شاملًا)" in dated
    assert "دون تاريخ انتهاء محدد" in undated
    assert "null" not in undated.lower() and "none" not in undated.lower()


def test_create_campaign_saves_null_like_dates_as_sql_null():
    fake = _fake_pool()
    with patch.object(db, "pool", return_value=fake):
        asyncio.run(db.create_campaign(
            4, "raw", {"title": "عرض", "start": "null", "end": "   "},
            "ad", 2, 3,
        ))

    params = fake.fetchrow.await_args.args[1:]
    assert params[8:10] == (None, None)


def test_campaign_description_normalizes_ai_dates_before_validation_and_preview():
    data = {
        "title": "عرض بلا موعد", "start": "null", "end": "   ",
        "discount": 10, "category_code": "FOOD", "city_code": "KHARTOUM",
        "target": None, "issues": [],
    }
    preview_message = SimpleNamespace(message_id=1)
    reply_text = AsyncMock(side_effect=[None, preview_message])
    update = SimpleNamespace(
        message=SimpleNamespace(text="عرض تجريبي", reply_text=reply_text),
        effective_chat=SimpleNamespace(id=7001),
    )
    context = SimpleNamespace(user_data={"db_user_id": 17})
    fake = _fake_pool(fetchrow=AsyncMock(side_effect=[{"id": 2}, {"id": 3}]))
    validator = Mock(wraps=db.validate_campaign_dates)

    with (
        patch.object(bot.db, "get_business_by_user", new=AsyncMock(return_value={"id": 9, "status": "approved"})),
        patch.object(bot.db, "pool", return_value=fake),
        patch.object(bot.db, "validate_campaign_dates", new=validator),
        patch.object(bot.db, "validate_campaign_target", new=AsyncMock(return_value=True)),
        patch.object(bot.ai, "extract_campaign_data", new=AsyncMock(return_value=data)),
        patch.object(bot.ai, "generate_ad_copy", new=AsyncMock(return_value="نص العرض")),
        patch.object(bot, "_clear_campaign_preview", new=AsyncMock()),
        patch.object(bot, "_callback_message_reference", return_value=None),
    ):
        state = asyncio.run(bot.campaign_description_received(update, context))

    assert state == bot.CAMPAIGN_CONFIRM
    assert validator.call_args_list[0].args == (None, None)
    assert context.user_data["pending_campaign"]["ai_data"]["start"] is None
    assert context.user_data["pending_campaign"]["ai_data"]["end"] is None
    preview = reply_text.await_args_list[1].args[0]
    assert "دون تاريخ انتهاء محدد" in preview
    assert "null" not in preview.lower() and "none" not in preview.lower()
