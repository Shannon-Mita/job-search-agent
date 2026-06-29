"""
skills/search/serper_search.py

Uses Serper.dev (Google Search API) to find climate job listings
from boards that block direct scraping (Climatebase, LinkedIn, etc.)

Cost: ~£4/month for 2,500 searches
Get API key at: serper.dev

Run standalone:
    python skills/search/serper_search.py
    python skills/search/serper_search.py --source climatebase
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
log = logging.getLogger("search.serper")

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
SERPER_URL     = "https://google.serper.dev/search"
DELAY_BETWEEN  = 2
MAX_DESCRIPTION = 3000


def make_fingerprint(company: str, title: str, source: str) -> str:
    raw = f"{company.lower().strip()}|{title.lower().strip()}|{source}"
    return hashlib.sha1(raw.encode()).hexdigest()


def parse_salary(text: str):
    if not text:
        return None, None
    text = text.replace(",", "").lower()
    pattern = r"£?\s*(\d+(?:\.\d+)?)\s*k?"
    matches = re.findall(pattern, text)
    values = []
    for m in matches:
        val = float(m)
        if val < 500:
            val *= 1000
        if 20000 <= val <= 500000:
            values.append(int(val))
    if len(values) >= 2:
        return min(values), max(values)
    elif len(values) == 1:
        return values[0], values[0]
    return None, None


def serper_search(query: str, num: int = 10) -> list:
    """Execute a Google search via Serper API. Returns list of results."""
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
            log.warning(f"Serper API {resp.status_code}: {query}")
            return []
        data = resp.json()
        return data.get("organic", [])
    except Exception as e:
        log.warning(f"Serper error: {e}")
        return []


def extract_job_from_result(result: dict, source: str) -> Optional[dict]:
    """
    Extract job details from a Google search result snippet.
    Returns a job dict or None if not a real job listing.
    """
    title   = result.get("title", "")
    url     = result.get("link", "")
    snippet = result.get("snippet", "")

    if not title or not url:
        return None

    # Clean title — remove site name suffix
    for sep in [" | ", " - ", " — ", " · "]:
        if sep in title:
            title = title.split(sep)[0].strip()

    if len(title) < 5 or len(title) > 120:
        return None

    # Skip non-job pages
    skip_patterns = [
        "jobs at ", "careers at ", "working at ",
        "about us", "home page", "login", "sign up",
        "climate jobs", "job board", "find jobs",
        "browse jobs", "search jobs",
        " jobs in ", " jobs for ", "job listings",
    ]
    if any(p in title.lower() for p in skip_patterns):
        return None

    # Skip LinkedIn/Indeed collection pages ("329 Solar Energy... jobs in UK")
    if re.match(r"^\d[\d,+]*\s+", title):
        return None

    # ── Company extraction ────────────────────────────────────────────
    company = ""

    # Pattern 1: Climatebase snippet format "Company Name; Location; ..."
    m = re.search(r"^([^;]+);\s*[A-Z]", snippet)
    if m:
        candidate = m.group(1).strip()
        # Sanity check — company names are 2-50 chars, not sentence fragments
        if 2 <= len(candidate) <= 50 and not candidate.endswith("."):
            company = candidate

    # Pattern 2: "at Company Name" in snippet
    if not company:
        m = re.search(r"\bat ([A-Z][A-Za-z0-9\s&\-\.]+?)[\.,\-\|;]", snippet)
        if m:
            company = m.group(1).strip()

    # Pattern 3: Extract from URL slug for known boards
    if not company and "climatebase.org" in url:
        # climatebase.org/jobs/{company-slug}/{job-slug}
        m = re.search(r"/jobs/([^/]+)/[^/]+$", url)
        if m:
            slug = m.group(1).replace("-", " ").title()
            company = slug

    # ── Location extraction ───────────────────────────────────────────
    location = ""

    # Climatebase format: "Company; Location; ..."
    parts = snippet.split(";")
    if len(parts) >= 2:
        loc_candidate = parts[1].strip()
        if len(loc_candidate) < 50:
            location = loc_candidate

    # Fallback: scan for known location terms
    if not location:
        loc_patterns = [
            r"\b(London|Manchester|Bristol|Edinburgh|Birmingham|Remote|Hybrid|UK|United Kingdom)\b",
        ]
        for pattern in loc_patterns:
            m = re.search(pattern, snippet, re.I)
            if m:
                location = m.group(1)
                break

    # ── Salary extraction ─────────────────────────────────────────────
    salary_raw = ""
    salary_match = re.search(r"£[\d,k\s\-]+(?:per year|pa|salary|OTE)?", snippet, re.I)
    if salary_match:
        salary_raw = salary_match.group(0)

    return {
        "title":        title,
        "company_name": company or "Unknown",
        "location":     location,
        "salary_raw":   salary_raw,
        "url":          url,
        "description":  snippet[:MAX_DESCRIPTION],
        "source":       source,
    }


# ── Search query sets per source ─────────────────────────────────────────────

def build_queries(profile: dict) -> dict:
    """Build targeted search queries for each source."""
    keywords = profile.get("skills_keywords", {}).get("core", [])[:8]
    titles   = profile.get("target_titles", [])[:6]
    context  = profile.get("context_signals", [])[:5]

    # Mix title and keyword searches
    search_terms = titles[:4] + keywords[:4]

    queries = {
        "climatebase": [
            f'site:climatebase.org "{term}" UK' for term in search_terms[:5]
        ],
        "workonclimate": [
            f'site:workonclimate.org "{term}"' for term in search_terms[:4]
        ],
        "terra_do": [
            f'site:terra.do "{term}" UK' for term in search_terms[:3]
        ],
        "general": [
            f'"{term}" ("climate" OR "sustainability" OR "circular economy") UK jobs'
            for term in titles[:4]
        ],
    }
    return queries


# ── Main scrape function ──────────────────────────────────────────────────────

def scrape_via_serper(source_filter: Optional[str] = None) -> dict:
    """
    Run Serper searches across all configured sources.
    Returns summary dict.
    """
    if not SERPER_API_KEY:
        log.error("SERPER_API_KEY not set — add it to .env")
        return {"error": "No API key"}

    conn    = get_conn()
    profile = get_profile()
    queries = build_queries(profile)

    # Filter to specific source if requested
    if source_filter:
        queries = {k: v for k, v in queries.items() if k == source_filter}

    # Load watchlist for company flagging
    watchlist = {
        r["name"].lower() for r in conn.execute(
            "SELECT name FROM companies WHERE active = 1"
        ).fetchall()
    }

    all_jobs  = []
    new_count = 0
    searches_used = 0

    for source, query_list in queries.items():
        log.info(f"Searching: {source} ({len(query_list)} queries)")

        for query in query_list:
            log.info(f"  Query: {query}")
            results = serper_search(query, num=10)
            searches_used += 1

            for result in results:
                job = extract_job_from_result(result, f"serper_{source}")
                if not job:
                    continue

                # Check if company is on watchlist — boost signal
                company_lower = job["company_name"].lower()
                on_watchlist  = any(
                    wl_name in company_lower or company_lower in wl_name
                    for wl_name in watchlist
                )

                fingerprint = make_fingerprint(
                    job["company_name"], job["title"], job["source"]
                )
                salary_min, salary_max = parse_salary(job.get("salary_raw", ""))

                # Get company_id if on watchlist
                company_id = None
                if on_watchlist:
                    row = conn.execute(
                        "SELECT id FROM companies WHERE LOWER(name) LIKE ?",
                        (f"%{company_lower}%",)
                    ).fetchone()
                    if row:
                        company_id = row["id"]

                existing = conn.execute(
                    "SELECT id FROM jobs WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()

                if existing:
                    conn.execute(
                        "UPDATE jobs SET last_seen = datetime('now') WHERE fingerprint = ?",
                        (fingerprint,),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            fingerprint, title, company_name, company_id,
                            location, salary_raw, salary_min, salary_max,
                            url, description, source, mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dream')
                        """,
                        (
                            fingerprint, job["title"], job["company_name"],
                            company_id, job["location"], job["salary_raw"],
                            salary_min, salary_max,
                            job["url"], job["description"], job["source"],
                        ),
                    )
                    new_count += 1
                    all_jobs.append(job)

            conn.commit()
            time.sleep(DELAY_BETWEEN)

    # Flag new companies not on watchlist
    new_companies = {
        j["company_name"] for j in all_jobs
        if j["company_name"].lower() not in watchlist
        and j["company_name"] != "Unknown"
    }
    for company in new_companies:
        try:
            conn.execute(
                """
                INSERT INTO signals (company_name, signal_type, source, summary)
                VALUES (?, 'hiring_post', 'serper',
                        'Found via Google search — not in watchlist. Consider adding.')
                """,
                (company,),
            )
        except Exception:
            pass
    conn.commit()
    conn.close()

    summary = {
        "sources_searched": len(queries),
        "searches_used":    searches_used,
        "jobs_new":         new_count,
        "new_companies_flagged": len(new_companies),
        "timestamp":        datetime.now().isoformat(),
    }

    log.info(
        f"Serper complete — {searches_used} searches, "
        f"{new_count} new jobs, {len(new_companies)} new companies flagged"
    )
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Search for climate jobs via Serper/Google")
    parser.add_argument("--source", choices=["climatebase", "linkedin", "workonclimate",
                                              "terra_do", "general"],
                        help="Search only one source")
    args = parser.parse_args()
    result = scrape_via_serper(source_filter=args.source)
    print(f"\nSummary: {json.dumps(result, indent=2)}")
