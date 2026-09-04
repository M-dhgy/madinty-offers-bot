"""
db.py — طبقة الوصول لقاعدة البيانات (PostgreSQL عبر asyncpg)
كل الدوال هنا async ويجب أن تُستدعى بعد تهيئة pool عبر init_pool().
"""

import os
import random
import string
import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    dsn = os.environ["DATABASE_URL"]  # e.g. postgresql://user:pass@localhost:5432/madinty
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
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


# ---------------------------------------------------------------
# Users
# ---------------------------------------------------------------
async def get_user_by_telegram_id(telegram_id: int):
    return await pool().fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)


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
    return await pool().fetchrow("SELECT * FROM businesses WHERE user_id=$1 ORDER BY id DESC LIMIT 1", user_id)


async def create_business(user_id: int, name: str, business_type: str, city_id: int, phone: str):
    return await pool().fetchrow(
        """INSERT INTO businesses (user_id, business_name, business_type, city_id, phone)
           VALUES ($1, $2, $3, $4, $5) RETURNING *""",
        user_id, name, business_type, city_id, phone,
    )


# ---------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------
async def create_campaign(business_id: int, raw_input: str, ai_data: dict, ad_text: str, category_id: int | None,
                           city_id: int | None):
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
           WHERE c.status='pending_review' ORDER BY c.id""")


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
    """المستخدمون الذين يطابقون مدينة/فئة الحملة."""
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


# ---------------------------------------------------------------
# Discount codes & events
# ---------------------------------------------------------------
def _gen_code(city_code: str) -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{city_code[:5]}-{suffix}"


async def create_discount_code(campaign_id: int, user_id: int, city_code: str):
    code = _gen_code(city_code)
    row = await pool().fetchrow(
        """INSERT INTO discount_codes (campaign_id, user_id, code)
           VALUES ($1, $2, $3) RETURNING *""",
        campaign_id, user_id, code,
    )
    await log_event(campaign_id, user_id, "CODE_GENERATED")
    return row


async def redeem_code(code: str):
    row = await pool().fetchrow(
        """UPDATE discount_codes SET status='redeemed', redeemed_at=now()
           WHERE code=$1 AND status='unused' RETURNING *""",
        code,
    )
    if row:
        await log_event(row["campaign_id"], row["user_id"], "REDEEMED")
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
