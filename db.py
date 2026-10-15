"""
db.py — طبقة الوصول لقاعدة البيانات (PostgreSQL عبر asyncpg)
v0.2:
  • ترقية آمنة عند init_pool (is_paid, UNIQUE code, expires_at)
  • find_city_by_name(name)
  • list_public_listings / list_public_businesses تدعم city_id=None (كل المدن)
  • create_discount_code يعيد الكود الموجود لنفس (حملة، مستخدم)
  • أنشطة pending لا تظهر للعملاء إلا بعد الاعتماد
"""

import os
import random
import string
from datetime import date
from pathlib import Path
import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    dsn = os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    async with _pool.acquire() as connection:
        await apply_migrations(connection)
    return _pool


async def apply_migrations(connection, migrations_dir: Path | None = None):
    """Apply ordered SQL migrations once, atomically, and without discarding data."""
    directory = Path(migrations_dir) if migrations_dir else Path(__file__).with_name("migrations")
    # Serialize the first creation too: locking only after this DDL lets
    # concurrent startup processes race on pg_class/pg_type for this table.
    async with connection.transaction():
        await connection.fetchval("SELECT pg_advisory_xact_lock($1)", 5219014201)
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    applied_versions = []
    for migration in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
        version = migration.name.split("_", 1)[0]
        async with connection.transaction():
            await connection.fetchval("SELECT pg_advisory_xact_lock($1)", 5219014201)
            already_applied = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version=$1)",
                version,
            )
            if already_applied:
                continue
            await connection.execute(migration.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                version, migration.name,
            )
            applied_versions.append(version)
    return applied_versions


async def close_pool():
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() first")
    return _pool


# ---------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------
async def list_cities():
    return await pool().fetch("SELECT id, name, code FROM cities WHERE status='active' ORDER BY id")


async def list_categories():
    return await pool().fetch("SELECT id, name, code FROM categories WHERE status='active' ORDER BY id")


async def find_city_by_name(text: str):
    """مطابقة تقريبية لمدينة كتبها المستخدم بالعربي أو الإنجليزي."""
    if not text:
        return None
    text = text.strip()
    return await pool().fetchrow(
        """SELECT id, name, code FROM cities
           WHERE status='active' AND (name ILIKE $1 OR code ILIKE $1)
           ORDER BY id LIMIT 1""",
        f"%{text}%",
    )


# ---------------------------------------------------------------
# Users
# ---------------------------------------------------------------
async def get_user_by_telegram_id(telegram_id: int):
    return await pool().fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)


async def get_user_by_id(user_id: int):
    return await pool().fetchrow("SELECT * FROM users WHERE id=$1", user_id)


async def create_user(telegram_id: int, username: str | None, first_name: str | None):
    return await pool().fetchrow(
        """INSERT INTO users (telegram_id, username, first_name)
           VALUES ($1, $2, $3)
           ON CONFLICT (telegram_id) DO UPDATE SET last_active_at = now()
           RETURNING *""",
        telegram_id, username, first_name,
    )


async def set_account_type(user_id: int, account_type: str):
    await pool().execute("UPDATE users SET account_type=$1 WHERE id=$2", account_type, user_id)


async def set_user_city(user_id: int, city_id: int):
    await pool().execute("UPDATE users SET city_id=$1 WHERE id=$2", city_id, user_id)


async def set_user_phone(user_id: int, phone: str):
    await pool().execute("UPDATE users SET phone=$1 WHERE id=$2", phone, user_id)


async def touch_user(user_id: int):
    await pool().execute("UPDATE users SET last_active_at=now() WHERE id=$1", user_id)


async def set_user_categories(user_id: int, category_ids: list[int]):
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM user_categories WHERE user_id=$1", user_id)
            for cid in category_ids:
                await conn.execute(
                    "INSERT INTO user_categories (user_id, category_id) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    user_id, cid,
                )


async def get_user_categories(user_id: int):
    return await pool().fetch(
        """SELECT c.id, c.name, c.code FROM user_categories uc
           JOIN categories c ON c.id = uc.category_id
           WHERE uc.user_id=$1""",
        user_id,
    )


# ---------------------------------------------------------------
# Businesses
# ---------------------------------------------------------------
async def get_business_by_user(user_id: int):
    return await pool().fetchrow(
        "SELECT * FROM businesses WHERE user_id=$1 ORDER BY id DESC LIMIT 1", user_id
    )


async def create_business(user_id: int, name: str, business_type: str,
                          city_id: int, phone: str, is_paid: bool = False):
    return await pool().fetchrow(
        """INSERT INTO businesses (user_id, business_name, business_type, city_id, phone, is_paid, status)
           VALUES ($1,$2,$3,$4,$5,$6,'pending') RETURNING *""",
        user_id, name, business_type, city_id, phone, is_paid,
    )


async def get_business(business_id: int):
    return await pool().fetchrow(
        """SELECT b.*, u.telegram_id AS owner_telegram_id FROM businesses b
           JOIN users u ON u.id=b.user_id WHERE b.id=$1""",
        business_id,
    )


async def list_pending_businesses():
    return await pool().fetch(
        """SELECT b.*, u.telegram_id AS owner_telegram_id, c.name AS city_name
           FROM businesses b
           JOIN users u ON u.id=b.user_id
           LEFT JOIN cities c ON c.id=b.city_id
           WHERE b.status='pending' ORDER BY b.id"""
    )


