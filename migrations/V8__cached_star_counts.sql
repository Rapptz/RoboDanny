-- Revises: V7
-- Creation Date: 2023-06-25 06:03:32.998792 UTC
-- Reason: Cached star counts and improved star stats performance

SET work_mem = '256MB';

ALTER TABLE starboard_entries ADD COLUMN IF NOT EXISTS total INTEGER NOT NULL DEFAULT 0;

-- Super slow migration query
WITH counts AS (
  SELECT entry_id, COUNT(*) AS total
  FROM starrers
  GROUP BY entry_id
)
UPDATE starboard_entries
SET total = counts.total
FROM counts
WHERE id = counts.entry_id;

CREATE INDEX IF NOT EXISTS starrers_author_id_idx ON starrers (author_id);

-- This table is updated manually by the bot every so often
-- It is meant to provide a cache, similar to a materialized view
-- except with semantics that allow its updates to not block the
-- actual starrers table
CREATE TABLE IF NOT EXISTS star_givers (
    id SERIAL PRIMARY KEY,
    author_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    total INTEGER NOT NULL
);

-- Insert the data
INSERT INTO star_givers (author_id, guild_id, total)
SELECT starrers.author_id, entry.guild_id, COUNT(*)
FROM starrers
INNER JOIN starboard_entries entry ON entry.id = starrers.entry_id
GROUP BY starrers.author_id, entry.guild_id;

-- Add our indices and constraints
CREATE INDEX IF NOT EXISTS star_givers_author_id_idx ON star_givers (author_id);
CREATE INDEX IF NOT EXISTS star_givers_guild_id_idx ON star_givers (guild_id);
CREATE UNIQUE INDEX IF NOT EXISTS star_givers_uniq_idx ON star_givers (author_id, guild_id);
ALTER TABLE star_givers ADD CONSTRAINT star_givers_guild_id_fk FOREIGN KEY (guild_id) REFERENCES starboard (id) ON DELETE CASCADE ON UPDATE NO ACTION;

RESET work_mem;
