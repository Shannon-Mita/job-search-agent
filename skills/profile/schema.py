"""
Database schema for the job search agent.
Run this once to initialise the SQLite database.
All tables defined here — never create tables anywhere else.
"""

SCHEMA = """

-- ─────────────────────────────────────────────
-- Company watchlist — source of truth imported
-- from Shannon_Company_Watchlist.xlsx
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    career_page_url TEXT    NOT NULL,
    sector          TEXT    NOT NULL,
    category        TEXT    DEFAULT 'climate',
    priority        TEXT    NOT NULL DEFAULT 'MEDIUM'
                            CHECK(priority IN ('HIGH','MEDIUM','LOW')),
    active          INTEGER NOT NULL DEFAULT 1,
    added_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);

-- ─────────────────────────────────────────────
-- Every job posting the agent has ever seen.
-- One row per unique posting — deduplication
-- happens before insert.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT    NOT NULL UNIQUE,   -- SHA1(company+title+location)
    title           TEXT    NOT NULL,
    company_name    TEXT    NOT NULL,
    company_id      INTEGER REFERENCES companies(id),
    location        TEXT,
    salary_raw      TEXT,                      -- raw string from posting
    salary_min      INTEGER,                   -- parsed lower bound GBP
    salary_max      INTEGER,                   -- parsed upper bound GBP
    job_type        TEXT,                      -- full-time / contract / freelance
    url             TEXT    NOT NULL,
    description     TEXT,
    source          TEXT    NOT NULL,          -- reed / watchlist / linkedin / etc.
    sector          TEXT,
    score           INTEGER DEFAULT 0,         -- 0-100 from score skill
    score_breakdown TEXT,                      -- JSON: {title, sector, salary, location, seniority}
    mode            TEXT    NOT NULL DEFAULT 'dream'
                            CHECK(mode IN ('dream','bridge')),
    first_seen      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT    NOT NULL DEFAULT (datetime('now')),
    notified        INTEGER NOT NULL DEFAULT 0,
    dismissed       INTEGER NOT NULL DEFAULT 0
);

-- ─────────────────────────────────────────────
-- Application pipeline — one row per application.
-- Mirrors a lightweight CRM.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    status          TEXT    NOT NULL DEFAULT 'saved'
                            CHECK(status IN (
                                'saved','applying','applied',
                                'interview','offer','rejected','withdrawn'
                            )),
    applied_at      TEXT,
    contact_name    TEXT,
    contact_email   TEXT,
    contact_linkedin TEXT,
    cover_note      TEXT,
    follow_up_date  TEXT,
    notes           TEXT,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────
-- Watch log — records every career page scrape.
-- Used to detect new postings and debug failures.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS watch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    scraped_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    jobs_found      INTEGER NOT NULL DEFAULT 0,
    jobs_new        INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'ok'
                            CHECK(status IN ('ok','failed','blocked','empty')),
    error_message   TEXT,
    duration_ms     INTEGER
);

-- ─────────────────────────────────────────────
-- Run log — one row per full agent run.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS run_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    mode            TEXT    NOT NULL CHECK(mode IN ('dream','bridge','both')),
    trigger         TEXT    NOT NULL DEFAULT 'scheduled'
                            CHECK(trigger IN ('scheduled','manual')),
    companies_checked INTEGER DEFAULT 0,
    platforms_checked INTEGER DEFAULT 0,
    jobs_found      INTEGER DEFAULT 0,
    jobs_new        INTEGER DEFAULT 0,
    jobs_notified   INTEGER DEFAULT 0,
    duration_s      INTEGER,
    status          TEXT    NOT NULL DEFAULT 'ok'
                            CHECK(status IN ('ok','partial','failed')),
    notes           TEXT
);

-- ─────────────────────────────────────────────
-- Signals — pre-posting intelligence.
-- Hiring signals spotted before a job goes live.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER REFERENCES companies(id),
    company_name    TEXT    NOT NULL,
    signal_type     TEXT    NOT NULL
                            CHECK(signal_type IN (
                                'funding_round','headcount_growth',
                                'hiring_post','key_departure','expansion'
                            )),
    source          TEXT    NOT NULL,          -- linkedin / crunchbase / twitter
    url             TEXT,
    summary         TEXT,
    spotted_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    actioned        INTEGER NOT NULL DEFAULT 0
);

-- ─────────────────────────────────────────────
-- Indexes for common queries
-- ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_jobs_score       ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_mode        ON jobs(mode);
CREATE INDEX IF NOT EXISTS idx_jobs_notified    ON jobs(notified);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen  ON jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company     ON jobs(company_name);
CREATE INDEX IF NOT EXISTS idx_apps_status      ON applications(status);
CREATE INDEX IF NOT EXISTS idx_companies_sector ON companies(sector);
CREATE INDEX IF NOT EXISTS idx_companies_active ON companies(active);
"""