async def set_business_status(business_id: int, status: str):
    if status not in {"approved", "rejected"}:
        raise ValueError("Business review status must be approved or rejected")
    return await pool().fetchrow(
        "UPDATE businesses SET status=$1 WHERE id=$2 AND status='pending' RETURNING *",
        status, business_id,
    )


async def list_public_businesses(city_id: int | None = None, business_type: str | None = None):
    """للعملاء: فقط المعتمدة. city_id=None يعني كل المدن."""
    query = """SELECT b.*, c.name AS city_name, u.telegram_id AS owner_telegram_id
               FROM businesses b JOIN users u ON u.id=b.user_id
               LEFT JOIN cities c ON c.id=b.city_id
               WHERE b.status='approved'"""
    args = []
    if city_id:
        args.append(city_id)
        query += f" AND b.city_id=${len(args)}"
    if business_type:
        args.append(business_type)
        query += f" AND b.business_type=${len(args)}"
    query += " ORDER BY b.id DESC LIMIT 30"
    return await pool().fetch(query, *args)


async def log_business_event(business_id: int, user_id: int | None, event_type: str):
    await pool().execute(
        "INSERT INTO business_events (business_id, user_id, event_type) VALUES ($1,$2,$3)",
        business_id, user_id, event_type,
    )


async def business_report(business_id: int):
    row = await pool().fetchrow(
        """SELECT
           COUNT(*) FILTER (WHERE event_type='VIEW') AS views,
           COUNT(DISTINCT user_id) FILTER (WHERE event_type='VIEW' AND user_id IS NOT NULL) AS unique_viewers,
           COUNT(*) FILTER (WHERE event_type='CONTACT') AS contacts,
           COUNT(DISTINCT user_id) FILTER (WHERE event_type='CONTACT' AND user_id IS NOT NULL) AS unique_contacts
           FROM business_events WHERE business_id=$1""",
        business_id,
    )
    return dict(row) if row else {"views": 0, "unique_viewers": 0, "contacts": 0, "unique_contacts": 0}


# ---------------------------------------------------------------
# Listings (سوق الأفراد)
# ---------------------------------------------------------------
async def create_listing(user_id: int, title: str, description: str, price: str | None,
                         condition: str, city_id: int | None, category: str,
                         address: str, contact: str, delivery: str,
                         negotiable: bool, image_file_ids: list[str],
                         submission_key: str | None = None):
    parsed_price = None
    if price:
        try:
            parsed_price = float(str(price).replace(",", ".").strip())
        except ValueError:
            parsed_price = None
    return await pool().fetchrow(
        """INSERT INTO listings
           (user_id, title, description, price, condition, city_id, category, address,
            contact, delivery, negotiable, image_file_ids, submission_key)
           SELECT $1,$2,$3,$4,$5,ci.id,$7,$8,$9,$10,$11,$12,$13
           FROM cities ci WHERE ci.id=$6 AND ci.status='active'
           ON CONFLICT (user_id, submission_key) WHERE submission_key IS NOT NULL
           DO UPDATE SET submission_key=EXCLUDED.submission_key
           RETURNING *""",
        user_id, title, description, parsed_price, condition, city_id, category,
        address, contact, delivery, negotiable, image_file_ids[:2], submission_key,
    )


async def resubmit_listing(listing_id: int, user_id: int, title: str, description: str,
                           price: str | None, condition: str, city_id: int, category: str,
                           address: str, contact: str, delivery: str, negotiable: bool,
                           image_file_ids: list[str]):
    parsed_price = None
    if price:
        try:
            parsed_price = float(str(price).replace(",", ".").strip())
        except ValueError:
            parsed_price = None
    return await pool().fetchrow(
        """UPDATE listings AS l SET title=$3, description=$4, price=$5, condition=$6,
                  city_id=ci.id, category=$8, address=$9, contact=$10, delivery=$11,
                  negotiable=$12, image_file_ids=$13, status='pending_review',
                  expires_at=now()+interval '72 hours', updated_at=now()
           FROM cities ci
           WHERE l.id=$1 AND l.user_id=$2 AND l.status='rejected'
             AND ci.id=$7 AND ci.status='active'
           RETURNING l.*""",
        listing_id, user_id, title, description, parsed_price, condition, city_id,
        category, address, contact, delivery, negotiable, image_file_ids[:2],
    )


async def list_public_listings(city_id: int | None = None, category: str | None = None):
    """city_id=None يعني كل المدن."""
    await expire_listings()
    query = """SELECT l.*, c.name AS city_name, u.telegram_id AS owner_telegram_id
               FROM listings l JOIN users u ON u.id=l.user_id
               LEFT JOIN cities c ON c.id=l.city_id
               WHERE l.status='approved' AND l.expires_at > now()"""
    args = []
    if city_id:
        args.append(city_id)
        query += f" AND l.city_id=${len(args)}"
    if category:
        args.append(category)
        query += f" AND l.category=${len(args)}"
    query += " ORDER BY l.created_at DESC LIMIT 30"
    return await pool().fetch(query, *args)


async def list_pending_listings():
    await expire_listings()
    return await pool().fetch(
        """SELECT l.*, u.telegram_id, u.first_name, c.name AS city_name
           FROM listings l JOIN users u ON u.id=l.user_id
           LEFT JOIN cities c ON c.id=l.city_id
           WHERE l.status='pending_review' ORDER BY l.id"""
    )


async def get_listing(listing_id: int):
    return await pool().fetchrow("SELECT * FROM listings WHERE id=$1", listing_id)


