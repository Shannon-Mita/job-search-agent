"""
skills/search/bridge_search.py

Searches for short-term, freelance, fractional and contract opportunities.
Focuses on platforms with actual structured job listings, not content pages.

Sources:
  - Reed API (free) — contract/interim filter, UK focused
  - Otta (via Serper) — startup contract roles
  - LinkedIn (via Serper) — targeted fractional/interim searches
  - Guardian Jobs (via Serper) — interim/contract filter

Run standalone:
    python skills/search/bridge_search.py
    python skills/search/bridge_search.py --dry-run
"""

import sys
import time
import hashlib
import logging
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "skills" / "profile"))

from db import get_conn, get_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("search.bridge")

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
REED_API_KEY   = os.getenv("REED_API_KEY")
SERPER_URL     = "https://google.serper.dev/search"
DELAY_BETWEEN  = 2
MAX_DESCRIPTION = 3000

# Phrases that indicate content pages, not job listings
NON_JOB_SKIP = [
    "how to", "guide to", "what is", "top 10", "best ",
    "why you", "tips for", "advice on", "insight",
    "newsletter", "podcast", "webinar", "course", "event",
    "salary guide", "market report", "hiring guide",
    "oecd", "policy", "guidance on", "support group",
    "focuses on", "the view from", "should feel",
]

# Minimum indicators that something is an actual job posting
JOB_INDICATORS = [
    "hiring", "vacancy", "role", "position", "opportunity",
    "contract", "interim", "fractional", "freelance",
    "looking for", "seeking", "we need", "join us",
    "£", "per day", "per hour", "day rate",
]


def make_fingerprint(title: str, source: str, url: str) -> str:
    raw = f"{title.lower().strip()}|{source}|{url}"
    return hashlib.sha1(raw.encode()).hexdigest()


def is_real_job(title: str, snippet: str) -> bool:
    """Check if a search result is an actual job posting."""
    combined = f"{title} {snippet}".lower()

    # Reject content pages
    if any(phrase in combined for phrase in NON_JOB_SKIP):
        return False

    # Reject person names (First Last pattern with no job words)
    words = title.split()
    job_words = ["manager", "director", "head", "lead", "consultant",
                 "specialist", "associate", "officer", "analyst", "advisor",
                 "interim", "fractional", "contract", "freelance", "hiring"]
    if (2 <= len(words) <= 3
            and all(w[0].isupper() for w in words if len(w) > 1)
            and not any(w in title.lower() for w in job_words)):
        return False

    # Reject questions
    if title.strip().endswith("?"):
        return False

    # Must have at least one job indicator
    if not any(ind in combined for ind in JOB_INDICATORS):
        return False

    return True


