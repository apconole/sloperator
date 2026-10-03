-- Persist the References header chain so thread reconstruction can use it
-- as an independent signal beyond In-Reply-To.
ALTER TABLE events ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]';
