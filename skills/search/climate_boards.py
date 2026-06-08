"""
skills/search/climate_boards.py

Scrapes climate-specific job boards for relevant roles.

Tier 1 boards (high relevance):
  - Climatebase        climatebase.org/jobs
  - inClimate          inclimate.com/jobs
  - Terra.do           terra.do/climate-jobs/job-board
  - Work on Climate    workonclimate.org/jobs
  - Green Jobs Network greenjobsnetwork.com

Tier 2 boards (broader signal):
  - Escape the City    escapethecity.org/explore
  - Enable Green       enable.green/jobs
  - GreenJobs UK       greenjobs.co.uk

Run standalone:
    python skills/search/climate_boards.py --tier 1
"""

import sys
import time
import hashlib
import logging
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "skills" / "profile"))

from db import get_conn, get_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("search.climate_boards")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

REQUEST_TIMEOUT    = 20
DELAY_BETWEEN      = 3
MAX_DESCRIPTION    = 3000
MAX_JOBS_PER_BOARD = 100


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


def _looks_js_rendered(soup: BeautifulSoup) -> bool:
    text = soup.get_text(strip=True)
    if len(text) < 500:
        return True
    js_indicators = [
        "enable javascript", "javascript is required",
        "loading...", "__NEXT_DATA__",
    ]
    return any(phrase in text.lower() for phrase in js_indicators)


def fetch_playwright(url: str) -> Optional[BeautifulSoup]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("Playwright not installed")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            )
            page.goto(url, wait_until="networkidle", timeout=30000)
            for selector in ["[class*='job']", "[class*='position']",
                             "[class*='opening']", "article"]:
                try:
                    page.wait_for_selector(selector, timeout=5000)
                    break
                except PWTimeout:
                    continue
            html = page.content()
            browser.close()
            return BeautifulSoup(html, "lxml")
    except Exception as e:
        log.warning(f"Playwright failed for {url}: {e}")
        return None


def fetch(url: str, params: dict = None) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, params=params,
                           timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            if not _looks_js_rendered(soup):
                return soup
            log.info(f"  JS-rendered, switching to Playwright: {url}")
            return fetch_playwright(url)
        log.warning(f"HTTP {resp.status_code}: {url}")
    except requests.exceptions.RequestException as e:
        log.warning(f"Fetch error: {url} — {e}")
    return None


def _extract_cards(soup, base_url, source, selectors=None):
    jobs = []
    if selectors is None:
        selectors = [
            "[class*='job-card']", "[class*='job-item']",
            "[class*='job-listing']", "[class*='position']",
            "[class*='opening']", "[class*='role']",
            "article", "li[class]",
        ]
    for selector in selectors:
        cards = soup.select(selector)
        if not cards:
            continue
        for card in cards[:MAX_JOBS_PER_BOARD]:
            title_el = card.find(["h2", "h3", "h4", "strong"])
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if len(title) < 5 or len(title) > 150:
                continue
            comp_el  = card.find(class_=re.compile(r"company|org|employer", re.I))
            company  = comp_el.get_text(strip=True) if comp_el else "Unknown"
            loc_el   = card.find(class_=re.compile(r"location|city|remote", re.I))
            location = loc_el.get_text(strip=True) if loc_el else ""
            sal_el   = card.find(class_=re.compile(r"salary|compensation|pay", re.I))
            salary   = sal_el.get_text(strip=True) if sal_el else ""
            link     = card.find("a", href=True)
            if link:
                href = link["href"]
                job_url = href if href.startswith("http") else urljoin(base_url, href)
            else:
                job_url = base_url
            jobs.append({
                "title": title, "company_name": company,
                "location": location, "salary_raw": salary,
                "url": job_url,
                "description": card.get_text(separator=" ", strip=True)[:MAX_DESCRIPTION],
                "source": source,
            })
        if jobs:
            return jobs
    return jobs


def scrape_climatebase() -> list:
    jobs = []
    profile  = get_profile()
    keywords = profile.get("skills_keywords", {}).get("core") or profile.get("title_keywords", [])
    keywords = keywords[:5]
    for keyword in keywords:
        soup = fetch("https://climatebase.org/jobs",
                    params={"q": keyword, "location": "United Kingdom"})
        if soup:
            found = _extract_cards(soup, "https://climatebase.org", "climatebase")
            jobs.extend(found)
            log.info(f"  climatebase [{keyword}]: {len(found)} roles")
        time.sleep(DELAY_BETWEEN)
    return jobs


def scrape_inclimate() -> list:
    soup = fetch("https://www.inclimate.com/jobs")
    if not soup:
        return []
    return _extract_cards(soup, "https://www.inclimate.com", "inclimate")


def scrape_terra_do() -> list:
    soup = fetch("https://www.terra.do/climate-jobs/job-board/")
    if not soup:
        return []
    return _extract_cards(soup, "https://www.terra.do", "terra.do")


def scrape_work_on_climate() -> list:
    soup = fetch("https://workonclimate.org/jobs/")
    if not soup:
        return []
    return _extract_cards(soup, "https://workonclimate.org", "workonclimate")


def scrape_green_jobs_network() -> list:
    soup = fetch("https://greenjobsnetwork.com/jobs/")
    if not soup:
        return []
    return _extract_cards(soup, "https://greenjobsnetwork.com", "greenjobsnetwork",
                          selectors=[".job_listing", "[class*='job-listing']"])


