"""
skills/search/bridge_search.py

Searches for short-term, freelance, fractional and contract opportunities
aligned with Shannon's skills profile.

Sources:
  - Google/Serper searches for freelance/contract roles
  - LinkedIn Services Marketplace (via Serper)
  - PeoplePerHour (direct scrape)
  - Upwork (via Serper — direct scraping blocked)
  - Contra, Bark (via Serper)

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

SERPER_API_KEY  = os.getenv("SERPER_API_KEY")
SERPER_URL      = "https://google.serper.dev/search"
DELAY_BETWEEN   = 2
MAX_DESCRIPTION = 3000


def make_fingerprint(title: str, source: str, url: str) -> str:
    raw = f"{title.lower().strip()}|{source}|{url}"
    return hashlib.sha1(raw.encode()).hexdigest()


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
    """Extract a bridge/freelance opportunity from a search result."""
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

    # Skip irrelevant pages
    skip = [
        "how to find", "guide to", "what is", "top 10",
        "best freelance", "sign up", "login", "register",
        "post a job", "hire a", "find a freelancer",
    ]
    if any(p in title.lower() for p in skip):
        return None

    # Skip LinkedIn collection pages ("329 jobs in X")
    if re.match(r"^\d[\d,+]*\s+", title):
        return None

    # Skip person names (First Last - Title pattern)
    if re.match(r"^[A-Z][a-z]+ [A-Z][a-z\.]+\s*$", title):
        return None
    if re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+ [-–]", title):
        return None

    # Extract company/client
    company = ""
    m = re.search(r"\bat ([A-Z][A-Za-z0-9\s&\-]+?)[\.,\|;]", snippet)
    if m:
        company = m.group(1).strip()

    # Extract day rate / salary signals
    salary_raw = ""
    rate_match = re.search(
        r"£[\d,k\s\-]+(?:per day|/day|pd|per hour|/hour|ph|per annum|pa)?",
        snippet, re.I
    )
    if rate_match:
        salary_raw = rate_match.group(0)

    # Location
    location = "Remote"  # default for freelance
    loc_m = re.search(
        r"\b(London|Manchester|Bristol|Remote|Hybrid|UK|United Kingdom)\b",
        snippet, re.I
    )
    if loc_m:
        location = loc_m.group(1)

    return {
        "title":        title,
        "company_name": company or "Unknown",
        "location":     location,
        "salary_raw":   salary_raw,
        "url":          url,
        "description":  snippet[:MAX_DESCRIPTION],
        "source":       source,
        "mode":         "bridge",
    }


def build_bridge_queries(profile: dict) -> list:
    """Build search queries for bridge/freelance opportunities."""
    bridge  = profile.get("bridge_income", {})
    skills  = profile.get("skills_keywords", {}).get("core", [])[:6]

    # Core skill terms to search for
    skill_terms = [
        "global mobility", "people operations", "business development",
        "sales enablement", "talent acquisition", "HR operations",
        "go-to-market", "operational efficiency",
    ]

    contract_terms = [
        "fractional", "interim", "contract", "freelance consulting",
    ]

    queries = []

    # Fractional/interim roles — UK focused
    for skill in skill_terms[:5]:
        for contract in contract_terms[:2]:
            queries.append(
                f'"{contract}" "{skill}" UK site:linkedin.com OR site:peopleperhour.com OR site:contra.com'
            )

    # PeoplePerHour specific
    for skill in skill_terms[:4]:
        queries.append(f'site:peopleperhour.com "{skill}"')

    # Upwork via Google
    for skill in skill_terms[:3]:
        queries.append(f'site:upwork.com "{skill}" "global" OR "UK"')

    # General freelance market
    queries.extend([
        '"fractional Head of People" OR "fractional HR" UK 2025 2026',
        '"interim Head of Talent" OR "interim People Director" UK',
        '"fractional BD" OR "fractional business development" climate sustainability UK',
        '"contract people operations" OR "contract HR" climate sustainability UK',
        '"fractional COO" OR "fractional Chief of Staff" climate UK startup',
        '"global mobility consultant" OR "global mobility contractor" UK',
        '"sales enablement consultant" OR "sales enablement contractor" UK',
    ])

    return queries


def scrape_peopleperhour(profile: dict) -> list:
    """Direct scrape of PeoplePerHour for relevant project listings."""
    jobs = []
    skills = [
        "people-operations", "business-development",
        "hr-consulting", "talent-acquisition", "sales-enablement"
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    for skill in skills[:3]:
        url  = f"https://www.peopleperhour.com/freelance-{skill}-jobs"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select("[class*='listing'], [class*='job-item'], article")
            for card in cards[:10]:
                title_el = card.find(["h2", "h3", "h4", "a"])
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if len(title) < 5 or len(title) > 120:
                    continue
                link = card.find("a", href=True)
                job_url = link["href"] if link else url
                if not job_url.startswith("http"):
                    job_url = f"https://www.peopleperhour.com{job_url}"
                jobs.append({
                    "title":        title,
                    "company_name": "PeoplePerHour client",
                    "location":     "Remote",
                    "salary_raw":   "",
                    "url":          job_url,
                    "description":  card.get_text(separator=" ", strip=True)[:MAX_DESCRIPTION],
                    "source":       "peopleperhour",
                    "mode":         "bridge",
                })
        except Exception as e:
            log.warning(f"PeoplePerHour error for {skill}: {e}")
        time.sleep(DELAY_BETWEEN)

    return jobs


def save_bridge_jobs(conn, jobs: list) -> tuple:
    """Save bridge jobs to database with mode='bridge'."""
    cursor    = conn.cursor()
    new_count = 0

    for job in jobs:
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

        # Parse salary
        salary_raw = job.get("salary_raw", "")
        salary_min = None
        salary_max = None
        if salary_raw:
            nums = re.findall(r"[\d,]+", salary_raw.replace("k", "000"))
            vals = [int(n.replace(",", "")) for n in nums if n.replace(",", "").isdigit()]
            vals = [v for v in vals if 100 <= v <= 2000]  # day rate range
            if len(vals) >= 2:
                salary_min, salary_max = min(vals), max(vals)
            elif len(vals) == 1:
                salary_min = salary_max = vals[0]

        cursor.execute(
            """
            INSERT INTO jobs (
                fingerprint, title, company_name,
                location, salary_raw, salary_min, salary_max,
                url, description, source, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'bridge')
            """,
            (
                fingerprint, job["title"], job["company_name"],
                job.get("location", ""), salary_raw,
                salary_min, salary_max,
                job["url"], job.get("description", ""), job["source"],
            ),
        )
        new_count += 1

    conn.commit()
    return new_count, len(jobs)


def run_bridge_search(dry_run: bool = False) -> dict:
    """Run all bridge income searches."""
    if not SERPER_API_KEY:
        log.error("SERPER_API_KEY not set — add to .env")
        return {"error": "no_api_key"}

    conn    = get_conn()
    profile = get_profile()
    queries = build_bridge_queries(profile)

    all_jobs      = []
    searches_used = 0

    log.info(f"Running {len(queries)} bridge income searches")

    # Serper searches
    for query in queries:
        log.info(f"  Query: {query[:80]}...")
        results = serper_search(query, num=10)
        searches_used += 1

        for result in results:
            job = extract_bridge_job(result, "serper_bridge")
            if job:
                all_jobs.append(job)

        time.sleep(DELAY_BETWEEN)

    # PeoplePerHour direct scrape
    log.info("Scraping PeoplePerHour directly...")
    pph_jobs = scrape_peopleperhour(profile)
    all_jobs.extend(pph_jobs)
    log.info(f"  PeoplePerHour: {len(pph_jobs)} listings")

    if dry_run:
        print(f"\nDRY RUN — {len(all_jobs)} bridge opportunities found")
        for j in all_jobs[:20]:
            print(f"  {j['title']} via {j['source']} ({j['location']})")
        conn.close()
        return {"dry_run": True, "found": len(all_jobs)}

    new_count, total = save_bridge_jobs(conn, all_jobs)
    conn.close()

    summary = {
        "searches_used": searches_used,
        "jobs_found":    total,
        "jobs_new":      new_count,
        "timestamp":     datetime.now().isoformat(),
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
