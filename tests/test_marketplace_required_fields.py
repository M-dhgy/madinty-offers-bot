import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import marketplace
import pytest


CITY = {"id": 3, "name": "بحري", "code": "BAHRI"}


def test_complete_listing_advances_despite_conflicting_ai_issues():
    parsed = {
        "title": "هاتف Samsung A54",
        "description": "مستعمل بحالة ممتازة",
        "price": "800000",
        "address": "بحري السوق العربي",
        "contact": "0912345678",
        "delivery": "لا توجد خدمة توصيل",
        "condition": "مستعمل",
        "issues": ["رقم التواصل ناقص", "العنوان غير موجود"],
    }
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo-id")],
        caption="هاتف Samsung A54. الموقع: بحري السوق العربي. التواصل: 0912345678",
        reply_text=AsyncMock(),
    )
    context = SimpleNamespace(
        user_data={"db_user_id": 8, "market_city_id": 3},
    )

    with (
        patch.object(marketplace.ai, "extract_listing_data", new=AsyncMock(return_value=parsed)),
        patch.object(marketplace.ai, "review_listing", new=AsyncMock(return_value={
            "title": parsed["title"],
            "description": parsed["description"],
            "issues": ["رقم التواصل غير موجود"],
        })) as review,
        patch.object(marketplace.db, "list_cities", new=AsyncMock(return_value=[CITY])),
    ):
        state = asyncio.run(marketplace.sell_message(SimpleNamespace(message=message), context))

    assert state == marketplace.SELL_CONFIRM
    assert context.user_data["market_draft"]["contact"] == "0912345678"
    review.assert_awaited_once_with(parsed)
    assert "راجع إعلانك قبل إرساله" in message.reply_text.await_args.args[0]


def test_truly_missing_required_field_still_prompts_seller_and_does_not_review():
    parsed = {
        "title": "هاتف Samsung A54",
        "description": "مستعمل بحالة ممتازة",
        "address": "بحري السوق العربي",
        "contact": "   ",
        "issues": [],
    }
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo-id")],
        caption="هاتف Samsung A54، الموقع بحري السوق العربي",
        reply_text=AsyncMock(),
    )
    context = SimpleNamespace(user_data={"db_user_id": 8, "market_city_id": 3})

    with (
        patch.object(marketplace.ai, "extract_listing_data", new=AsyncMock(return_value=parsed)),
        patch.object(marketplace.ai, "review_listing", new=AsyncMock()) as review,
    ):
        state = asyncio.run(marketplace.sell_message(SimpleNamespace(message=message), context))

    assert state == marketplace.SELL_MESSAGE
    assert "رقم التواصل" in message.reply_text.await_args.args[0]
    review.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "invalid_value", "label"),
    [
        ("title", 123, "اسم الغرض"),
        ("title", ["هاتف"], "اسم الغرض"),
        ("address", {"city": "بحري"}, "العنوان والموقع"),
        ("contact", True, "رقم التواصل"),
        ("contact", ["0912345678"], "رقم التواصل"),
    ],
)
def test_non_string_required_ai_fields_are_rejected(field, invalid_value, label):
    parsed = {
        "title": "هاتف",
        "address": "بحري",
        "contact": "0912345678",
        "issues": [],
    }
    parsed[field] = invalid_value
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo-id")],
        caption="بيانات إعلان",
        reply_text=AsyncMock(),
    )
    context = SimpleNamespace(user_data={"db_user_id": 8, "market_city_id": 3})
    with (
        patch.object(marketplace.ai, "extract_listing_data", new=AsyncMock(return_value=parsed)),
        patch.object(marketplace.ai, "review_listing", new=AsyncMock()) as review,
    ):
        state = asyncio.run(marketplace.sell_message(SimpleNamespace(message=message), context))

    assert state == marketplace.SELL_MESSAGE
    assert label in message.reply_text.await_args.args[0]
    review.assert_not_awaited()
