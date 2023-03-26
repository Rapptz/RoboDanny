-- Revises: V6
-- Creation Date: 2023-03-25 02:43:00.921357 UTC
-- Reason: timezones

CREATE TABLE IF NOT EXISTS user_settings (
    id BIGINT PRIMARY KEY, -- The discord user ID
    timezone TEXT -- The user's timezone
);

ALTER TABLE reminders ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE todo ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';
