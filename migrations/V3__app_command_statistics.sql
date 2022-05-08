-- Revises: V2
-- Creation Date: 2022-05-08 11:11:10.968660 UTC
-- Reason: app command statistics

ALTER TABLE commands ADD COLUMN IF NOT EXISTS app_command BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS commands_app_command_idx ON commands (app_command);