def scrape_escape_the_city() -> list:
    soup = fetch("https://www.escapethecity.org/explore")
    if not soup:
        return []
    return _extract_cards(soup, "https://www.escapethecity.org", "escape_the_city",
                          selectors=["[class*='opportunity']", "[class*='job']", "article"])


def scrape_enable_green() -> list:
    soup = fetch("https://enable.green/jobs/")
    if not soup:
        return []
    return _extract_cards(soup, "https://enable.green", "enable_green")


def scrape_greenjobs_uk() -> list:
    soup = fetch("https://www.greenjobs.co.uk/browse-jobs/")
    if not soup:
        return []
    jobs = _extract_cards(soup, "https://www.greenjobs.co.uk", "greenjobs_uk",
                          selectors=[".job", "[class*='job-item']", "table tr"])
    return [j for j in jobs if j["title"] not in ("Job Title", "")]


BOARDS = [
    {"name": "Climatebase",        "fn": scrape_climatebase,        "tier": 1},
    {"name": "inClimate",          "fn": scrape_inclimate,          "tier": 1},
    {"name": "Terra.do",           "fn": scrape_terra_do,           "tier": 1},
    {"name": "Work on Climate",    "fn": scrape_work_on_climate,    "tier": 1},
    {"name": "Green Jobs Network", "fn": scrape_green_jobs_network, "tier": 1},
    {"name": "Escape the City",    "fn": scrape_escape_the_city,    "tier": 2},
    {"name": "Enable Green",       "fn": scrape_enable_green,       "tier": 2},
    {"name": "GreenJobs UK",       "fn": scrape_greenjobs_uk,       "tier": 2},
]


def flag_new_companies(conn, jobs: list):
    cursor = conn.cursor()
    watchlist = {
        r[0].lower() for r in cursor.execute("SELECT name FROM companies").fetchall()
    }
    new_companies = {
        j["company_name"].strip() for j in jobs
        if j.get("company_name", "").strip().lower() not in watchlist
        and j.get("company_name", "") not in ("", "Unknown")
    }
    for company in new_companies:
        try:
            cursor.execute(
                """
                INSERT INTO signals (company_name, signal_type, source, summary)
                VALUES (?, 'hiring_post', 'climate_board',
                        'Hiring on climate boards — not in watchlist. Consider adding.')
                """,
                (company,),
            )
        except Exception:
            pass
    conn.commit()
    if new_companies:
        log.info(f"  {len(new_companies)} new companies flagged for watchlist review")


def save_jobs(conn, jobs: list) -> tuple:
    cursor    = conn.cursor()
    new_count = 0
    watchlist = {
        r["name"].lower(): r["id"]
        for r in cursor.execute("SELECT id, name FROM companies").fetchall()
    }
    for job in jobs:
        fingerprint = make_fingerprint(
            job["company_name"], job["title"], job["source"]
        )
        salary_min, salary_max = parse_salary(job.get("salary_raw", ""))
        company_id = watchlist.get(job["company_name"].lower())
        existing = cursor.execute(
            "SELECT id FROM jobs WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing:
            cursor.execute(
                "UPDATE jobs SET last_seen = datetime('now') WHERE fingerprint = ?",
                (fingerprint,),
            )
        else:
            cursor.execute(
                """
                INSERT INTO jobs (
                    fingerprint, title, company_name, company_id,
                    location, salary_raw, salary_min, salary_max,
                    url, description, source, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dream')
                """,
                (
                    fingerprint, job["title"], job["company_name"], company_id,
                    job.get("location", ""), job.get("salary_raw", ""),
                    salary_min, salary_max,
                    job["url"], job.get("description", ""), job["source"],
                ),
            )
            new_count += 1
    conn.commit()
    return new_count, len(jobs)


def scrape_all_boards(tier_filter: Optional[int] = None) -> dict:
    conn     = get_conn()
    all_jobs = []
    results  = {}

    boards = [b for b in BOARDS if tier_filter is None or b["tier"] == tier_filter]
    log.info(f"Scraping {len(boards)} climate job boards")

    for board in boards:
        log.info(f"Scraping: {board['name']}")
        try:
            jobs = board["fn"]()
            results[board["name"]] = len(jobs)
            all_jobs.extend(jobs)
            log.info(f"  → {len(jobs)} roles found")
        except Exception as e:
            log.error(f"  Failed: {board['name']} — {e}")
            results[board["name"]] = 0
        time.sleep(DELAY_BETWEEN)

    new_count, total = save_jobs(conn, all_jobs)
    flag_new_companies(conn, all_jobs)
    conn.close()

    summary = {
        "boards_scraped":  len(boards),
        "jobs_found":      total,
        "jobs_new":        new_count,
        "board_breakdown": results,
        "timestamp":       datetime.now().isoformat(),
    }
    log.info(f"Boards complete — {total} roles found, {new_count} new")
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scrape climate job boards")
    parser.add_argument("--tier", type=int, choices=[1, 2], help="Run only tier 1 or 2")
    args = parser.parse_args()
    result = scrape_all_boards(tier_filter=args.tier)
    print(f"\nSummary: {json.dumps(result, indent=2)}")