def serper_search(query: str, num: int = 10) -> list:
    if not SERPER_API_KEY:
        log.error("SERPER_API_KEY not set in .env")
        return []
    try:
        resp = requests.post(
            SERPER_URL,
            headers={
                "X-API-KEY": SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num, "gl": "uk", "hl": "en"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"Serper {resp.status_code}: {query}")
            return []
        return resp.json().get("organic", [])
    except Exception as e:
        log.warning(f"Serper error: {e}")
        return []


def extract_bridge_job(result: dict, source: str) -> Optional[dict]:
    """Extract a bridge job from a search result with strict validation."""
    title   = result.get("title", "")
    url     = result.get("link", "")
    snippet = result.get("snippet", "")

    if not title or not url:
        return None

    # Clean title
    for sep in [" | ", " - ", " — ", " · "]:
        if sep in title:
            title = title.split(sep)[0].strip()

    if len(title) < 5 or len(title) > 120:
        return None

    # Strict job validation
    if not is_real_job(title, snippet):
        return None

    # Extract location — UK only
    location = ""
    uk_locations = [
        "London", "Manchester", "Bristol", "Edinburgh", "Birmingham",
        "Leeds", "Remote", "Hybrid", "UK", "United Kingdom",
    ]
    for loc in uk_locations:
        if loc.lower() in snippet.lower():
            location = loc
            break
    if not location:
        location = "Remote"  # reasonable default for freelance

    # Extract rate
    salary_raw = ""
    rate_match = re.search(
        r"£[\d,]+(?:k)?(?:\s*[-–]\s*£[\d,]+(?:k)?)?(?:\s*(?:per day|/day|pd|per hour|/hour|ph|per annum|pa))?",
        snippet, re.I
    )
    if rate_match:
        salary_raw = rate_match.group(0)

    return {
        "title":        title,
        "company_name": "Via " + source.replace("serper_", "").replace("_", " ").title(),
        "location":     location,
        "salary_raw":   salary_raw,
        "url":          url,
        "description":  snippet[:MAX_DESCRIPTION],
        "source":       f"serper_{source}",
        "mode":         "bridge",
    }


def search_reed_contract() -> list:
    """
    Search Reed API for contract/interim roles matching Shannon's skills.
    Reed is used here specifically for bridge income — structured UK listings
    with contract filter applied.
    """
    if not REED_API_KEY:
        log.info("No Reed API key — skipping Reed bridge search")
        return []

    jobs = []
    keywords = [
        "fractional people operations",
        "interim head of people",
        "contract talent acquisition",
        "fractional HR director",
        "interim business development",
        "contract global mobility",
        "fractional chief of staff",
        "interim sales enablement",
    ]

    for keyword in keywords:
        try:
            resp = requests.get(
                "https://www.reed.co.uk/api/1.0/search",
                auth=(REED_API_KEY, ""),
                params={
                    "keywords":       keyword,
                    "locationName":   "United Kingdom",
                    "contractType":   "contract",
                    "resultsToTake":  10,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                continue

            for job in resp.json().get("results", []):
                jobs.append({
                    "title":        job.get("jobTitle", ""),
                    "company_name": job.get("employerName", "Unknown"),
                    "location":     job.get("locationName", "UK"),
                    "salary_raw":   f"£{job.get('minimumSalary', '')} - £{job.get('maximumSalary', '')}".strip("- £"),
                    "url":          job.get("jobUrl", ""),
                    "description":  job.get("jobDescription", "")[:MAX_DESCRIPTION],
                    "source":       "reed_contract",
                    "mode":         "bridge",
                })
            time.sleep(DELAY_BETWEEN)
        except Exception as e:
            log.warning(f"Reed error for '{keyword}': {e}")

    log.info(f"Reed contract: {len(jobs)} roles found")
    return jobs


def search_serper_bridge() -> list:
    """Targeted Serper searches for actual freelance/contract postings."""
    jobs = []

    # Very specific queries that return actual job postings
    queries = [
        # LinkedIn job postings — contract filter
        'site:linkedin.com/jobs "interim" OR "fractional" ("people operations" OR "HR" OR "talent") UK',
        'site:linkedin.com/jobs "contract" ("business development" OR "sales enablement") ("climate" OR "sustainability") UK',
        'site:linkedin.com/jobs "fractional" ("Chief of Staff" OR "Head of People" OR "COO") UK',
        # Specific fractional platforms
        'site:fractional.work ("people" OR "HR" OR "talent" OR "business development") UK',
        'site:calmerry.com OR site:worksome.co.uk "fractional" OR "interim" "people" UK',
        # Guardian Jobs contract
        'site:jobs.theguardian.com "interim" OR "contract" ("sustainability" OR "climate" OR "people operations")',
        # Exec contract boards
        '"interim director" OR "fractional director" ("people" OR "talent" OR "HR" OR "BD") UK 2025 2026',
        '"fractional Head of People" UK ("climate" OR "sustainability" OR "startup" OR "scaleup")',
        '"interim Chief of Staff" UK startup scaleup 2025 2026',
        '"contract global mobility" OR "interim global mobility" UK',
    ]

    for query in queries:
        log.info(f"  Bridge query: {query[:70]}...")
        results = serper_search(query, num=10)

        for result in results:
            job = extract_bridge_job(result, "bridge")
            if job:
                jobs.append(job)

        time.sleep(DELAY_BETWEEN)

    log.info(f"Serper bridge: {len(jobs)} valid postings found")
    return jobs


def save_bridge_jobs(conn, jobs: list) -> tuple:
    """Save bridge jobs with deduplication."""
    cursor    = conn.cursor()
    new_count = 0

    for job in jobs:
        if not job.get("title") or not job.get("url"):
            continue

        fingerprint = make_fingerprint(
            job["title"], job["source"], job["url"]
        )

        existing = cursor.execute(
            "SELECT id FROM jobs WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()

        if existing:
            cursor.execute(
                "UPDATE jobs SET last_seen = datetime('now') WHERE fingerprint = ?",
                (fingerprint,),
            )
            continue

        cursor.execute(
            """
            INSERT INTO jobs (
                fingerprint, title, company_name,
                location, salary_raw,
                url, description, source, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'bridge')
            """,
            (
                fingerprint,
                job["title"],
                job.get("company_name", "Unknown"),
                job.get("location", ""),
                job.get("salary_raw", ""),
                job["url"],
                job.get("description", ""),
                job["source"],
            ),
        )
        new_count += 1

    conn.commit()
    return new_count, len(jobs)


def run_bridge_search(dry_run: bool = False) -> dict:
    """Run all bridge income searches."""
    conn    = get_conn()
    profile = get_profile()

    log.info("Starting bridge income search")
    all_jobs = []

    # Reed contract search (structured, reliable)
    reed_jobs = search_reed_contract()
    all_jobs.extend(reed_jobs)

    # Serper targeted searches
    serper_jobs = search_serper_bridge()
    all_jobs.extend(serper_jobs)

    log.info(f"Total bridge opportunities found: {len(all_jobs)}")

    if dry_run:
        print(f"\nDRY RUN — {len(all_jobs)} bridge opportunities")
        for j in all_jobs:
            print(f"  [{j['source']}] {j['title']} ({j['location']})")
        conn.close()
        return {"dry_run": True, "found": len(all_jobs)}

    new_count, total = save_bridge_jobs(conn, all_jobs)
    conn.close()

    summary = {
        "jobs_found": total,
        "jobs_new":   new_count,
        "timestamp":  datetime.now().isoformat(),
    }
    log.info(f"Bridge search complete — {total} found, {new_count} new")
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Search for bridge income opportunities")
    parser.add_argument("--dry-run", action="store_true", help="Find without saving")
    args = parser.parse_args()
    result = run_bridge_search(dry_run=args.dry_run)
    print(f"\nSummary: {json.dumps(result, indent=2)}")
