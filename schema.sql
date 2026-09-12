-- ============================================================
-- Madinty Offers (بوت عروض مدينتي) — PostgreSQL Schema
-- Phase A (MVP) + Phase B (discount codes / analytics) tables
-- v0.2 — تحديثات:
--   • businesses.is_paid (جاهز للباقات)
--   • discount_codes: UNIQUE (campaign_id, user_id) + expires_at
--   • فهارس إضافية للبحث بالمدينة
-- ============================================================

CREATE TABLE IF NOT EXISTS cities (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    code        TEXT UNIQUE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    code        TEXT UNIQUE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    telegram_id     BIGINT UNIQUE NOT NULL,
    username        TEXT,
    first_name      TEXT,
    phone           TEXT,
    city_id         INTEGER REFERENCES cities(id),
    account_type    TEXT CHECK (account_type IN ('customer', 'merchant')),
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_categories (
    user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
    category_id  INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, category_id)
);

CREATE TABLE IF NOT EXISTS businesses (
    id             SERIAL PRIMARY KEY,
    user_id        INTEGER REFERENCES users(id) ON DELETE CASCADE,
    business_name  TEXT NOT NULL,
    business_type  TEXT,
    city_id        INTEGER REFERENCES cities(id),
    phone          TEXT,
    address        TEXT,
    description    TEXT,
    is_paid        BOOLEAN NOT NULL DEFAULT FALSE,   -- اشتراك في باقة → يتخطى المراجعة
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_businesses_status ON businesses(status);
CREATE INDEX IF NOT EXISTS idx_businesses_city   ON businesses(city_id);

CREATE TABLE IF NOT EXISTS campaigns (
    id                SERIAL PRIMARY KEY,
    business_id       INTEGER REFERENCES businesses(id) ON DELETE CASCADE,
    title             TEXT,
    description       TEXT,
    raw_input         TEXT,
    category_id       INTEGER REFERENCES categories(id),
    city_id           INTEGER REFERENCES cities(id),
    target_audience   TEXT,
    discount_percent  INTEGER,
    image_url         TEXT,
    start_date        DATE,
    end_date          DATE,
    status            TEXT NOT NULL DEFAULT 'draft',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at       TIMESTAMPTZ,
    published_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status   ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_city_cat ON campaigns(city_id, category_id);

CREATE TABLE IF NOT EXISTS discount_codes (
    id            SERIAL PRIMARY KEY,
    campaign_id   INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    code          TEXT UNIQUE NOT NULL,
    status        TEXT NOT NULL DEFAULT 'unused',  -- unused / redeemed
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,
    redeemed_at   TIMESTAMPTZ,
    -- كود واحد فقط لكل (حملة، مستخدم) — يمنع التلاعب
    CONSTRAINT uq_discount_campaign_user UNIQUE (campaign_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_codes_campaign ON discount_codes(campaign_id, status);

CREATE TABLE IF NOT EXISTS campaign_events (
    id            SERIAL PRIMARY KEY,
    campaign_id   INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,  -- SENT / DELIVERED / CLICKED / CODE_GENERATED / REDEEMED
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_campaign ON campaign_events(campaign_id);

-- سوق الأفراد
CREATE TABLE IF NOT EXISTS listings (
    id             SERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    description    TEXT,
    price          NUMERIC(12,2),
    condition      TEXT,
    city_id        INTEGER REFERENCES cities(id),
    category       TEXT,
    address        TEXT,
    contact        TEXT,
    delivery       TEXT,
    negotiable     BOOLEAN NOT NULL DEFAULT FALSE,
    image_file_ids TEXT[] NOT NULL DEFAULT '{}',
    expires_at     TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '72 hours'),
    status         TEXT NOT NULL DEFAULT 'pending_review',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_listings_search ON listings(status, city_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_user   ON listings(user_id);

CREATE TABLE IF NOT EXISTS listing_events (
    id         SERIAL PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS business_events (
    id          SERIAL PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event_type  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_listing_events_listing   ON listing_events(listing_id, event_type);
CREATE INDEX IF NOT EXISTS idx_business_events_business ON business_events(business_id, event_type);

-- ============================================================
-- Seed data
-- ============================================================
INSERT INTO cities (name, code) VALUES
    ('الخرطوم', 'KHARTOUM'),
    ('أم درمان', 'OMDURMAN'),
    ('بحري', 'BAHRI')
ON CONFLICT (code) DO NOTHING;

INSERT INTO categories (name, code) VALUES
    ('مطاعم وكافيهات', 'FOOD'),
    ('تسوق', 'SHOPPING'),
    ('صحة وجمال', 'BEAUTY'),
    ('خدمات', 'SERVICES')
ON CONFLICT (code) DO NOTHING;
