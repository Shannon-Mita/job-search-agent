"""
skills/score/scorer.py

Scores every unscored job in the database against Shannon's profile.
Runs after each scrape. Updates jobs.score and jobs.score_breakdown.

Scoring weights (sum to 100):
    company_watchlist_match  30
    earning_signal_match     25
    skills_keyword_match     20
    sector_match             15
    location_match           10

Run standalone:
    python skills/score/scorer.py
    python skills/score/scorer.py --rescore-all
"""

import sys
import json
import logging
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "skills" / "profile"))

from db import get_conn, get_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("score.scorer")


# ── Scoring components ────────────────────────────────────────────────────────

def score_company_watchlist(company_name: str, watchlist: dict) -> tuple[int, str]:
    """
    30 points max.
    HIGH priority company   = 30
    MEDIUM priority company = 22
    LOW priority company    = 12
    Not on watchlist        = 0
    """
    entry = watchlist.get(company_name.lower())
    if not entry:
        return 0, "not_on_watchlist"
    priority = entry.get("priority", "LOW")
    scores = {"HIGH": 30, "MEDIUM": 22, "LOW": 12}
    return scores.get(priority, 0), f"watchlist_{priority.lower()}"


def score_earning_signals(text: str, profile: dict) -> tuple[int, str]:
    """
    25 points max.
    Scaled by number of earning signals found in job text.
    1 signal = 12, 2 = 18, 3+ = 25
    """
    if not text:
        return 0, "no_text"

    signals = profile.get("earning_signals", [])
    text_lower = text.lower()
    found = [s for s in signals if s.lower() in text_lower]

    if len(found) >= 3:
        return 25, f"signals:{','.join(found[:3])}"
    elif len(found) == 2:
        return 18, f"signals:{','.join(found)}"
    elif len(found) == 1:
        return 12, f"signals:{found[0]}"
    return 0, "no_earning_signals"


def score_skills_keywords(title: str, description: str, profile: dict) -> tuple[int, str]:
    """
    20 points max.
    Checks title (weighted 2x) and description for core skills keywords.
    1 match = 8, 2 = 13, 3 = 17, 4+ = 20
    """
    core_keywords = profile.get("skills_keywords", {}).get("core", [])
    if not core_keywords:
        core_keywords = profile.get("skills", [])

    title_lower       = (title or "").lower()
    description_lower = (description or "").lower()

    found = set()
    for kw in core_keywords:
        kw_lower = kw.lower()
        if kw_lower in title_lower:
            found.add(kw)
            found.add(kw)  # title match counts double — add twice to set won't work, use count
        if kw_lower in description_lower:
            found.add(kw)

    # Count title matches separately for weighting
    title_matches = [kw for kw in core_keywords if kw.lower() in title_lower]
    desc_matches  = [kw for kw in core_keywords if kw.lower() in description_lower]

    # Weighted total: title match = 2 points, description match = 1 point
    weighted = len(set(title_matches)) * 2 + len(set(desc_matches))

    if weighted >= 8:
        return 20, f"keywords:{','.join(set(title_matches + desc_matches))[:80]}"
    elif weighted >= 5:
        return 17, f"keywords:{','.join(set(title_matches + desc_matches))[:80]}"
    elif weighted >= 3:
        return 13, f"keywords:{','.join(set(title_matches + desc_matches))[:80]}"
    elif weighted >= 1:
        return 8,  f"keywords:{','.join(set(title_matches + desc_matches))[:80]}"
    return 0, "no_keyword_match"


def score_sector(sector: str, profile: dict) -> tuple[int, str]:
    """
    15 points max.
    HIGH priority sector   = 15
    MEDIUM priority sector = 10
    LOW priority sector    = 4
    Unknown sector         = 0
    """
    if not sector:
        return 0, "no_sector"

    sectors = profile.get("sectors", {})
    config  = sectors.get(sector)

    if not config:
        # Try partial match
        for s, c in sectors.items():
            if s.lower() in sector.lower() or sector.lower() in s.lower():
                config = c
                break

    if not config:
        return 0, f"unknown_sector:{sector}"

    priority = config.get("priority", "LOW")
    weight   = config.get("weight", 0.3)
    base     = {"HIGH": 15, "MEDIUM": 10, "LOW": 4}
    score    = int(base.get(priority, 0) * weight)
    return score, f"sector_{priority.lower()}:{sector}"


