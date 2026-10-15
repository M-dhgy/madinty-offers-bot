-- Durable per-recipient state for resumable campaign broadcasts.
CREATE TABLE IF NOT EXISTS campaign_recipient_deliveries (
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'sending', 'sent', 'rejected', 'uncertain')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    attempt_started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_campaign_recipient_delivery_state
    ON campaign_recipient_deliveries(campaign_id, state, user_id);