async def get_public_listing(listing_id: int):
    """Return an ad only while it is approved and still within its term."""
    return await pool().fetchrow(
        """SELECT l.*, c.name AS city_name FROM listings l
           LEFT JOIN cities c ON c.id=l.city_id
           WHERE l.id=$1 AND l.status='approved' AND l.expires_at > now()""",
        listing_id,
    )


async def set_listing_status(listing_id: int, status: str):
    if status not in {"approved", "rejected"}:
        raise ValueError("Listing review status must be approved or rejected")
    expiry_update = ", expires_at=now()+interval '72 hours'" if status == "approved" else ""
    return await pool().fetchrow(
        "UPDATE listings SET status=$1, updated_at=now()" + expiry_update +
        " WHERE id=$2 AND status='pending_review' RETURNING *",
        status, listing_id,
    )


async def log_listing_event(listing_id: int, user_id: int | None, event_type: str):
    if event_type == "VIEW":
        await log_listing_views([listing_id], user_id)
        return
    await pool().execute(
        "INSERT INTO listing_events (listing_id, user_id, event_type) VALUES ($1,$2,$3)",
        listing_id, user_id, event_type,
    )


async def log_listing_views(listing_ids: list[int], user_id: int | None):
    """Record identified users once per UTC day; anonymous impressions remain countable."""
    ids = list(dict.fromkeys(listing_ids))
    if not ids:
        return
    if user_id is None:
        await pool().execute(
            """INSERT INTO listing_events (listing_id, user_id, event_type)
               SELECT ids.listing_id, NULL, 'VIEW'
               FROM unnest($1::integer[]) AS ids(listing_id)""",
            ids,
        )
        return

    async with pool().acquire() as conn:
        async with conn.transaction():
            for listing_id in ids:
                # Serialize check+insert for this pair; no history rewrite or unique constraint needed.
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock($1::integer, $2::integer)",
                    listing_id, user_id,
                )
                utc_day_start = await conn.fetchval(
                    "SELECT date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
                )
                await conn.execute(
                    """INSERT INTO listing_events (listing_id, user_id, event_type)
                       SELECT $1, $2, 'VIEW'
                       WHERE NOT EXISTS (
                         SELECT 1 FROM listing_events e
                         WHERE e.listing_id=$1 AND e.user_id=$2 AND e.event_type='VIEW'
                           AND e.created_at >= $3
                           AND e.created_at < $3 + interval '1 day'
                       )""",
                    listing_id, user_id, utc_day_start,
                )


async def expire_listings():
    await pool().execute(
        "UPDATE listings SET status=CASE WHEN status='approved' THEN 'expired' ELSE status END, "
        "image_file_ids='{}', updated_at=now() "
        "WHERE (status='approved' AND expires_at <= now()) "
        "OR (status='expired' AND cardinality(image_file_ids)>0)"
    )


async def renew_listing(listing_id: int, user_id: int):
    await expire_listings()
    return await pool().fetchrow(
        "UPDATE listings SET status='pending_review', "
        "expires_at=now()+interval '72 hours', updated_at=now() "
        "WHERE id=$1 AND user_id=$2 AND status IN ('expired','approved') "
        "AND cardinality(image_file_ids)>0 RETURNING *",
        listing_id, user_id,
    )


async def list_user_listings(user_id: int):
    await expire_listings()
    return await pool().fetch(
        """SELECT l.id, l.title, l.status, l.expires_at, l.image_file_ids,
                  COUNT(e.id) FILTER (WHERE e.event_type='VIEW') AS views,
                  COUNT(DISTINCT e.user_id) FILTER (
                      WHERE e.event_type='VIEW' AND e.user_id IS NOT NULL
                  ) AS unique_viewers,
                  COUNT(e.id) FILTER (WHERE e.event_type='CONTACT') AS contacts,
                  COUNT(DISTINCT e.user_id) FILTER (
                      WHERE e.event_type='CONTACT' AND e.user_id IS NOT NULL
                  ) AS unique_contacts
           FROM listings l
           LEFT JOIN listing_events e ON e.listing_id=l.id
           WHERE l.user_id=$1
           GROUP BY l.id
           ORDER BY l.id DESC LIMIT 20""",
        user_id,
    )


async def list_user_listings_page(
    user_id: int, mode: str = "list", cursor_id: int | None = None,
    direction: str = "next", limit: int = 10,
):
    """Keyset-paginated owner view; every query remains scoped to the DB user."""
    if mode not in {"list", "stats", "renew", "sold"}:
        raise ValueError("Unknown listing page mode")
    if direction not in {"next", "prev"}:
        raise ValueError("direction must be next or prev")
    if type(limit) is not int or limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    if cursor_id is not None and (type(cursor_id) is not int or cursor_id <= 0):
        raise ValueError("cursor_id must be a positive integer")

    await expire_listings()
    conditions = ["l.user_id=$1"]
    args = [user_id]
    if mode == "renew":
        args.append(["approved", "expired"])
        conditions.extend([f"l.status=ANY(${len(args)}::text[])", "cardinality(l.image_file_ids)>0"])
    elif mode == "sold":
        args.append(["approved", "pending_review"])
        conditions.append(f"l.status=ANY(${len(args)}::text[])")
    if cursor_id is not None:
        args.append(cursor_id)
        operator = ">" if direction == "prev" else "<"
        conditions.append(f"l.id{operator}${len(args)}")

    order = "ASC" if direction == "prev" else "DESC"
    args.append(limit + 1)
    rows = await pool().fetch(
        f"""SELECT l.id, l.title, l.status, l.expires_at, l.image_file_ids,
                     COUNT(e.id) FILTER (WHERE e.event_type='VIEW') AS views,
                     COUNT(DISTINCT e.user_id) FILTER (
                         WHERE e.event_type='VIEW' AND e.user_id IS NOT NULL
                     ) AS unique_viewers,
                     COUNT(e.id) FILTER (WHERE e.event_type='CONTACT') AS contacts,
                     COUNT(DISTINCT e.user_id) FILTER (
                         WHERE e.event_type='CONTACT' AND e.user_id IS NOT NULL
                     ) AS unique_contacts
              FROM listings l LEFT JOIN listing_events e ON e.listing_id=l.id
              WHERE {' AND '.join(conditions)}
              GROUP BY l.id ORDER BY l.id {order} LIMIT ${len(args)}""",
        *args,
    )
    has_extra = len(rows) > limit
    rows = rows[:limit]
    if direction == "prev":
        rows.reverse()
        return rows, has_extra, cursor_id is not None
    return rows, cursor_id is not None, has_extra