def score_location(location: str, profile: dict) -> tuple[int, str]:
    """
    10 points max.
    Hybrid UK / UK-based = 10
    Remote               = 9
    Australia            = 8
    Other onsite UK      = 6
    No location info     = 5 (benefit of doubt)
    Requires relocation  = 0
    """
    if not location:
        return 5, "no_location_info"

    loc_lower = location.lower()

    # Remote
    if any(w in loc_lower for w in ["remote", "anywhere", "distributed", "worldwide"]):
        if any(w in loc_lower for w in ["uk", "united kingdom", "britain", "london", "england"]):
            return 10, "remote_uk"
        return 9, "remote_global"

    # UK locations
    uk_cities = ["london", "manchester", "bristol", "edinburgh", "birmingham",
                 "leeds", "oxford", "cambridge", "uk", "united kingdom", "england",
                 "scotland", "wales", "britain", "hybrid"]
    if any(city in loc_lower for city in uk_cities):
        if "hybrid" in loc_lower:
            return 10, "hybrid_uk"
        return 8, "onsite_uk"

    # Australia
    if any(w in loc_lower for w in ["australia", "sydney", "melbourne", "brisbane", "perth"]):
        return 8, "australia"

    # EU / Europe — acceptable but not ideal
    if any(w in loc_lower for w in ["europe", "european", "amsterdam", "berlin",
                                     "paris", "dublin", "amsterdam"]):
        return 5, "europe"

    # US — remote roles acceptable, onsite not
    if any(w in loc_lower for w in ["united states", "usa", "new york", "san francisco",
                                     "los angeles", "boston", "chicago"]):
        return 3, "us_onsite"

    # Explicitly non-relevant locations — don't surface these
    non_relevant = ["ghana", "indonesia", "nigeria", "kenya", "uganda",
                    "tanzania", "ethiopia", "bangladesh", "vietnam",
                    "cambodia", "myanmar"]
    if any(w in loc_lower for w in non_relevant):
        return 0, f"non_relevant_location:{location[:30]}"

    return 4, f"other:{location[:30]}"


# ── Excluded role detection ───────────────────────────────────────────────────

def is_excluded(title: str, description: str, profile: dict) -> tuple[bool, str]:
    """Return (True, reason) if role should be excluded entirely."""
    excluded_kws = profile.get("excluded_keywords", [])
    text = f"{title} {description}".lower()

    for kw in excluded_kws:
        if kw.lower() in text:
            return True, f"excluded_keyword:{kw}"

    excluded_sectors_text = [s.lower() for s in profile.get("excluded_sectors", [])]
    for s in excluded_sectors_text:
        if s in text:
            return True, f"excluded_sector:{s}"

    return False, ""


# ── Main scoring function ─────────────────────────────────────────────────────

def score_job(job: dict, watchlist: dict, profile: dict) -> tuple[int, dict]:
    """
    Score a single job. Returns (total_score, breakdown_dict).
    """
    title       = job.get("title", "")
    description = job.get("description", "")
    company     = job.get("company_name", "")
    location    = job.get("location", "")
    sector      = job.get("sector", "")

    # Check exclusions first
    excluded, reason = is_excluded(title, description, profile)
    if excluded:
        return -1, {"excluded": reason}

    # Score each component
    s_company,  d_company  = score_company_watchlist(company, watchlist)
    s_earning,  d_earning  = score_earning_signals(f"{title} {description}", profile)
    s_keywords, d_keywords = score_skills_keywords(title, description, profile)
    s_sector,   d_sector   = score_sector(sector, profile)
    s_location, d_location = score_location(location, profile)

    total = s_company + s_earning + s_keywords + s_sector + s_location

    breakdown = {
        "company_watchlist_match": {"score": s_company,  "detail": d_company},
        "earning_signal_match":    {"score": s_earning,  "detail": d_earning},
        "skills_keyword_match":    {"score": s_keywords, "detail": d_keywords},
        "sector_match":            {"score": s_sector,   "detail": d_sector},
        "location_match":          {"score": s_location, "detail": d_location},
        "total":                   total,
    }

    return total, breakdown


# ── Description enrichment ───────────────────────────────────────────────────

