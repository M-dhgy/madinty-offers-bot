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
import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    dsn = os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)

    # ترقية آمنة لقاعدة بيانات موجودة — لا حذف بيانات
    await _pool.execute("""
        ALTER TABLE businesses ADD COLUMN IF NOT EXISTS is_paid BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE discount_codes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

        CREATE INDEX IF NOT EXISTS idx_businesses_status ON businesses(status);
        CREATE INDEX IF NOT EXISTS idx_businesses_city   ON businesses(city_id);
        CREATE INDEX IF NOT EXISTS idx_campaigns_city_cat ON campaigns(city_id, category_id);
        CREATE INDEX IF NOT EXISTS idx_codes_campaign    ON discount_codes(campaign_id, status);
        CREATE INDEX IF NOT EXISTS idx_listings_user     ON listings(user_id);

        CREATE TABLE IF NOT EXISTS listings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT,
            price NUMERIC(12,2),
            condition TEXT,
            city_id INTEGER REFERENCES cities(id),
            category TEXT,
            address TEXT,
            contact TEXT,
            delivery TEXT,
            negotiable BOOLEAN NOT NULL DEFAULT FALSE,
            image_file_ids TEXT[] NOT NULL DEFAULT '{}',
            expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '72 hours'),
            status TEXT NOT NULL DEFAULT 'pending_review',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS listing_events (
            id SERIAL PRIMARY KEY,
            listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS business_events (
            id SERIAL PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_listings_search ON listings(status, city_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_listing_events_listing ON listing_events(listing_id, event_type);
        CREATE INDEX IF NOT EXISTS idx_business_events_business ON business_events(business_id, event_type);
    """)

    # قيد UNIQUE لعمود (campaign_id, user_id) — إن لم يكن موجودًا
    try:
        await _pool.execute("""
            ALTER TABLE discount_codes
            ADD CONSTRAINT uq_discount_campaign_user UNIQUE (campaign_id, user_id)
        """)
    except asyncpg.DuplicateObjectError:
        pass
    except asyncpg.UniqueViolationError:
        pass

    return _pool


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
    status = "approved" if is_paid else "pending"
    return await pool().fetchrow(
        """INSERT INTO businesses (user_id, business_name, business_type, city_id, phone, is_paid, status)
           VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
        user_id, name, business_type, city_id, phone, is_paid, status,
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
    return await pool().fetchrow(
        "UPDATE businesses SET status=$1 WHERE id=$2 RETURNING *",
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
                         negotiable: bool, image_file_ids: list[str]):
    parsed_price = None
    if price:
        try:
            parsed_price = float(str(price).replace(",", ".").strip())
        except ValueError:
            parsed_price = None
    return await pool().fetchrow(
        """INSERT INTO listings
           (user_id, title, description, price, condition, city_id, category, address,
            contact, delivery, negotiable, image_file_ids)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *""",
        user_id, title, description, parsed_price, condition, city_id, category,
        address, contact, delivery, negotiable, image_file_ids[:2],
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


async def set_listing_status(listing_id: int, status: str):
    return await pool().fetchrow(
        "UPDATE listings SET status=$1, updated_at=now() WHERE id=$2 RETURNING *",
        status, listing_id,
    )


async def log_listing_event(listing_id: int, user_id: int | None, event_type: str):
    if event_type == "VIEW" and user_id is not None:
        await pool().execute(
            """INSERT INTO listing_events (listing_id, user_id, event_type)
               SELECT $1,$2,$3
               WHERE NOT EXISTS (
                 SELECT 1 FROM listing_events
                 WHERE listing_id=$1 AND user_id=$2 AND event_type=$3
                   AND created_at >= current_date
               )""",
            listing_id, user_id, event_type,
        )
        return
    await pool().execute(
        "INSERT INTO listing_events (listing_id, user_id, event_type) VALUES ($1,$2,$3)",
        listing_id, user_id, event_type,
    )


async def expire_listings():
    await pool().execute(
        "UPDATE listings SET status='expired', updated_at=now() "
        "WHERE status IN ('approved','pending_review') AND expires_at <= now()"
    )


async def renew_listing(listing_id: int, user_id: int):
    return await pool().fetchrow(
        "UPDATE listings SET status='pending_review', "
        "expires_at=now()+interval '72 hours', updated_at=now() "
        "WHERE id=$1 AND user_id=$2 AND status IN ('expired','approved') RETURNING *",
        listing_id, user_id,
    )


async def list_user_listings(user_id: int):
    await expire_listings()
    return await pool().fetch(
        "SELECT id, title, status, expires_at FROM listings WHERE user_id=$1 ORDER BY id DESC LIMIT 20",
        user_id,
    )


async def mark_listing_sold(listing_id: int, user_id: int):
    return await pool().fetchrow(
        "UPDATE listings SET status='sold', updated_at=now() WHERE id=$1 AND user_id=$2 RETURNING *",
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
async def create_campaign(business_id: int, raw_input: str, ai_data: dict, ad_text: str,
                          category_id: int | None, city_id: int | None):
    return await pool().fetchrow(
        """INSERT INTO campaigns
             (business_id, title, description, raw_input, category_id, city_id,
              target_audience, discount_percent, status)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending_review')
           RETURNING *""",
        business_id,
        ai_data.get("title") or ad_text[:60],
        ad_text,
        raw_input,
        category_id,
        city_id,
        ai_data.get("target"),
        ai_data.get("discount"),
    )


async def get_campaign(campaign_id: int):
    return await pool().fetchrow("SELECT * FROM campaigns WHERE id=$1", campaign_id)


async def list_pending_campaigns():
    return await pool().fetch(
        """SELECT c.*, b.business_name FROM campaigns c
           JOIN businesses b ON b.id = c.business_id
           WHERE c.status='pending_review' ORDER BY c.id"""
    )


async def set_campaign_status(campaign_id: int, status: str):
    if status == "approved":
        await pool().execute(
            "UPDATE campaigns SET status=$1, approved_at=now() WHERE id=$2", status, campaign_id)
    else:
        await pool().execute("UPDATE campaigns SET status=$1 WHERE id=$2", status, campaign_id)


async def mark_campaign_published(campaign_id: int):
    await pool().execute(
        "UPDATE campaigns SET status='active', published_at=now() WHERE id=$1", campaign_id)


async def find_target_users(city_id: int | None, category_id: int | None):
    """المستخدمون المطابقون. إذا كان كلا المعيارين None → لا أحد (حماية)."""
    if city_id is None and category_id is None:
        return []
    query = """
        SELECT DISTINCT u.* FROM users u
        LEFT JOIN user_categories uc ON uc.user_id = u.id
        WHERE u.account_type = 'customer' AND u.status = 'active'
    """
    args = []
    idx = 1
    if city_id:
        query += f" AND u.city_id = ${idx}"
        args.append(city_id)
        idx += 1
    if category_id:
        query += f" AND uc.category_id = ${idx}"
        args.append(category_id)
        idx += 1
    return await pool().fetch(query, *args)


async def list_active_campaigns_for_user(user_id: int):
    return await pool().fetch(
        """SELECT DISTINCT c.* FROM campaigns c
           JOIN users u ON u.city_id = c.city_id
           JOIN user_categories uc ON uc.category_id = c.category_id
           WHERE c.status='active' AND u.id=$1
             AND (c.start_date IS NULL OR c.start_date <= CURRENT_DATE)
             AND (c.end_date IS NULL OR c.end_date >= CURRENT_DATE)
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
    """كود واحد لكل (حملة، مستخدم). إن وُجد سابقًا يُعاد نفسه."""
    existing = await pool().fetchrow(
        "SELECT * FROM discount_codes WHERE campaign_id=$1 AND user_id=$2",
        campaign_id, user_id,
    )
    if existing:
        return existing

    # expires_at = end_date الحملة إن وُجد
    campaign = await pool().fetchrow("SELECT end_date FROM campaigns WHERE id=$1", campaign_id)
    expires_at = None
    if campaign and campaign["end_date"]:
        expires_at = campaign["end_date"]

    for _ in range(5):
        code = _gen_code(city_code)
        try:
            row = await pool().fetchrow(
                """INSERT INTO discount_codes (campaign_id, user_id, code, expires_at)
                   VALUES ($1, $2, $3, $4) RETURNING *""",
                campaign_id, user_id, code, expires_at,
            )
            await log_event(campaign_id, user_id, "CODE_GENERATED")
            return row
        except asyncpg.UniqueViolationError:
            # إما تصادم كود، أو المستخدم حصل على كود بالفعل — نتحقق:
            existing = await pool().fetchrow(
                "SELECT * FROM discount_codes WHERE campaign_id=$1 AND user_id=$2",
                campaign_id, user_id,
            )
            if existing:
                return existing
            continue
    raise RuntimeError("تعذر إنشاء كود خصم فريد بعد عدة محاولات")


async def redeem_code(code: str):
    row = await pool().fetchrow(
        """UPDATE discount_codes SET status='redeemed', redeemed_at=now()
           WHERE code=$1 AND status='unused'
             AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)
           RETURNING *""",
        code,
    )
    if row:
        await log_event(row["campaign_id"], row["user_id"], "REDEEMED")
    return row


async def redeem_code_for_business(code: str, business_id: int):
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE discount_codes dc SET status='redeemed', redeemed_at=now()
                   FROM campaigns c
                   WHERE dc.campaign_id=c.id AND c.business_id=$1
                     AND dc.code=$2 AND dc.status='unused'
                     AND (dc.expires_at IS NULL OR dc.expires_at >= CURRENT_DATE)
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
        WHERE c.status IN ('approved','active','expired')
        GROUP BY c.id ORDER BY c.id DESC LIMIT 20
        """
    )