async def mark_listing_sold(listing_id: int, user_id: int):
    return await pool().fetchrow(
        "UPDATE listings SET status='sold', updated_at=now() "
        "WHERE id=$1 AND user_id=$2 AND status IN ('approved','pending_review') RETURNING *",
        listing_id, user_id,
    )


async def listing_report(listing_id: int):
    row = await pool().fetchrow(
        """SELECT
           COUNT(*) FILTER (WHERE event_type='VIEW') AS views,
           COUNT(DISTINCT user_id) FILTER (WHERE event_type='VIEW' AND user_id IS NOT NULL) AS unique_viewers,
           COUNT(*) FILTER (WHERE event_type='CONTACT') AS contacts,
           COUNT(DISTINCT user_id) FILTER (WHERE event_type='CONTACT' AND user_id IS NOT NULL) AS unique_contacts
           FROM listing_events WHERE listing_id=$1""",
        listing_id,
    )
    return dict(row) if row else {"views": 0, "unique_viewers": 0, "contacts": 0, "unique_contacts": 0}


# ---------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------
def normalize_campaign_date_values(start_value, end_value):
    """Convert optional blank/null-like AI date values to Python None."""
    def normalize(value):
        if value is None:
            return None
        if isinstance(value, str) and (
            not value.strip() or value.strip().lower() in {"null", "none"}
        ):
            return None
        return value

    return normalize(start_value), normalize(end_value)


