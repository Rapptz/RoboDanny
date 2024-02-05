-- Revises: V8
-- Creation Date: 2024-01-23 08:11:00.536813 UTC
-- Reason: mod alerts and gatekeeper

ALTER TABLE guild_mod_config ADD COLUMN IF NOT EXISTS alert_webhook_url TEXT;
ALTER TABLE guild_mod_config ADD COLUMN IF NOT EXISTS alert_channel_id BIGINT;

CREATE TABLE IF NOT EXISTS guild_gatekeeper (
    id BIGINT PRIMARY KEY,
    started_at TIMESTAMP CHECK (started_at IS NULL OR (channel_id IS NOT NULL AND role_id IS NOT NULL AND message_id IS NOT NULL)),
    channel_id BIGINT,
    role_id BIGINT,
    message_id BIGINT,
    bypass_action TEXT NOT NULL DEFAULT 'ban',
    rate TEXT
);


DO $$ BEGIN
    CREATE TYPE gatekeeper_role_state AS ENUM ('added', 'pending_add', 'pending_remove');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS guild_gatekeeper_members (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    state gatekeeper_role_state DEFAULT 'pending_add',
    PRIMARY KEY (guild_id, user_id)
);
