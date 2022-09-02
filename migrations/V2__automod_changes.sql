-- Revises: V1
-- Creation Date: 2022-05-06 14:33:59.784020 UTC
-- Reason: automod changes

ALTER TABLE guild_mod_config RENAME COLUMN safe_mention_channel_ids TO safe_automod_channel_ids;
ALTER TABLE guild_mod_config RENAME COLUMN raid_mode TO automod_flags;
ALTER TABLE guild_mod_config ALTER COLUMN automod_flags SET DEFAULT 0;
ALTER TABLE guild_mod_config ADD COLUMN broadcast_webhook_url TEXT;

-- Previous versions of raid_mod = 2 implied raid_mode = 1
-- Due to this now being interpreted as bit flags this will need to be 3 (1 | 2)
UPDATE guild_mod_config SET automod_flags = 3 WHERE automod_flags = 2;

-- Remove all nulls and set them to zero
UPDATE guild_mod_config SET automod_flags = 0 WHERE automod_flags IS NULL;

-- Change the flags to be not null now that there are no null values
ALTER TABLE guild_mod_config ALTER COLUMN automod_flags SET NOT NULL;