def validate_campaign_dates(start_value, end_value):
    """Parse optional ISO calendar dates and reject invalid/reversed windows."""
    start_value, end_value = normalize_campaign_date_values(start_value, end_value)

    def parse(value, field):
        if value is None:
            return None
        if type(value) is date:
            return value
        if not isinstance(value, str) or len(value) != 10 or value.strip() != value:
            raise ValueError(f"Invalid campaign date for {field}; expected YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid campaign date for {field}; expected YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"Invalid campaign date for {field}; expected YYYY-MM-DD")
        return parsed

    start_date = parse(start_value, "start")
    end_date = parse(end_value, "end")
    if start_date and end_date and end_date < start_date:
        raise ValueError("Invalid campaign date range: end must not precede start")
    return start_date, end_date


async def create_campaign(business_id: int, raw_input: str, ai_data: dict, ad_text: str,
                          category_id: int | None, city_id: int | None,
                          submission_key: str | None = None):
    start_value, end_value = normalize_campaign_date_values(
        ai_data.get("start"), ai_data.get("end")
    )
    start_date, end_date = validate_campaign_dates(
        start_value, end_value
    )
    inserted = await pool().fetchrow(
        """INSERT INTO campaigns
             (business_id, title, description, raw_input, category_id, city_id,
              target_audience, discount_percent, start_date, end_date, status, submission_key)
           SELECT b.id, $2, $3, $4, ca.id, ci.id, $7, $8, $9, $10, 'pending_review', $11
           FROM businesses b
           JOIN cities ci ON ci.id=$6 AND ci.status='active'
           JOIN categories ca ON ca.id=$5 AND ca.status='active'
           WHERE b.id=$1 AND b.status='approved' AND b.city_id=ci.id
           ON CONFLICT (business_id, submission_key) WHERE submission_key IS NOT NULL
           DO NOTHING
           RETURNING *""",
        business_id,
        ai_data.get("title") or ad_text[:60],
        ad_text,
        raw_input,
        category_id,
        city_id,
        ai_data.get("target"),
        ai_data.get("discount"),
        start_date,
        end_date,
        submission_key,
    )

    if inserted:
        result = dict(inserted)
        result["_created"] = True
        return result
    if submission_key is None:
        return None
    existing = await pool().fetchrow(
        "SELECT * FROM campaigns WHERE business_id=$1 AND submission_key=$2",
        business_id, submission_key,
    )
    if not existing:
        return None
    result = dict(existing)
    result["_created"] = False
    return result


async def get_campaign(campaign_id: int):
    return await pool().fetchrow("SELECT * FROM campaigns WHERE id=$1", campaign_id)


async def list_pending_campaigns():
    return await pool().fetch(
        """SELECT c.*, b.business_name FROM campaigns c
           JOIN businesses b ON b.id = c.business_id
           WHERE c.status='pending_review' AND b.status='approved' ORDER BY c.id"""
    )


async def review_campaign(campaign_id: int, action: str):
    if action == "reject":
        return await pool().fetchrow(
            "UPDATE campaigns SET status='rejected' WHERE id=$1 AND status='pending_review' RETURNING *",
            campaign_id,
        )
    if action != "approve":
        raise ValueError("Campaign review action must be approve or reject")
    return await pool().fetchrow(
        """UPDATE campaigns c SET status='approved', approved_at=now()
           FROM businesses b, cities ci, categories ca
           WHERE c.id=$1 AND c.status='pending_review'
             AND b.id=c.business_id AND b.status='approved'
             AND ci.id=c.city_id AND ci.status='active' AND b.city_id=ci.id
             AND ca.id=c.category_id AND ca.status='active'
           RETURNING c.*""",
        campaign_id,
    )


async def validate_campaign_target(business_id: int, city_id: int | None, category_id: int | None) -> bool:
    """Require an approved merchant, its registered active city, and an active category."""
    if type(city_id) is not int or city_id <= 0 or type(category_id) is not int or category_id <= 0:
        return False
    return bool(await pool().fetchval(
        """SELECT EXISTS (
             SELECT 1 FROM businesses b
             JOIN cities ci ON ci.id=$2 AND ci.status='active'
             JOIN categories ca ON ca.id=$3 AND ca.status='active'
             WHERE b.id=$1 AND b.status='approved' AND b.city_id=ci.id
           )""",
        business_id, city_id, category_id,
    ))


async def claim_campaign_for_broadcast(campaign_id: int, resume: bool = False):
    """Claim once; resumption is explicit and skips ambiguous sends."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            campaign = await conn.fetchrow(
                "SELECT * FROM campaigns WHERE id=$1 FOR UPDATE", campaign_id
            )
            if not campaign:
                return None

            previous_status = campaign["status"]
            if previous_status == "approved" and not resume:
                allowed_status = "approved"
            elif resume and previous_status in {"sending", "broadcast_failed"}:
                if previous_status == "sending":
                    stale = await conn.fetchval(
                        "SELECT $1::timestamptz IS NULL OR $1 < now() - interval '5 minutes'",
                        campaign["broadcast_started_at"],
                    )
                    if not stale:
                        return None
                allowed_status = previous_status
            else:
                return None

            claimed = await conn.fetchrow(
                """UPDATE campaigns c SET status='sending', broadcast_started_at=now()
                   FROM businesses b, cities ci, categories ca
                   WHERE c.id=$1 AND c.status=$2
                     AND (c.start_date IS NULL OR c.start_date <= (now() AT TIME ZONE 'UTC')::date)
                     AND (c.end_date IS NULL OR c.end_date >= (now() AT TIME ZONE 'UTC')::date)
                     AND b.id=c.business_id AND b.status='approved'
                     AND ci.id=c.city_id AND ci.status='active' AND b.city_id=ci.id
                     AND ca.id=c.category_id AND ca.status='active'
                   RETURNING c.*""",
                campaign_id, allowed_status,
            )
            if not claimed:
                return None

            if previous_status == "sending":
                # A worker that died during send has an unknowable Telegram outcome.
                await conn.execute(
                    """UPDATE campaign_recipient_deliveries
                       SET state='uncertain', last_error=COALESCE(
                             last_error, 'Worker stopped during send; Telegram outcome unknown'
                           ), updated_at=now()
                       WHERE campaign_id=$1 AND state='sending'""",
                    campaign_id,
                )
            elif previous_status == "approved":
                # Freeze the audience at launch; recovery uses this same snapshot.
                await conn.execute(
                    """INSERT INTO campaign_recipient_deliveries (campaign_id, user_id)
                       SELECT $1, u.id FROM users u
                       JOIN user_categories uc ON uc.user_id=u.id AND uc.category_id=$3
                       JOIN cities ci ON ci.id=u.city_id AND ci.status='active'
                       JOIN categories ca ON ca.id=uc.category_id AND ca.status='active'
                       WHERE u.account_type='customer' AND u.status='active' AND u.city_id=$2
                       ON CONFLICT (campaign_id, user_id) DO NOTHING""",
                    campaign_id, claimed["city_id"], claimed["category_id"],
                )
            return claimed


async def finish_campaign_broadcast(campaign_id: int, succeeded: bool):
    """Finalize only after pending recipients are either sent or classified."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            has_pending = await conn.fetchval(
                """SELECT EXISTS (
                     SELECT 1 FROM campaign_recipient_deliveries
                     WHERE campaign_id=$1 AND state='pending'
                   )""",
                campaign_id,
            )
            status = "active" if succeeded and not has_pending else "broadcast_failed"
            return await conn.fetchrow(
                """UPDATE campaigns SET status=$2,
                          published_at=CASE WHEN $2='active' THEN COALESCE(published_at, now())
                                            ELSE published_at END
                   WHERE id=$1 AND status='sending' RETURNING *""",
                campaign_id, status,
            )


async def release_campaign_without_delivery(campaign_id: int):
    """Return an untouched campaign to approved when there were no recipients."""
    return await pool().fetchrow(
        "UPDATE campaigns SET status='approved', broadcast_started_at=NULL WHERE id=$1 AND status='sending' RETURNING *",
        campaign_id,
    )


async def list_pending_campaign_recipients(campaign_id: int):
    return await pool().fetch(
        """SELECT d.user_id AS id, u.telegram_id
           FROM campaign_recipient_deliveries d
           JOIN users u ON u.id=d.user_id
           WHERE d.campaign_id=$1 AND d.state='pending'
           ORDER BY d.user_id""",
        campaign_id,
    )


