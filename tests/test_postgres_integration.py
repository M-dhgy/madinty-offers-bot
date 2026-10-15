"""Integration test for PostgreSQL schema, campaigns, codes, and listings.

The test uses only a dedicated database whose name ends in ``_test``. It is
skipped when DATABASE_URL is not configured, while CI always supplies it.
"""

import asyncio
import os
import time
from datetime import timedelta
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

    parsed = urlsplit(dsn)
    database_name = unquote(parsed.path.rsplit("/", 1)[-1])
    if not database_name.endswith("_test"):
        pytest.fail(
            "PostgreSQL integration tests require a dedicated database whose "
            "name ends with '_test'; refusing to touch another database"
        )
    if parsed.hostname not in {None, "", "localhost", "127.0.0.1", "::1"}:
        pytest.fail("PostgreSQL integration tests are restricted to a local server")
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
        await db.set_account_type(user_id, "customer")
        await db.set_user_city(user_id, city["id"])
        await db.set_user_categories(user_id, [category["id"]])

        listing = await db.create_listing(
            user_id, "إعلان اختبار", "تفاصيل", "5", "جيد", city["id"],
            "عام", "الخرطوم", "0900000000", "لا", False, ["telegram-file-id"],
            submission_key="integration-listing-attempt",
        )
        assert listing["image_file_ids"] == ["telegram-file-id"]
        duplicate_listing = await db.create_listing(
            user_id, "عنوان لا يجب أن يستبدل الأصل", "تفاصيل", "5", "جيد", city["id"],
            "عام", "الخرطوم", "0900000000", "لا", False, ["telegram-file-id"],
            submission_key="integration-listing-attempt",
        )
        assert duplicate_listing["id"] == listing["id"]
        assert duplicate_listing["title"] == listing["title"]
        independent_listing = await db.create_listing(
            user_id, "إعلان مستقل", "تفاصيل", "8", "جيد", city["id"],
            "عام", "الخرطوم", "0900000000", "لا", False, ["telegram-file-id"],
            submission_key="integration-listing-attempt-2",
        )
        assert independent_listing["id"] != listing["id"]
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
        concurrent_view_listing = await db.create_listing(
            user_id, "إعلان مشاهدات متزامنة", "تفاصيل", "20", "جيد", city["id"],
            "عام", "الخرطوم", "0900000000", "لا", False, ["view-photo"],
        )
        await db.set_listing_status(concurrent_view_listing["id"], "approved")
        await asyncio.gather(*(
            db.log_listing_views([concurrent_view_listing["id"]], user_id)
            for _ in range(48)
        ))
        concurrent_view_count = await db.pool().fetchval(
            """SELECT COUNT(*) FROM listing_events
               WHERE listing_id=$1 AND user_id=$2 AND event_type='VIEW'
                 AND (created_at AT TIME ZONE 'UTC')::date=(now() AT TIME ZONE 'UTC')::date""",
            concurrent_view_listing["id"], user_id,
        )
        assert concurrent_view_count == 1

        anonymous_view_listing = await db.create_listing(
            user_id, "إعلان مشاهدات مجهولة", "تفاصيل", "20", "جيد", city["id"],
            "عام", "الخرطوم", "0900000000", "لا", False, ["anonymous-photo"],
        )
        await db.set_listing_status(anonymous_view_listing["id"], "approved")
        await asyncio.gather(*(
            db.log_listing_views([anonymous_view_listing["id"]], None)
            for _ in range(7)
        ))
        anonymous_view_count = await db.pool().fetchval(
            "SELECT COUNT(*) FROM listing_events WHERE listing_id=$1 AND user_id IS NULL AND event_type='VIEW'",
            anonymous_view_listing["id"],
        )
        assert anonymous_view_count == 7
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
            submission_key="integration-campaign-attempt",
        )
        assert campaign["status"] == "pending_review"
        assert campaign["_created"] is True
        duplicate_campaign = await db.create_campaign(
            business["id"],
            "عنوان لا يجب أن يستبدل الأصل",
            {"title": "عنوان آخر", "target": "الجميع", "discount": 10},
            "نص آخر",
            category["id"],
            city["id"],
            submission_key="integration-campaign-attempt",
        )
        assert duplicate_campaign["id"] == campaign["id"]
        assert duplicate_campaign["description"] == campaign["description"]
        assert duplicate_campaign["_created"] is False
        independent_campaign = await db.create_campaign(
            business["id"], "حملة مستقلة", {"title": "حملة مستقلة", "target": "الجميع", "discount": 5},
            "عرض مستقل", category["id"], city["id"],
            submission_key="integration-campaign-attempt-2",
        )
        assert independent_campaign["_created"] is True
        assert independent_campaign["id"] != campaign["id"]

        approved_campaign = await db.review_campaign(campaign["id"], "approve")
        assert approved_campaign["status"] == "approved"

        claim = await db.claim_campaign_for_broadcast(campaign["id"])
        duplicate_claim = await db.claim_campaign_for_broadcast(campaign["id"])
        assert claim["status"] == "sending"
        assert duplicate_claim is None
        pending_recipients = await db.list_pending_campaign_recipients(campaign["id"])
        assert [row["id"] for row in pending_recipients] == [user_id]
        assert await db.claim_campaign_recipient(campaign["id"], user_id)
        assert await db.record_campaign_recipient_result(campaign["id"], user_id, "sent")

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

        today_utc = await db.pool().fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")
        future_campaign = await db.create_campaign(
            business["id"], "future", {"title": "مستقبلية", "start": (today_utc + timedelta(days=1)).isoformat(),
                                          "end": (today_utc + timedelta(days=7)).isoformat()},
            "حملة مستقبلية", category["id"], city["id"], submission_key="integration-future",
        )
        ended_campaign = await db.create_campaign(
            business["id"], "ended", {"title": "منتهية", "start": (today_utc - timedelta(days=7)).isoformat(),
                                         "end": (today_utc - timedelta(days=1)).isoformat()},
            "حملة منتهية", category["id"], city["id"], submission_key="integration-ended",
        )
        ending_today_campaign = await db.create_campaign(
            business["id"], "ending-today", {"title": "تنتهي اليوم", "start": (today_utc - timedelta(days=1)).isoformat(),
                                               "end": today_utc.isoformat()},
            "حملة تنتهي اليوم", category["id"], city["id"], submission_key="integration-ending-today",
        )
        undated_campaign = await db.create_campaign(
            business["id"], "undated", {"title": "بلا تواريخ"}, "حملة بلا تواريخ",
            category["id"], city["id"], submission_key="integration-undated",
        )
        assert future_campaign["start_date"] == today_utc + timedelta(days=1)
        assert future_campaign["end_date"] == today_utc + timedelta(days=7)
        assert ended_campaign["start_date"] == today_utc - timedelta(days=7)
        assert ended_campaign["end_date"] == today_utc - timedelta(days=1)
        assert ending_today_campaign["end_date"] == today_utc
        assert undated_campaign["start_date"] is None
        assert undated_campaign["end_date"] is None
        await db.pool().execute(
            "UPDATE campaigns SET status='approved' WHERE id=ANY($1::integer[])",
            [future_campaign["id"], ended_campaign["id"]],
        )
        assert await db.claim_campaign_for_broadcast(future_campaign["id"]) is None
        assert await db.claim_campaign_for_broadcast(ended_campaign["id"]) is None
        await db.pool().execute(
            "UPDATE campaigns SET status='active' WHERE id=ANY($1::integer[])",
            [future_campaign["id"], ended_campaign["id"], ending_today_campaign["id"], undated_campaign["id"]],
        )
        active_ids = {
            row["id"] for row in await db.list_active_campaigns_for_user(user_id)
        }
        assert future_campaign["id"] not in active_ids
        assert ended_campaign["id"] not in active_ids
        assert ending_today_campaign["id"] in active_ids
        assert undated_campaign["id"] in active_ids
        assert await db.create_discount_code(future_campaign["id"], user_id, city["code"]) is None
        assert await db.create_discount_code(ended_campaign["id"], user_id, city["code"]) is None
        today_code = await db.create_discount_code(ending_today_campaign["id"], user_id, city["code"])
        expected_expiry = await db.pool().fetchval(
            "SELECT (($1::date + 1)::timestamp AT TIME ZONE 'UTC')", today_utc
        )
        assert today_code["expires_at"] == expected_expiry
        assert await db.redeem_code_for_business(today_code["code"], business["id"])
        undated_code = await db.create_discount_code(undated_campaign["id"], user_id, city["code"])
        assert undated_code and undated_code["expires_at"] is None

        retry_campaign = await db.create_campaign(
            business["id"], "retry", {"title": "استئناف"}, "حملة استئناف",
            category["id"], city["id"], submission_key="integration-retry",
        )
        assert (await db.review_campaign(retry_campaign["id"], "approve"))["status"] == "approved"
        assert await db.claim_campaign_for_broadcast(retry_campaign["id"])
        assert await db.claim_campaign_recipient(retry_campaign["id"], user_id)
        assert await db.record_campaign_recipient_result(
            retry_campaign["id"], user_id, "pending", "RetryAfter retry limit reached"
        )
        assert (await db.finish_campaign_broadcast(retry_campaign["id"], succeeded=False))["status"] == "broadcast_failed"
        assert (await db.claim_campaign_for_broadcast(retry_campaign["id"], resume=True))["status"] == "sending"
        retry_pending = await db.list_pending_campaign_recipients(retry_campaign["id"])
        assert [row["id"] for row in retry_pending] == [user_id]
        assert await db.claim_campaign_recipient(retry_campaign["id"], user_id)
        assert await db.record_campaign_recipient_result(retry_campaign["id"], user_id, "sent")
        assert (await db.finish_campaign_broadcast(retry_campaign["id"], succeeded=True))["status"] == "active"

        uncertain_campaign = await db.create_campaign(
            business["id"], "uncertain", {"title": "ملتبسة"}, "حملة ملتبسة",
            category["id"], city["id"], submission_key="integration-uncertain",
        )
        assert (await db.review_campaign(uncertain_campaign["id"], "approve"))["status"] == "approved"
        assert await db.claim_campaign_for_broadcast(uncertain_campaign["id"])
        assert await db.claim_campaign_recipient(uncertain_campaign["id"], user_id)
        await db.pool().execute(
            "UPDATE campaigns SET broadcast_started_at=now()-interval '6 minutes' WHERE id=$1",
            uncertain_campaign["id"],
        )
        assert (await db.claim_campaign_for_broadcast(uncertain_campaign["id"], resume=True))["status"] == "sending"
        uncertain_summary = await db.campaign_delivery_summary(uncertain_campaign["id"])
        assert uncertain_summary["uncertain"] == 1 and uncertain_summary["pending"] == 0
        assert await db.list_pending_campaign_recipients(uncertain_campaign["id"]) == []
        assert (await db.finish_campaign_broadcast(uncertain_campaign["id"], succeeded=False))["status"] == "broadcast_failed"

        telegram_update_id = time.time_ns()
        assert await db.claim_telegram_update(telegram_update_id, "integration-contact") is True
        receipt = await db.get_telegram_update_receipt(telegram_update_id)
        assert receipt["status"] == "processing" and receipt["stage"] == "claimed"
        assert await db.release_telegram_update(telegram_update_id, "integration-contact") is True
        assert await db.claim_telegram_update(telegram_update_id, "integration-contact") is True
        assert await db.log_listing_contact_for_update(
            lifecycle_listing["id"], user_id, telegram_update_id, "integration-contact"
        ) is True
        assert await db.log_listing_contact_for_update(
            lifecycle_listing["id"], user_id, telegram_update_id, "integration-contact"
        ) is False
        event_count = await db.pool().fetchval(
            "SELECT COUNT(*) FROM listing_events WHERE source_update_id=$1", telegram_update_id
        )
        assert event_count == 1
        assert await db.claim_telegram_update(telegram_update_id, "integration-contact") is False
        assert await db.set_telegram_update_stage(
            telegram_update_id, "integration-contact", "notification_attempting"
        ) is True
        await db.pool().execute(
            "UPDATE telegram_update_receipts SET lease_until=now()-interval '1 second' WHERE update_id=$1",
            telegram_update_id,
        )
        assert await db.claim_telegram_update(telegram_update_id, "integration-contact") is False
        receipt = await db.get_telegram_update_receipt(telegram_update_id)
        assert receipt["status"] == "uncertain" and receipt["stage"] == "uncertain"

        completed_update_id = telegram_update_id + 1
        assert await db.claim_telegram_update(completed_update_id, "integration-complete") is True
        assert await db.complete_telegram_update(completed_update_id, "integration-complete") is True
        assert await db.claim_telegram_update(completed_update_id, "integration-complete") is False

        details_retry_id = completed_update_id + 100
        assert await db.claim_telegram_update(details_retry_id, "listing_contact") is True
        assert await db.log_listing_contact_for_update(
            lifecycle_listing["id"], user_id, details_retry_id, "listing_contact"
        ) is True
        assert await db.set_telegram_update_stage(
            details_retry_id, "listing_contact", "notification_attempting"
        ) is True
        await db.pool().execute(
            "UPDATE telegram_update_receipts SET lease_until=now()-interval '1 second' WHERE update_id=$1",
            details_retry_id,
        )
        assert await db.claim_telegram_update(details_retry_id, "listing_contact") is True
        receipt = await db.get_telegram_update_receipt(details_retry_id)
        assert receipt["status"] == "processing" and receipt["stage"] == "notification_attempting"
        assert await db.release_telegram_update(details_retry_id, "listing_contact") is True
        assert await db.claim_telegram_update(details_retry_id, "listing_contact") is True
        assert await db.log_listing_contact_for_update(
            lifecycle_listing["id"], user_id, details_retry_id, "listing_contact"
        ) is False
        assert await db.complete_telegram_update(details_retry_id, "listing_contact") is True

        business_update_id = completed_update_id + 1
        assert await db.claim_telegram_update(business_update_id, "business_contact") is True
        assert await db.log_business_contact_for_update(
            business["id"], user_id, business_update_id
        ) is True
        assert await db.log_business_contact_for_update(
            business["id"], user_id, business_update_id
        ) is False
        assert await db.pool().fetchval(
            "SELECT COUNT(*) FROM business_events WHERE source_update_id=$1", business_update_id
        ) == 1
        assert await db.complete_telegram_update(business_update_id, "business_contact") is True

        business_update_id_2 = business_update_id + 1
        assert await db.claim_telegram_update(business_update_id_2, "business_contact") is True
        assert await db.log_business_contact_for_update(
            business["id"], user_id, business_update_id_2
        ) is True
        assert await db.complete_telegram_update(business_update_id_2, "business_contact") is True
        assert await db.pool().fetchval(
            """SELECT COUNT(*) FROM business_events WHERE business_id=$1
               AND source_update_id=ANY($2::bigint[])""",
            business["id"], [business_update_id, business_update_id_2],
        ) == 2

        campaign_update_id = business_update_id_2 + 1
        assert await db.claim_telegram_update(campaign_update_id, "get_discount_code") is True
        assert await db.log_campaign_event_for_update(
            campaign["id"], user_id, "CLICKED", campaign_update_id, "get_discount_code"
        ) is True
        assert await db.log_campaign_event_for_update(
            campaign["id"], user_id, "CLICKED", campaign_update_id, "get_discount_code"
        ) is False
        assert await db.complete_telegram_update(campaign_update_id, "get_discount_code") is True

        report = await db.campaign_report(campaign["id"])
        assert report["sent"] == 1
        assert report["clicked"] == 1
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
