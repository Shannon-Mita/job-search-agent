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
    10 points max for simple scoring.
    Strict — roles must be UK, Remote, or Australia to score positively.
    Anything else scores 0 and should not be notified.
    """
    if not location or location.strip() == "":
        return 0, "no_location_info"

    loc_lower = location.lower().strip()

    # Remote — always acceptable
    if any(w in loc_lower for w in ["remote", "anywhere", "distributed", "worldwide", "global"]):
        if any(w in loc_lower for w in ["uk", "united kingdom", "britain", "england", "london"]):
            return 10, "remote_uk"
        return 9, "remote_global"

    # UK locations — acceptable
    uk_terms = [
        "london", "manchester", "bristol", "edinburgh", "birmingham",
        "leeds", "oxford", "cambridge", "glasgow", "liverpool",
        "uk", "united kingdom", "england", "scotland", "wales",
        "britain", "hybrid", "sheffield", "nottingham", "reading",
        "brighton", "bath", "coventry", "leicester",
    ]
    if any(term in loc_lower for term in uk_terms):
        if "hybrid" in loc_lower:
            return 10, "hybrid_uk"
        return 8, "onsite_uk"

    # Australia — acceptable, Shannon open to relocation
    au_terms = ["australia", "sydney", "melbourne", "brisbane", "perth", "adelaide"]
    if any(term in loc_lower for term in au_terms):
        return 7, "australia"

    # Everything else — not relevant, score 0
    # This includes US, Europe, Asia, Africa, etc.
    return 0, f"non_relevant:{location[:40]}"


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


def run_simple_scoring(rescore_all: bool = False) -> dict:
    """
    Simplified scoring — surfaces any role that matches on company OR title.
    No description needed. Company on watchlist = show it.
    Title keyword match = show it.
    """
    conn     = get_conn()
    profile  = get_profile()
    watchlist = load_watchlist(conn)

    title_keywords = profile.get("title_keywords", [])
    target_titles  = [t.lower() for t in profile.get("target_titles", [])]
    all_title_terms = title_keywords + target_titles

    if rescore_all:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE dismissed = 0"
        ).fetchall()
    else:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE score = 0 AND dismissed = 0"
        ).fetchall()

    log.info(f"Simple scoring {len(jobs)} jobs")

    scored     = 0
    notifiable = 0
    excluded   = 0

    for job in jobs:
        job = dict(job)
        title       = (job.get("title") or "").lower()
        company     = (job.get("company_name") or "").lower()
        location    = (job.get("location") or "").lower()
        description = (job.get("description") or "").lower()

        # Hard exclusions first
        excluded_flag, reason = is_excluded(
            job.get("title", ""), job.get("description", ""), profile
        )
        if excluded_flag:
            conn.execute(
                "UPDATE jobs SET score = -1, dismissed = 1 WHERE id = ?",
                (job["id"],)
            )
            excluded += 1
            continue

        score = 0
        breakdown = {}

        # Component 1: Company on watchlist (0-30)
        company_score, company_detail = score_company_watchlist(
            job.get("company_name", ""), watchlist
        )
        score += company_score
        breakdown["company"] = {"score": company_score, "detail": company_detail}

        # Component 2: Title keyword match (0-40)
        title_score = 0
        matched_terms = []
        for term in all_title_terms:
            if term.lower() in title:
                title_score = 40
                matched_terms.append(term)
                break
        # Partial match — title contains relevant words
        if title_score == 0:
            relevant_words = [
                "business development", "head of bd",
                "partnerships", "head of partnerships",
                "chief of staff",
                "people operations", "head of people", "head of talent",
                "hr operations", "head of hr",
                "sales enablement", "head of sales enablement",
                "global mobility", "head of global mobility",
                "market entry",
                "go-to-market", "gtm",
                "workforce", "talent acquisition",
                "commercial director", "head of commercial",
                "vp operations", "head of operations", "director of operations",
            ]
            for word in relevant_words:
                if word in title:
                    title_score = 20
                    matched_terms.append(word)
                    break
        score += title_score
        breakdown["title"] = {"score": title_score, "matched": matched_terms}

        # Component 3: Location (0-15)
        loc_score, loc_detail = score_location(job.get("location", ""), profile)
        # Rescale to 15 max
        loc_score = int(loc_score * 1.5)
        score += loc_score
        breakdown["location"] = {"score": loc_score, "detail": loc_detail}

        # Component 4: Sector (0-15)
        sec_score, sec_detail = score_sector(job.get("sector", ""), profile)
        score += sec_score
        breakdown["sector"] = {"score": sec_score, "detail": sec_detail}

        # Notify logic:
        # Watchlist company + relevant title = notify (location trusted)
        # OR strong title match + confirmed location = notify (open market)
        notify = 1 if (
            (company_score >= 12 and title_score >= 20)
            or
            (title_score >= 40 and loc_score > 0)
        ) else 0

        conn.execute(
            """
            UPDATE jobs SET score = ?, score_breakdown = ?, notified = ?
            WHERE id = ?
            """,
            (score, json.dumps(breakdown), notify, job["id"])
        )
        scored += 1
        if notify:
            notifiable += 1
            log.info(f"  NOTIFY — {job.get('title')} @ {job.get('company_name')} [{score}pts]")

    conn.commit()
    conn.close()

    summary = {
        "jobs_scored":   scored,
        "jobs_excluded": excluded,
        "notifiable":    notifiable,
    }
    log.info(f"Done — {scored} scored, {notifiable} notifiable, {excluded} excluded")
    return summary


# Use simple scoring as default
run_scoring = run_simple_scoring


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Score jobs against Shannon's profile")
    parser.add_argument("--rescore-all", action="store_true",
                        help="Re-score all jobs, not just unscored ones")
    args = parser.parse_args()
    result = run_scoring(rescore_all=args.rescore_all)
    print(f"\nSummary: {result}")