async def claim_campaign_recipient(campaign_id: int, user_id: int):
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE campaigns SET broadcast_started_at=now() WHERE id=$1 AND status='sending'",
                campaign_id,
            )
            return await conn.fetchrow(
                """UPDATE campaign_recipient_deliveries
                   SET state='sending', attempts=attempts+1, attempt_started_at=now(),
                       updated_at=now(), last_error=NULL
                   WHERE campaign_id=$1 AND user_id=$2 AND state='pending'
                   RETURNING campaign_id, user_id""",
                campaign_id, user_id,
            )


async def record_campaign_recipient_result(
    campaign_id: int, user_id: int, state: str, error: str | None = None
) -> bool:
    if state not in {"pending", "sent", "rejected", "uncertain"}:
        raise ValueError("Invalid campaign recipient state")
    async with pool().acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """UPDATE campaign_recipient_deliveries
                   SET state=$3, last_error=$4, updated_at=now()
                   WHERE campaign_id=$1 AND user_id=$2 AND state='sending'
                   RETURNING user_id""",
                campaign_id, user_id, state, (error or "")[:1000] or None,
            )
            if not updated:
                return False
            await conn.execute(
                "UPDATE campaigns SET broadcast_started_at=now() WHERE id=$1 AND status='sending'",
                campaign_id,
            )
            if state == "sent":
                # Event and delivery transition commit or roll back together.
                await conn.execute(
                    """INSERT INTO campaign_events (campaign_id, user_id, event_type)
                       VALUES ($1, $2, 'SENT')""",
                    campaign_id, user_id,
                )
            return True


async def campaign_delivery_summary(campaign_id: int):
    row = await pool().fetchrow(
        """SELECT COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE state='pending') AS pending,
                  COUNT(*) FILTER (WHERE state='sending') AS sending,
                  COUNT(*) FILTER (WHERE state='sent') AS sent,
                  COUNT(*) FILTER (WHERE state='rejected') AS rejected,
                  COUNT(*) FILTER (WHERE state='uncertain') AS uncertain
           FROM campaign_recipient_deliveries WHERE campaign_id=$1""",
        campaign_id,
    )
    return dict(row) if row else {
        "total": 0, "pending": 0, "sending": 0,
        "sent": 0, "rejected": 0, "uncertain": 0,
    }


async def find_target_users(city_id: int | None, category_id: int | None):
    """Return only active customers matching both a valid city and category."""
    if type(city_id) is not int or city_id <= 0 or type(category_id) is not int or category_id <= 0:
        return []
    return await pool().fetch(
        """SELECT DISTINCT u.* FROM users u
           JOIN user_categories uc ON uc.user_id=u.id AND uc.category_id=$2
           JOIN cities ci ON ci.id=u.city_id AND ci.status='active'
           JOIN categories ca ON ca.id=uc.category_id AND ca.status='active'
           WHERE u.account_type='customer' AND u.status='active' AND u.city_id=$1""",
        city_id, category_id,
    )


async def list_active_campaigns_for_user(user_id: int):
    return await pool().fetch(
        """SELECT DISTINCT c.* FROM campaigns c
           JOIN businesses b ON b.id=c.business_id AND b.status='approved'
           JOIN cities ci ON ci.id=c.city_id AND ci.status='active'
           JOIN categories ca ON ca.id=c.category_id AND ca.status='active'
           JOIN users u ON u.id=$1 AND u.city_id=c.city_id
             AND u.account_type='customer' AND u.status='active'
           JOIN user_categories uc ON uc.category_id = c.category_id
             WHERE c.status='active' AND b.city_id=c.city_id AND uc.user_id=u.id
             AND (c.start_date IS NULL OR c.start_date <= (now() AT TIME ZONE 'UTC')::date)
             AND (c.end_date IS NULL OR c.end_date >= (now() AT TIME ZONE 'UTC')::date)
           ORDER BY c.id DESC LIMIT 20""",
        user_id,
    )


# ---------------------------------------------------------------
# Discount codes & events
# ---------------------------------------------------------------
def _gen_code(city_code: str) -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{city_code[:5]}-{suffix}"


