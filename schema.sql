-- schema.sql — shared data contract for ogunjob.db
--
-- Canonical file. Tasks copy CREATE TABLE IF NOT EXISTS for the tables
-- they write; do not invent columns, types, or constraints beyond these.
-- SQLite has no BOOLEAN: `remote` is INTEGER 0/1.
-- Date/datetime: ISO strings (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).
-- JSON: TEXT (serialized array/object).
--
-- Path: ogunjob.db at repo root (same file for every writer).
-- tracker.py used to use applications.db; the agreed path is ogunjob.db.

-- ---------------------------------------------------------------------------
-- applications — EXISTING (tracker.py). Schema FROZEN: do not change.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    link TEXT,
    cv_version TEXT,
    status TEXT DEFAULT 'da_inviare',
    applied_on TEXT,
    last_update TEXT,
    notes TEXT
);

-- ---------------------------------------------------------------------------
-- jobs — scrapers. One row per collected listing.
-- UNIQUE(source, url) avoids duplicates on the same source.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    url TEXT NOT NULL,
    description TEXT,
    location TEXT,
    remote INTEGER NOT NULL DEFAULT 0,
    posted_date TEXT,
    scraped_at TEXT NOT NULL,
    raw_json TEXT,
    UNIQUE (source, url)
);

CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs (source);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs (scraped_at);

-- ---------------------------------------------------------------------------
-- skills — matcher. Candidate skills (profile and/or listings).
-- Same label from different sources = two rows (UNIQUE label+source).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    tags TEXT,
    source TEXT,
    confidence REAL,
    UNIQUE (label, source)
);

-- ---------------------------------------------------------------------------
-- matches — matcher. One current match per job (UPSERT on job_id).
-- job_id is a logical reference to jobs.id: no FOREIGN KEY, so scrapers
-- and matcher stay decoupled at CREATE TABLE time.
-- score: 0.0–1.0 (not 0–100).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    score REAL NOT NULL,
    rationale TEXT,
    computed_at TEXT NOT NULL,
    UNIQUE (job_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_score ON matches (score DESC);
