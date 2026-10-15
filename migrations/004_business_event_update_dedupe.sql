ALTER TABLE business_events
    ADD COLUMN IF NOT EXISTS source_update_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_business_events_source_update
    ON business_events(source_update_id) WHERE source_update_id IS NOT NULL;
