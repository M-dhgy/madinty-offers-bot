"""Integration test for PostgreSQL schema, campaigns, codes, and listings.

The test uses only a dedicated database whose name ends in ``_test``. It is
skipped when DATABASE_URL is not configured, while CI always supplies it.
"""

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest
from dotenv import load_dotenv

import db


ROOT = Path(__file__).resolve().parents[1]


def _database_url() -> str:
    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("Set DATABASE_URL to run PostgreSQL integration tests")

    database_name = unquote(urlsplit(dsn).path.rsplit("/", 1)[-1])
    if not database_name.endswith("_test"):
        pytest.fail(
            "PostgreSQL integration tests require a dedicated database whose "
            "name ends with '_test'; refusing to touch another database"
        )
    return dsn


async def _campaign_lifecycle(dsn: str) -> None:
    schema_sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(schema_sql)
    finally:
        await connection.close()

    await db.init_pool()
    user_id = None
    try:
        cities = await db.list_cities()
        categories = await db.list_categories()
        city = next(row for row in cities if row["code"] == "KHARTOUM")
        category = next(row for row in categories if row["code"] == "FOOD")

        telegram_id = time.time_ns() // 1_000
        user = await db.create_user(telegram_id, "db_integration_test", "اختبار")
        user_id = user["id"]

        listing = await db.create_listing(
            user_id, "إعلان اختبار", "تفاصيل", "5", "جيد", city["id"],
            "عام", "الخرطوم", "0900000000", "لا", False, ["telegram-file-id"],
        )
        assert listing["image_file_ids"] == ["telegram-file-id"]
        await db.pool().execute(
            "UPDATE listings SET status='approved', expires_at=now()-interval '1 second' WHERE id=$1",
            listing["id"],
        )
        await db.expire_listings()
        expired_listing = await db.get_listing(listing["id"])
        assert expired_listing["status"] == "expired"
        assert expired_listing["image_file_ids"] == []
        # Simulate an already-expired row created before photo-reference cleanup.
        await db.pool().execute(
            "UPDATE listings SET image_file_ids=ARRAY['legacy-telegram-file-id'] WHERE id=$1",
            listing["id"],
        )
        await db.expire_listings()
        cleaned_legacy_listing = await db.get_listing(listing["id"])
        assert cleaned_legacy_listing["image_file_ids"] == []

        lifecycle_listing = await db.create_listing(
            user_id, "إعلان دورة الحياة", "تفاصيل", "25", "جيد", city["id"],
            "عام", "الخرطوم", "0900000000", "لا", False, ["fresh-photo-reference"],
        )
        first_approval = await db.set_listing_status(lifecycle_listing["id"], "approved")
        assert first_approval["status"] == "approved"
        renewed_listing = await db.renew_listing(lifecycle_listing["id"], user_id)
        assert renewed_listing["status"] == "pending_review"
        assert renewed_listing["image_file_ids"] == ["fresh-photo-reference"]
        second_approval = await db.set_listing_status(lifecycle_listing["id"], "approved")
        assert second_approval["status"] == "approved"
        await db.log_listing_event(lifecycle_listing["id"], user_id, "VIEW")
        await db.log_listing_event(lifecycle_listing["id"], user_id, "VIEW")
        await db.log_listing_event(lifecycle_listing["id"], user_id, "CONTACT")
        await db.log_listing_event(lifecycle_listing["id"], user_id, "CONTACT")
        listing_stats = await db.listing_report(lifecycle_listing["id"])
        assert listing_stats == {
            "views": 1, "unique_viewers": 1, "contacts": 2, "unique_contacts": 1
        }
        sold_listing = await db.mark_listing_sold(lifecycle_listing["id"], user_id)
        assert sold_listing["status"] == "sold"
        assert (await db.get_listing(lifecycle_listing["id"]))["status"] == "sold"

        business = await db.create_business(
            user_id, "نشاط اختبار تكامل", "مطعم", city["id"], "0900000000", is_paid=True
        )
        assert business["status"] == "pending"

        approved_business = await db.set_business_status(business["id"], "approved")
        assert approved_business["status"] == "approved"

        campaign = await db.create_campaign(
            business["id"],
            "اختبار حملة",
            {"title": "اختبار حملة", "target": "الجميع", "discount": 10},
            "عرض اختبار تكامل قاعدة البيانات",
            category["id"],
            city["id"],
        )
        assert campaign["status"] == "pending_review"

        approved_campaign = await db.review_campaign(campaign["id"], "approve")
        assert approved_campaign["status"] == "approved"

        claim = await db.claim_campaign_for_broadcast(campaign["id"])
        duplicate_claim = await db.claim_campaign_for_broadcast(campaign["id"])
        assert claim["status"] == "sending"
        assert duplicate_claim is None

        finished = await db.finish_campaign_broadcast(campaign["id"], succeeded=True)
        assert finished["status"] == "active"
        assert finished["published_at"] is not None

        code = await db.create_discount_code(campaign["id"], user_id, city["code"])
        duplicate_code = await db.create_discount_code(campaign["id"], user_id, city["code"])
        assert duplicate_code["code"] == code["code"]
        assert await db.redeem_code_for_business(code["code"], business["id"] + 1000000) is None
        redeemed_code = await db.redeem_code_for_business(code["code"], business["id"])
        assert redeemed_code["status"] == "redeemed"
        assert redeemed_code["redeemed_at"] is not None
        assert await db.redeem_code_for_business(code["code"], business["id"]) is None

        await db.log_event(campaign["id"], user_id, "SENT")
        report = await db.campaign_report(campaign["id"])
        assert report["sent"] == 1
        assert report["codes"] == 1
        assert report["redeemed"] == 1
    finally:
        try:
            if user_id is not None:
                await db.pool().execute("DELETE FROM users WHERE id=$1", user_id)
        finally:
            await db.close_pool()


def test_postgres_schema_and_campaign_lifecycle():
    asyncio.run(_campaign_lifecycle(_database_url()))
