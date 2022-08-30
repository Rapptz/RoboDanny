-- Revises: V4
-- Creation Date: 2022-08-24 00:32:18.674624 UTC
-- Reason: todo commands

CREATE TABLE IF NOT EXISTS todo (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    channel_id BIGINT,
    message_id BIGINT,
    guild_id BIGINT,
    due_date TIMESTAMP,
    content TEXT,
    completed_at TIMESTAMP,
    cached_content TEXT,
    reminder_triggered BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS todo_user_id_idx ON todo(user_id);
CREATE INDEX IF NOT EXISTS todo_message_id_idx ON todo(message_id);
CREATE INDEX IF NOT EXISTS todo_completed_at_idx ON todo(completed_at);
CREATE INDEX IF NOT EXISTS todo_due_date_idx ON todo(due_date);
