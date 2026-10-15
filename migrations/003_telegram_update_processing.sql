-- Preserve legacy receipts as completed; new update claims use leased processing stages.
ALTER TABLE telegram_update_receipts
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE telegram_update_receipts
    ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE telegram_update_receipts
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE telegram_update_receipts
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 1;
UPDATE telegram_update_receipts
SET status='completed', stage='completed', lease_until=NULL
WHERE status IS NULL OR stage IS NULL;
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='ck_telegram_update_receipt_status'
          AND conrelid='telegram_update_receipts'::regclass
    ) THEN
        ALTER TABLE telegram_update_receipts
            ADD CONSTRAINT ck_telegram_update_receipt_status
            CHECK (status IN ('processing', 'completed', 'uncertain'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='ck_telegram_update_receipt_stage'
          AND conrelid='telegram_update_receipts'::regclass
    ) THEN
        ALTER TABLE telegram_update_receipts
            ADD CONSTRAINT ck_telegram_update_receipt_stage
            CHECK (stage IN ('claimed', 'event_logged', 'notification_attempting',
                             'notification_sent', 'completed', 'uncertain'));
    END IF;
END
$migration$;
ALTER TABLE listing_events
    ADD COLUMN IF NOT EXISTS source_update_id BIGINT;
ALTER TABLE campaign_events
    ADD COLUMN IF NOT EXISTS source_update_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_listing_events_source_update
    ON listing_events(source_update_id) WHERE source_update_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_campaign_events_source_update
    ON campaign_events(source_update_id) WHERE source_update_id IS NOT NULL;
