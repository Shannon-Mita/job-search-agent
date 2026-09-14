"""
skills/search/jooble_search.py

Structured jobs source via the Jooble API — free tier, real posted-date
field (`updated`), clean company/title fields straight from the
aggregator. No scraping or content-page filtering needed here, unlike
google_jobs.py — Jooble only returns actual postings.

Covers climate and tech/AI verticals with the same query matrix as
google_jobs.py. Jobs older than 30 days are filtered at fetch time
since `updated` is a real timestamp, not a fuzzy "N days ago" string.

API note: the "uk.jooble.org" subdomain 403s under Cloudflare's WAF
with the default requests User-Agent — use the bare "jooble.org" host
with a browser-like User-Agent instead (confirmed working 2026-09-11).

Run standalone:
    python skills/search/jooble_search.py
    python skills/search/jooble_search.py --dry-run
"""

import sys, os, time, hashlib, logging, json
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "skills" / "profile"))
from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("search.jooble")

JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY")
JOOBLE_URL = f"https://jooble.org/api/{JOOBLE_API_KEY}" if JOOBLE_API_KEY else None
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
LOCATION = "UK"
MAX_AGE_DAYS = 30
DELAY = 1.5

# Jooble's `location` param is a ranking signal, not a hard filter — it
# still returns US/other-country results (confirmed 2026-09-11: 19/155
# non-UK on first real run). Profile excludes relocation_other outright,
# so enforce it client-side.
UK_LOCATION_TERMS = [
    "united kingdom", "uk", "london", "manchester", "bristol", "edinburgh",
    "birmingham", "leeds", "remote", "hybrid",
]


def is_uk_or_remote(location: str) -> bool:
    loc = (location or "").lower()
    return any(t in loc for t in UK_LOCATION_TERMS)

# Query matrix — loaded from the shared query_terms.json (bare terms, no
# location text), then jooble's per-term UK/remote suffix is reapplied here.
# The suffix isn't uniform across terms — these overrides preserve the exact
# query strings this file used before the shared-config extraction.
import json as _json
with open(ROOT / "skills" / "search" / "query_terms.json") as _f:
    _terms = _json.load(_f)

_CLIMATE_SUFFIX_OVERRIDES = {
    "climate revenue lead": " remote",
    "sustainability partnerships": " remote UK",
}
_TECH_SUFFIX_OVERRIDES = {
    "AI sales": " UK remote",
    "forward deployed engineer": " UK remote",
    "AI revenue": " UK remote",
}
_TECH_FULL_OVERRIDES = {
    "SaaS partnerships AI": "SaaS partnerships UK AI",
}

CLIMATE_QUERIES = [
    t + _CLIMATE_SUFFIX_OVERRIDES.get(t, " UK")
    for t in _terms["climate_queries"]
]
TECH_QUERIES = [
    _TECH_FULL_OVERRIDES.get(t, t + _TECH_SUFFIX_OVERRIDES.get(t, " UK"))
    for t in _terms["tech_queries"]
]


def make_fingerprint(title, company):
    raw = f"{(company or '').lower().strip()}|{(title or '').lower().strip()}"
    return hashlib.sha1(raw.encode()).hexdigest()


def parse_posted_age(updated: str) -> int:
    """Days since Jooble's `updated` ISO timestamp. Returns 999 if unparseable."""
    if not updated:
        return 999
    try:
        ts = updated.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((datetime.now(timezone.utc) - dt).days, 0)
    except Exception:
        return 999


def search_jooble(keywords: str) -> list:
    if not JOOBLE_URL:
        log.error("No JOOBLE_API_KEY")
        return []
    try:
        resp = requests.post(JOOBLE_URL, headers=HEADERS,
            json={"keywords": keywords, "location": LOCATION}, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Jooble {resp.status_code} for: {keywords}")
            return []
        return resp.json().get("jobs", [])
    except Exception as e:
        log.warning(f"Error for '{keywords}': {e}")
        return []


def run(dry_run: bool = False) -> dict:
    conn = get_conn()
    matrix = [("climate", q) for q in CLIMATE_QUERIES] + [("tech", q) for q in TECH_QUERIES]
    total_found = total_saved = total_stale = total_non_uk = 0
    seen_fps = set()

    for category, query in matrix:
        jobs = search_jooble(query)
        kept = 0

        for j in jobs:
            title = j.get("title", "")
            company = j.get("company", "")
            location = j.get("location", "")
            url = j.get("link", "")
            salary = j.get("salary", "") or ""
            description = j.get("snippet", "")[:3000]
            updated = j.get("updated", "")

            if not title or not url:
                continue

            if not is_uk_or_remote(location):
                total_non_uk += 1
                continue

            age = parse_posted_age(updated)
            if age > MAX_AGE_DAYS:
                total_stale += 1
                continue

            fp = make_fingerprint(title, company)
            if fp in seen_fps:
                continue
            seen_fps.add(fp)
            total_found += 1
            kept += 1

            if dry_run:
                continue

            existing = conn.execute("SELECT id FROM jobs WHERE fingerprint = ?", (fp,)).fetchone()
            if existing:
                conn.execute("UPDATE jobs SET last_seen = datetime('now') WHERE fingerprint = ?", (fp,))
            else:
                conn.execute("""
                    INSERT INTO jobs (fingerprint, title, company_name, location, salary_raw, url,
                                      description, source, category, mode, posted_age_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'jooble', ?, 'dream', ?)
                """, (fp, title, company, location, salary, url, description, category, age))
                total_saved += 1

        log.info(f"[{category}] '{query}': {len(jobs)} raw, {kept} kept")
        time.sleep(DELAY)

    conn.commit()
    conn.close()
    summary = {
        "found_fresh": total_found, "saved_new": total_saved,
        "filtered_stale": total_stale, "filtered_non_uk": total_non_uk,
    }
    log.info(f"Jooble search complete: {summary}")
    return summary


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), indent=2))
