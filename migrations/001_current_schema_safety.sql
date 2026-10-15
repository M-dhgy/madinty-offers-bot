-- Upgrade the previously documented schema.sql baseline without deleting data.
-- Duplicate legacy records are preserved and block unique constraints with an
-- actionable error; they are never silently discarded or ignored.

ALTER TABLE businesses
    ADD COLUMN IF NOT EXISTS is_paid BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE discount_codes
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS broadcast_started_at TIMESTAMPTZ;
ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS submission_key TEXT;

CREATE INDEX IF NOT EXISTS idx_businesses_status ON businesses(status);
CREATE INDEX IF NOT EXISTS idx_businesses_city ON businesses(city_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_city_cat ON campaigns(city_id, category_id);
CREATE INDEX IF NOT EXISTS idx_codes_campaign ON discount_codes(campaign_id, status);

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
    submission_key TEXT,
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
ALTER TABLE listings ADD COLUMN IF NOT EXISTS city_id INTEGER REFERENCES cities(id);
ALTER TABLE listings ADD COLUMN IF NOT EXISTS submission_key TEXT;

CREATE INDEX IF NOT EXISTS idx_listings_user ON listings(user_id);
CREATE INDEX IF NOT EXISTS idx_listings_search ON listings(status, city_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_expiry ON listings(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_listing_events_listing ON listing_events(listing_id, event_type);
CREATE INDEX IF NOT EXISTS idx_business_events_business ON business_events(business_id, event_type);

CREATE TABLE IF NOT EXISTS telegram_update_receipts (
    update_id BIGINT PRIMARY KEY,
    action TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='uq_discount_campaign_user'
          AND conrelid='discount_codes'::regclass
    ) THEN
        IF EXISTS (
            SELECT 1 FROM discount_codes
            WHERE campaign_id IS NOT NULL AND user_id IS NOT NULL
            GROUP BY campaign_id, user_id HAVING COUNT(*) > 1
        ) THEN
            RAISE EXCEPTION
                'Cannot add uq_discount_campaign_user: duplicate campaign/user codes exist; preserve and resolve them explicitly first';
        END IF;
        ALTER TABLE discount_codes
            ADD CONSTRAINT uq_discount_campaign_user UNIQUE (campaign_id, user_id);
    END IF;
END
$migration$;

DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM campaigns
        WHERE business_id IS NOT NULL AND submission_key IS NOT NULL
        GROUP BY business_id, submission_key HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Cannot add campaign submission-key index: duplicate non-null keys exist; preserve and resolve them explicitly first';
    END IF;
    IF EXISTS (
        SELECT 1 FROM listings
        WHERE submission_key IS NOT NULL
        GROUP BY user_id, submission_key HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Cannot add listing submission-key index: duplicate non-null keys exist; preserve and resolve them explicitly first';
    END IF;
END
$migration$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_campaigns_submission_key
    ON campaigns(business_id, submission_key) WHERE submission_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_submission_key
    ON listings(user_id, submission_key) WHERE submission_key IS NOT NULL;