async def create_discount_code(campaign_id: int, user_id: int, city_code: str):
    """Return one code per user only while the campaign is eligible today (UTC)."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            campaign = await conn.fetchrow(
                """SELECT c.end_date FROM campaigns c
                   WHERE c.id=$1 AND c.status IN ('active','sending','broadcast_failed')
                     AND (c.start_date IS NULL OR c.start_date <= (now() AT TIME ZONE 'UTC')::date)
                     AND (c.end_date IS NULL OR c.end_date >= (now() AT TIME ZONE 'UTC')::date)""",
                campaign_id,
            )
            if not campaign:
                return None

            existing = await conn.fetchrow(
                "SELECT * FROM discount_codes WHERE campaign_id=$1 AND user_id=$2",
                campaign_id, user_id,
            )
            if existing:
                return existing if not existing["expires_at"] or existing["expires_at"] > await conn.fetchval("SELECT now()") else None

            for _ in range(5):
                code = _gen_code(city_code)
                row = await conn.fetchrow(
                    """INSERT INTO discount_codes (campaign_id, user_id, code, expires_at)
                       SELECT c.id, $2, $3,
                              CASE WHEN c.end_date IS NULL THEN NULL
                                   ELSE ((c.end_date + 1)::timestamp AT TIME ZONE 'UTC') END
                       FROM campaigns c
                       WHERE c.id=$1 AND c.status IN ('active','sending','broadcast_failed')
                         AND (c.start_date IS NULL OR c.start_date <= (now() AT TIME ZONE 'UTC')::date)
                         AND (c.end_date IS NULL OR c.end_date >= (now() AT TIME ZONE 'UTC')::date)
                       ON CONFLICT DO NOTHING
                       RETURNING *""",
                    campaign_id, user_id, code,
                )
                if row:
                    await conn.execute(
                        "INSERT INTO campaign_events (campaign_id, user_id, event_type) VALUES ($1, $2, 'CODE_GENERATED')",
                        campaign_id, user_id,
                    )
                    return row
                existing = await conn.fetchrow(
                    "SELECT * FROM discount_codes WHERE campaign_id=$1 AND user_id=$2",
                    campaign_id, user_id,
                )
                if existing:
                    return existing if not existing["expires_at"] or existing["expires_at"] > await conn.fetchval("SELECT now()") else None
                eligible = await conn.fetchval(
                    """SELECT EXISTS (SELECT 1 FROM campaigns c WHERE c.id=$1
                         AND c.status IN ('active','sending','broadcast_failed')
                         AND (c.start_date IS NULL OR c.start_date <= (now() AT TIME ZONE 'UTC')::date)
                         AND (c.end_date IS NULL OR c.end_date >= (now() AT TIME ZONE 'UTC')::date))""",
                    campaign_id,
                )
                if not eligible:
                    return None
    raise RuntimeError("تعذر إنشاء كود خصم فريد بعد عدة محاولات")


async def redeem_code(code: str):
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE discount_codes SET status='redeemed', redeemed_at=now()
                   WHERE code=$1 AND status='unused'
                     AND (expires_at IS NULL OR expires_at > now())
                     AND EXISTS (
                       SELECT 1 FROM campaigns c WHERE c.id=discount_codes.campaign_id
                         AND c.status IN ('active','sending','broadcast_failed')
                         AND (c.start_date IS NULL OR c.start_date <= (now() AT TIME ZONE 'UTC')::date)
                         AND (c.end_date IS NULL OR c.end_date >= (now() AT TIME ZONE 'UTC')::date)
                     )
                   RETURNING *""",
                code,
            )
            if row:
                await conn.execute(
                    "INSERT INTO campaign_events (campaign_id, user_id, event_type) "
                    "VALUES ($1, $2, 'REDEEMED')",
                    row["campaign_id"], row["user_id"],
                )
            return row


