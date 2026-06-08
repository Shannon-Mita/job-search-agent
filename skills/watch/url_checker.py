"""
skills/watch/url_checker.py

Checks all watchlist company career page URLs for dead links
and attempts to find the correct ATS URL automatically.

Run:
    python skills/watch/url_checker.py --status failed
    python skills/watch/url_checker.py --all
"""

import sys
import time
import logging
import requests
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "skills" / "profile"))
from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("watch.url_checker")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

ATS_PATTERNS = [
    "https://boards.greenhouse.io/{slug}",
    "https://jobs.lever.co/{slug}",
    "https://apply.workable.com/{slug}",
    "https://{slug}.workable.com",
    "https://{slug}.bamboohr.com/jobs/",
    "https://jobs.ashbyhq.com/{slug}",
    "https://{slug}.teamtailor.com/jobs",
    "https://careers.smartrecruiters.com/{slug}",
]


def slugify(name: str) -> str:
    return name.lower().replace(" ", "").replace("-", "").replace(".", "")


def slugify_hyphen(name: str) -> str:
    return name.lower().replace(" ", "-").replace(".", "")


def check_url(url: str, timeout: int = 8) -> tuple:
    """Returns (is_live, status_code). Validates content, not just HTTP status."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return False, resp.status_code

        text_lower = resp.text.lower()
        false_positive_phrases = [
            "page not found",
            "404 not found",
            "this page doesn't exist",
            "we couldn't find that page",
            "sorry, we can't find",
        ]
        if any(phrase in text_lower for phrase in false_positive_phrases):
            return False, 404

        if "workable.com" in url:
            # Validate via API — Workable returns 200 for unknown slugs on the HTML page
            import re as _re
            slug = None
            m = _re.search(r"apply\.workable\.com/([^/?#]+)", url)
            if m:
                slug = m.group(1)
            if slug:
                try:
                    api_resp = requests.post(
                        f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
                        json={"query": "", "location": [], "department": [],
                              "worktype": [], "remote": []},
                        headers={**HEADERS, "Content-Type": "application/json"},
                        timeout=timeout,
                    )
                    if api_resp.status_code != 200:
                        return False, 404
                except Exception:
                    return False, 0

        # Greenhouse: real accounts return JSON with jobs array
        if "greenhouse.io" in url:
            try:
                slug = url.rstrip("/").split("/")[-1]
                api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
                r = requests.get(api, timeout=6)
                if r.status_code != 200:
                    return False, 404
                data = r.json()
                if "jobs" not in data:
                    return False, 404
            except Exception:
                return False, 0

        # Lever: real accounts return a jobs array
        if "lever.co" in url:
            try:
                slug = url.rstrip("/").split("/")[-1]
                api = f"https://api.lever.co/v0/postings/{slug}"
                r = requests.get(api, timeout=6)
                if r.status_code != 200:
                    return False, 404
                if not isinstance(r.json(), list):
                    return False, 404
            except Exception:
                return False, 0

        return True, 200
    except requests.exceptions.RequestException:
        return False, 0


def find_ats_url(company_name: str) -> Optional[str]:
    """Try common ATS patterns to find a working career page."""
    slug_plain  = slugify(company_name)
    slug_hyphen = slugify_hyphen(company_name)

    for pattern in ATS_PATTERNS:
        for slug in [slug_plain, slug_hyphen]:
            url = pattern.replace("{slug}", slug)
            live, code = check_url(url, timeout=6)
            if live:
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=6)
                    if len(resp.text) < 1000:
                        continue
                except Exception:
                    continue
                return url
            time.sleep(0.5)
    return None


def run(status_filter: Optional[str] = None, fix: bool = False):
    conn   = get_conn()
    cursor = conn.cursor()

    if status_filter == "failed":
        query = """
            SELECT DISTINCT c.id, c.name, c.career_page_url
            FROM companies c
            JOIN watch_log w ON w.company_id = c.id
            WHERE w.status = 'failed'
            AND c.active = 1
            ORDER BY c.name
        """
        companies = cursor.execute(query).fetchall()
    else:
        companies = cursor.execute(
            "SELECT id, name, career_page_url FROM companies WHERE active = 1 ORDER BY name"
        ).fetchall()

    log.info(f"Checking {len(companies)} companies")

    results = {"live": [], "dead_fixed": [], "dead_unfixed": []}

    for company in companies:
        cid, name, url = company["id"], company["name"], company["career_page_url"]
        live, code = check_url(url)

        if live:
            results["live"].append(name)
            log.info(f"  ✓ {name}")
        else:
            log.warning(f"  ✗ {name} ({code}) — searching for ATS URL...")
            new_url = find_ats_url(name)

            if new_url:
                log.info(f"    → Found: {new_url}")
                results["dead_fixed"].append((name, url, new_url))
                if fix:
                    cursor.execute(
                        "UPDATE companies SET career_page_url = ? WHERE id = ?",
                        (new_url, cid)
                    )
                    conn.commit()
                    log.info(f"    ✓ Updated in database")
            else:
                log.warning(f"    → No ATS URL found — manual check needed")
                results["dead_unfixed"].append((name, url))

        time.sleep(1)

    conn.close()

    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"{'='*60}")
    print(f"Live:                    {len(results['live'])}")
    print(f"Dead — auto-fixed:       {len(results['dead_fixed'])}")
    print(f"Dead — needs manual fix: {len(results['dead_unfixed'])}")

    if results["dead_fixed"]:
        print(f"\nAuto-fixed URLs:")
        for name, old, new in results["dead_fixed"]:
            print(f"  {name}")
            print(f"    OLD: {old}")
            print(f"    NEW: {new}")

    if results["dead_unfixed"]:
        print(f"\nNeeds manual fix:")
        for name, url in results["dead_unfixed"]:
            print(f"  {name}: {url}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", choices=["failed"], help="Only check previously failed companies")
    parser.add_argument("--fix", action="store_true", help="Auto-update fixed URLs in database")
    parser.add_argument("--all", action="store_true", help="Check all companies")
    args = parser.parse_args()
    run(status_filter=args.status, fix=args.fix)
