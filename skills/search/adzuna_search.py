"""
skills/search/adzuna_search.py

Market-wide job source via Adzuna's API.
Structured, dated listings, real recurring free tier (1,000 calls/month).
Covers climate and tech/AI verticals with a query matrix.
"""

import sys, os, time, hashlib, logging, json
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "skills" / "profile"))
from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("search.adzuna")

ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
ADZUNA_URL     = "https://api.adzuna.com/v1/api/jobs/gb/search/1"
MAX_AGE_DAYS   = 30
DELAY          = 1.5

CLIMATE_QUERIES = [
    "climate business development", "sustainability business development",
    "climate partnerships", "sustainability commercial lead",
    "climate GTM", "carbon commercial manager",
    "climate revenue lead", "sustainability partnerships",
    "cleantech business development", "climate tech sales",
    "net zero commercial", "climate market entry",
]

TECH_QUERIES = [
    "AI business development", "AI partnerships", "AI sales",
    "AI GTM", "AI commercial lead", "AI go to market",
    "AI solutions consultant", "AI automation specialist",
    "agent orchestration", "forward deployed engineer",
    "AI operations manager", "AI revenue",
    "machine learning business development", "SaaS partnerships AI",
    "employer of record business development", "global mobility commercial",
]

UK_TERMS = [
    "uk", "united kingdom", "london", "manchester", "bristol",
    "edinburgh", "birmingham", "leeds", "remote", "hybrid",
]


def make_fingerprint(title, company):
    raw = f"{(company or '').lower().strip()}|{(title or '').lower().strip()}"
    return hashlib.sha1(raw.encode()).hexdigest()


def is_uk_or_remote(location: str) -> bool:
    loc = (location or "").lower()
    return any(term in loc for term in UK_TERMS) or loc.strip() == ""


def parse_age_days(created: str) -> int:
    """Adzuna 'created' field is ISO 8601, e.g. 2026-09-01T10:15:00Z"""
    if not created:
        return 999
    try:
        dt = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")
        return (datetime.now() - dt).days
    except ValueError:
        return 999


def search_adzuna(keywords: str) -> list:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        log.error("Adzuna credentials not set")
        return []
    try:
        resp = requests.get(ADZUNA_URL, params={
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what": keywords,
            "where": "UK",
            "results_per_page": 20,
            "sort_by": "date",
        }, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Adzuna {resp.status_code} for: {keywords}")
            return []
        return resp.json().get("results", [])
    except Exception as e:
        log.warning(f"Error for '{keywords}': {e}")
        return []


def run(dry_run: bool = False) -> dict:
    conn = get_conn()
    matrix = [("climate", q) for q in CLIMATE_QUERIES] + [("tech", q) for q in TECH_QUERIES]
    total_found = total_saved = total_stale = total_non_uk = 0
    seen_fps = set()
    requests_used = 0

    for category, query in matrix:
        jobs = search_adzuna(query)
        requests_used += 1
        log.info(f"[{category}] '{query}': {len(jobs)} raw")

        for j in jobs:
            title    = j.get("title", "")
            company  = (j.get("company") or {}).get("display_name", "")
            location = (j.get("location") or {}).get("display_name", "")
            url      = j.get("redirect_url", "")
            created  = j.get("created", "")
            desc     = (j.get("description") or "")[:3000]
            salary_min = j.get("salary_min")
            salary_max = j.get("salary_max")
            salary_raw = f"£{int(salary_min):,} - £{int(salary_max):,}" if salary_min and salary_max else ""

            if not is_uk_or_remote(location):
                total_non_uk += 1
                continue

            age = parse_age_days(created)
            if age > MAX_AGE_DAYS:
                total_stale += 1
                continue

            fp = make_fingerprint(title, company)
            if fp in seen_fps:
                continue
            seen_fps.add(fp)
            total_found += 1

            if dry_run:
                continue

            existing = conn.execute("SELECT id FROM jobs WHERE fingerprint = ?", (fp,)).fetchone()
            if existing:
                conn.execute("UPDATE jobs SET last_seen = datetime('now') WHERE fingerprint = ?", (fp,))
            else:
                conn.execute("""
                    INSERT INTO jobs (fingerprint, title, company_name, location, url,
                                      description, salary_raw, source, category, mode, posted_age_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'adzuna', ?, 'dream', ?)
                """, (fp, title, company, location, url, desc, salary_raw, category, age))
                total_saved += 1
        time.sleep(DELAY)

    conn.commit()
    conn.close()
    summary = {
        "requests_used": requests_used,
        "found_fresh": total_found,
        "saved_new": total_saved,
        "filtered_stale": total_stale,
        "filtered_non_uk": total_non_uk,
    }
    log.info(f"Adzuna complete: {summary}")
    return summary


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), indent=2))