def enrich_descriptions(conn) -> int:
    """
    Second-pass description fetcher.
    For jobs with empty descriptions, attempts to fetch from known ATS APIs.
    Returns number of jobs enriched.
    """
    import requests
    from bs4 import BeautifulSoup
    import re

    cursor = conn.cursor()
    empty_jobs = cursor.execute(
        "SELECT id, url, company_name FROM jobs WHERE (description IS NULL OR description = '') AND dismissed = 0"
    ).fetchall()

    log.info(f"Enriching descriptions for {len(empty_jobs)} jobs")
    enriched = 0

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)"
    }

    for job in empty_jobs:
        job_id = job["id"]
        url    = job["url"] or ""
        desc   = None

        try:
            # Greenhouse individual job
            m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
            if m:
                slug  = m.group(1)
                gh_id = m.group(2)
                resp = requests.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{gh_id}?content=true",
                    headers=HEADERS, timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    raw  = data.get("content", "") or ""
                    if raw:
                        soup = BeautifulSoup(raw, "lxml")
                        desc = soup.get_text(separator=" ", strip=True)[:3000]

            # Lever individual job
            if not desc:
                m = re.search(r"jobs\.lever\.co/([^/]+)/([a-f0-9-]{36})", url)
                if m:
                    slug    = m.group(1)
                    post_id = m.group(2)
                    resp = requests.get(
                        f"https://api.lever.co/v0/postings/{slug}/{post_id}",
                        headers=HEADERS, timeout=10
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        raw  = data.get("descriptionPlain", "") or data.get("description", "") or ""
                        if raw and "<" in raw:
                            soup = BeautifulSoup(raw, "lxml")
                            desc = soup.get_text(separator=" ", strip=True)[:3000]
                        elif raw:
                            desc = raw[:3000]

            # Ashby individual job
            if not desc:
                m = re.search(r"ashbyhq\.com/([^/]+)/([a-f0-9-]{36})", url)
                if m:
                    slug    = m.group(1)
                    post_id = m.group(2)
                    resp = requests.get(
                        "https://jobs.ashbyhq.com/api/non-user-graphql",
                        json={"operationName": "ApiJobPosting",
                              "variables": {"jobPostingId": post_id},
                              "query": "query ApiJobPosting($jobPostingId: String!) { jobPosting(jobPostingId: $jobPostingId) { title descriptionSections { descriptionHtml } } }"},
                        headers={**HEADERS, "Content-Type": "application/json"},
                        timeout=10
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        sections = (data.get("data", {})
                                       .get("jobPosting", {})
                                       .get("descriptionSections", []))
                        raw = " ".join(s.get("descriptionHtml", "") for s in sections)
                        if raw:
                            soup = BeautifulSoup(raw, "lxml")
                            desc = soup.get_text(separator=" ", strip=True)[:3000]

            if desc:
                cursor.execute(
                    "UPDATE jobs SET description = ? WHERE id = ?",
                    (desc, job_id)
                )
                enriched += 1

        except Exception as e:
            log.warning(f"Enrichment failed for job {job_id}: {e}")
            continue

    conn.commit()
    log.info(f"Enriched {enriched} job descriptions")
    return enriched


# ── Database operations ───────────────────────────────────────────────────────

def load_watchlist(conn) -> dict:
    """Load watchlist companies into a dict keyed by lowercase name."""
    rows = conn.execute(
        "SELECT name, sector, priority FROM companies WHERE active = 1"
    ).fetchall()
    return {
        r["name"].lower(): {"sector": r["sector"], "priority": r["priority"]}
        for r in rows
    }


def run_scoring(rescore_all: bool = False) -> dict:
    """
    Score all unscored jobs (or all jobs if rescore_all=True).
    Returns summary dict.
    """
    conn    = get_conn()
    profile = get_profile()
    watchlist = load_watchlist(conn)

    # Enrich empty descriptions before scoring
    enrich_descriptions(conn)

    if rescore_all:
        jobs = conn.execute("SELECT * FROM jobs WHERE dismissed = 0").fetchall()
        log.info(f"Re-scoring all {len(jobs)} jobs")
    else:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE score = 0 AND dismissed = 0"
        ).fetchall()
        log.info(f"Scoring {len(jobs)} unscored jobs")

    scored       = 0
    excluded     = 0
    notifiable   = 0
    min_save     = profile["scoring"]["min_score_to_save"]
    min_notify   = profile["scoring"]["min_score_to_notify"]

    for job in jobs:
        job = dict(job)
        total, breakdown = score_job(job, watchlist, profile)

        if total == -1:
            # Excluded role — mark dismissed
            conn.execute(
                "UPDATE jobs SET score = -1, score_breakdown = ?, dismissed = 1 WHERE id = ?",
                (json.dumps(breakdown), job["id"])
            )
            excluded += 1
            continue

        notified = 1 if total >= min_notify else 0

        conn.execute(
            """
            UPDATE jobs
            SET score = ?, score_breakdown = ?, notified = ?
            WHERE id = ?
            """,
            (total, json.dumps(breakdown), notified, job["id"])
        )
        scored += 1

        if total >= min_notify:
            notifiable += 1
            log.info(
                f"  HIGH SCORE {total}/100 — {job['title']} @ {job['company_name']} "
                f"[{job.get('sector', 'unknown')}]"
            )
        elif total >= min_save:
            log.info(
                f"  {total}/100 — {job['title']} @ {job['company_name']}"
            )

    conn.commit()
    conn.close()

    summary = {
        "jobs_scored":   scored,
        "jobs_excluded": excluded,
        "notifiable":    notifiable,
        "min_save":      min_save,
        "min_notify":    min_notify,
    }

    log.info(
        f"Scoring complete — {scored} scored, {excluded} excluded, "
        f"{notifiable} above notify threshold"
    )
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Score jobs against Shannon's profile")
    parser.add_argument("--rescore-all", action="store_true",
                        help="Re-score all jobs, not just unscored ones")
    args = parser.parse_args()
    result = run_scoring(rescore_all=args.rescore_all)
    print(f"\nSummary: {result}")
