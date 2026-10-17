import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import ai
import db
import marketplace


def test_listing_review_fails_closed_when_ai_client_raises(monkeypatch):
    def unavailable_client():
        raise RuntimeError("AI service unavailable")

    monkeypatch.setattr(ai, "client", unavailable_client)

    result = asyncio.run(ai.review_listing({"title": "Phone"}))

    assert result["ai_unavailable"] is True
    assert result["issues"]


def test_marketplace_requires_description_and_price(monkeypatch):
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo-id")],
        caption="هاتف بحالة جيدة",
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(user_data={})
    extract = AsyncMock(return_value={
        "title": "هاتف",
        "description": "",
        "price": None,
        "address": "بحري",
        "contact": "0912345678",
        "issues": [],
    })
    review = AsyncMock()
    monkeypatch.setattr(marketplace.ai, "extract_listing_data", extract)
    monkeypatch.setattr(marketplace.ai, "review_listing", review)

    result = asyncio.run(marketplace.sell_message(update, context))

    assert result == marketplace.SELL_MESSAGE
    response = message.reply_text.await_args.args[0]
    assert "وصف الغرض" in response
    assert "السعر المحدد" in response
    review.assert_not_awaited()


def test_marketplace_blocks_listing_when_ai_review_is_unavailable(monkeypatch):
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo-id")],
        caption="هاتف مستعمل، وصفه ممتاز، السعر 1000، الموقع بحري، التواصل 0912345678",
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(user_data={})
    monkeypatch.setattr(marketplace.ai, "extract_listing_data", AsyncMock(return_value={
        "title": "هاتف",
        "description": "هاتف مستعمل بحالة ممتازة",
        "price": "1000",
        "address": "بحري",
        "contact": "0912345678",
        "issues": [],
    }))
    monkeypatch.setattr(marketplace.ai, "review_listing", AsyncMock(return_value={
        "ai_unavailable": True,
        "issues": ["تعذر التحقق من الإعلان"],
    }))

    result = asyncio.run(marketplace.sell_message(update, context))

    assert result == marketplace.SELL_MESSAGE
    assert "market_draft" not in context.user_data
    assert "تعذر التحقق من الإعلان" in message.reply_text.await_args.args[0]


def test_find_or_create_city_creates_a_named_custom_city(monkeypatch):
    class FakePool:
        def __init__(self):
            self.inserted = None

        async def fetchrow(self, query, *args):
            if query.lstrip().startswith("SELECT id, name, code FROM cities"):
                return None
            if query.lstrip().startswith("INSERT INTO cities"):
                self.inserted = args
                return {"id": 77, "name": args[0], "code": args[1]}
            raise AssertionError(f"Unexpected query: {query}")

    fake_pool = FakePool()
    monkeypatch.setattr(db, "pool", lambda: fake_pool)

    result = asyncio.run(db.find_or_create_city("  مدينة   جديدة  "))

    assert result["id"] == 77
    assert result["name"] == "مدينة جديدة"
    assert fake_pool.inserted[1].startswith("CUSTOM_")