async def redeem_code_for_business(code: str, business_id: int):
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE discount_codes dc SET status='redeemed', redeemed_at=now()
                   FROM campaigns c
                   WHERE dc.campaign_id=c.id AND c.business_id=$1
                     AND dc.code=$2 AND dc.status='unused'
                     AND (dc.expires_at IS NULL OR dc.expires_at > now())
                     AND c.status IN ('active','sending','broadcast_failed')
                     AND (c.start_date IS NULL OR c.start_date <= (now() AT TIME ZONE 'UTC')::date)
                     AND (c.end_date IS NULL OR c.end_date >= (now() AT TIME ZONE 'UTC')::date)
                   RETURNING dc.*""",
                business_id, code,
            )
            if row:
                await conn.execute(
                    "INSERT INTO campaign_events (campaign_id, user_id, event_type) "
                    "VALUES ($1, $2, 'REDEEMED')",
                    row["campaign_id"], row["user_id"],
                )
            return row


async def log_event(campaign_id: int, user_id: int | None, event_type: str):
    await pool().execute(
        "INSERT INTO campaign_events (campaign_id, user_id, event_type) VALUES ($1, $2, $3)",
        campaign_id, user_id, event_type,
    )


async def log_campaign_event_for_update(
    campaign_id: int, user_id: int | None, event_type: str, update_id: int, action: str
) -> bool:
    async with pool().acquire() as conn:
        async with conn.transaction():
            event = await conn.fetchrow(
                """INSERT INTO campaign_events (campaign_id, user_id, event_type, source_update_id)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (source_update_id) WHERE source_update_id IS NOT NULL DO NOTHING
                   RETURNING id""",
                campaign_id, user_id, event_type, update_id,
            )
            receipt = await conn.fetchrow(
                """UPDATE telegram_update_receipts SET stage='event_logged'
                   WHERE update_id=$1 AND action=$2 AND status='processing'
                   RETURNING update_id""",
                update_id, action,
            )
            if not receipt:
                raise RuntimeError("Telegram update is not in a processing state")
            return bool(event)


async def log_listing_contact_for_update(
    listing_id: int, user_id: int | None, update_id: int, action: str = "listing_contact"
) -> bool:
    async with pool().acquire() as conn:
        async with conn.transaction():
            event = await conn.fetchrow(
                """INSERT INTO listing_events (listing_id, user_id, event_type, source_update_id)
                   VALUES ($1, $2, 'CONTACT', $3)
                   ON CONFLICT (source_update_id) WHERE source_update_id IS NOT NULL DO NOTHING
                   RETURNING id""",
                listing_id, user_id, update_id,
            )
            receipt = await conn.fetchrow(
                """UPDATE telegram_update_receipts SET stage='event_logged'
                   WHERE update_id=$1 AND action=$2 AND status='processing'
                   RETURNING update_id""",
                update_id, action,
            )
            if not receipt:
                raise RuntimeError("Telegram update is not in a processing state")
            return bool(event)


async def log_business_contact_for_update(
    business_id: int, user_id: int | None, update_id: int, action: str = "business_contact"
) -> bool:
    async with pool().acquire() as conn:
        async with conn.transaction():
            event = await conn.fetchrow(
                """INSERT INTO business_events (business_id, user_id, event_type, source_update_id)
                   VALUES ($1, $2, 'CONTACT', $3)
                   ON CONFLICT (source_update_id) WHERE source_update_id IS NOT NULL DO NOTHING
                   RETURNING id""",
                business_id, user_id, update_id,
            )
            receipt = await conn.fetchrow(
                """UPDATE telegram_update_receipts SET stage='event_logged'
                   WHERE update_id=$1 AND action=$2 AND status='processing'
                   RETURNING update_id""",
                update_id, action,
            )
            if not receipt:
                raise RuntimeError("Telegram update is not in a processing state")
            return bool(event)


async def get_telegram_update_receipt(update_id: int):
    return await pool().fetchrow(
        "SELECT * FROM telegram_update_receipts WHERE update_id=$1", update_id
    )


async def set_telegram_update_stage(update_id: int, action: str, stage: str) -> bool:
    if stage not in {"claimed", "event_logged", "notification_attempting", "notification_sent"}:
        raise ValueError("Invalid Telegram update processing stage")
    row = await pool().fetchrow(
        """UPDATE telegram_update_receipts SET stage=$3
           WHERE update_id=$1 AND action=$2 AND status='processing'
           RETURNING update_id""",
        update_id, action, stage,
    )
    return bool(row)


async def complete_telegram_update(update_id: int, action: str) -> bool:
    row = await pool().fetchrow(
        """UPDATE telegram_update_receipts
           SET status='completed', stage='completed', lease_until=NULL, processed_at=now()
           WHERE update_id=$1 AND action=$2 AND status='processing'
           RETURNING update_id""",
        update_id, action,
    )
    return bool(row)


async def release_telegram_update(update_id: int, action: str) -> bool:
    """Release replay-safe stages; listing contact can resume details after an ambiguous owner notice."""
    row = await pool().fetchrow(
        """UPDATE telegram_update_receipts SET lease_until=now()
           WHERE update_id=$1 AND action=$2 AND status='processing'
             AND (stage IN ('claimed', 'event_logged', 'notification_sent')
                  OR (action='listing_contact' AND stage='notification_attempting'))
           RETURNING update_id""",
        update_id, action,
    )
    return bool(row)


async def mark_telegram_update_uncertain(update_id: int, action: str) -> bool:
    row = await pool().fetchrow(
        """UPDATE telegram_update_receipts
           SET status='uncertain', stage='uncertain', lease_until=NULL, processed_at=now()
           WHERE update_id=$1 AND action=$2 AND status='processing'
           RETURNING update_id""",
        update_id, action,
    )
    return bool(row)


async def claim_telegram_update(update_id: int, action: str) -> bool:
    """Lease an update until completion; reclaim safe stages after failure."""
    if type(update_id) is not int or update_id < 0 or not action:
        raise ValueError("A valid Telegram update_id and action are required")
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE telegram_update_receipts
                   SET status='uncertain', stage='uncertain', lease_until=NULL, processed_at=now()
                   WHERE update_id=$1 AND status='processing'
                     AND stage='notification_attempting' AND lease_until <= now()
                     AND action <> 'listing_contact'""",
                update_id,
            )
            await conn.execute(
                """DELETE FROM telegram_update_receipts
                   WHERE status='completed' AND processed_at < now() - interval '30 days'"""
            )
            claimed = await conn.fetchval(
                """INSERT INTO telegram_update_receipts
                       (update_id, action, status, stage, lease_until, attempts)
                   VALUES ($1, $2, 'processing', 'claimed', now() + interval '5 minutes', 1)
                   ON CONFLICT (update_id) DO UPDATE SET
                       status='processing', lease_until=now() + interval '5 minutes',
                       attempts=telegram_update_receipts.attempts + 1, processed_at=now()
                   WHERE telegram_update_receipts.action=EXCLUDED.action
                     AND telegram_update_receipts.status='processing'
                     AND (
                         telegram_update_receipts.stage IN ('claimed', 'event_logged', 'notification_sent')
                         OR (EXCLUDED.action='listing_contact'
                             AND telegram_update_receipts.stage='notification_attempting')
                     )
                     AND telegram_update_receipts.lease_until <= now()
                   RETURNING 1""",
                update_id, action,
            )
            return bool(claimed)


async def campaign_report(campaign_id: int):
    row = await pool().fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE event_type='SENT') AS sent,
          COUNT(*) FILTER (WHERE event_type='CLICKED') AS clicked,
          COUNT(*) FILTER (WHERE event_type='CODE_GENERATED') AS codes,
          COUNT(*) FILTER (WHERE event_type='REDEEMED') AS redeemed
        FROM campaign_events WHERE campaign_id=$1
        """,
        campaign_id,
    )
    return dict(row) if row else {"sent": 0, "clicked": 0, "codes": 0, "redeemed": 0}


async def all_campaigns_report():
    """تقرير مجمّع لكل الحملات النشطة/المنشورة."""
    return await pool().fetch(
        """
        SELECT c.id, c.title, c.status,
          COUNT(*) FILTER (WHERE e.event_type='SENT') AS sent,
          COUNT(*) FILTER (WHERE e.event_type='CLICKED') AS clicked,
          COUNT(*) FILTER (WHERE e.event_type='CODE_GENERATED') AS codes,
          COUNT(*) FILTER (WHERE e.event_type='REDEEMED') AS redeemed
        FROM campaigns c LEFT JOIN campaign_events e ON e.campaign_id=c.id
        WHERE c.status IN ('approved','sending','active','broadcast_failed','expired')
        GROUP BY c.id ORDER BY c.id DESC LIMIT 20
        """
    )
