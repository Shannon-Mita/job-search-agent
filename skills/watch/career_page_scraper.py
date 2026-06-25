"""
skills/watch/career_page_scraper.py

Scrapes career pages for all active watchlist companies.
Detects new job postings before they appear on aggregators.

Run standalone:
    python skills/watch/career_page_scraper.py --limit 3 --priority HIGH
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
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "skills" / "profile"))

from db import get_conn, get_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("watch.career_pages")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_TIMEOUT  = 15
DELAY_BETWEEN    = 2
MAX_RETRIES      = 2
MAX_DESCRIPTION  = 3000


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


def make_fingerprint(company: str, title: str, location: str) -> str:
    raw = f"{company.lower().strip()}|{title.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha1(raw.encode()).hexdigest()


# Common words that appear in real job titles
JOB_TITLE_INDICATORS = {
    "manager", "director", "lead", "head", "engineer", "developer",
    "analyst", "specialist", "coordinator", "associate", "officer",
    "designer", "scientist", "consultant", "advisor", "executive",
    "architect", "strategist", "partner", "recruiter", "talent",
    "operations", "marketing", "sales", "finance", "legal", "people",
    "product", "commercial", "business", "technical", "senior", "junior",
    "principal", "vp", "cto", "coo", "cfo", "cmo", "chief", "intern",
    "apprentice", "graduate", "research", "data", "software", "hardware",
    "supply", "procurement", "logistics", "communications", "content",
}

# Phrases that definitively indicate non-job content
NON_JOB_PHRASES = {
    "explore open roles", "view open roles", "open roles", "see all roles",
    "view all jobs", "all jobs", "see open positions", "view positions",
    "privacy overview", "current job openings", "connect with us",
    "no open positions", "no current openings", "check back later",
    "join our team", "work with us", "our culture", "our values",
    "learn more", "find out more", "read more", "see more", "view more",
    "embrace a visionary mindset", "building a community",
    "remote by design", "cookie policy", "privacy policy",
}

LOCATION_TERMS = [
    # UK cities
    "London", "Manchester", "Bristol", "Edinburgh", "Birmingham",
    "Leeds", "Oxford", "Cambridge", "Glasgow", "Liverpool",
    "Brighton", "Bath", "Reading", "Sheffield", "Nottingham",
    "Newcastle", "Cardiff", "Belfast", "Exeter", "Norwich",
    # UK general
    "United Kingdom", "UK", "England", "Scotland", "Wales",
    # Work type
    "Remote", "Hybrid",
    # Australia
    "Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide",
    "Australia",
]


def clean_title_extract_location(raw: str) -> tuple[str, str]:
    """
    Strip location/UI artifacts from scraped title text and extract location.
    Returns (cleaned_title, location_or_empty).
    Only extracts locations present in LOCATION_TERMS (UK/Remote/AU).
    Non-matching cities remain in the title but location is left empty.
    """
    title = raw.strip()
    location = ""

    # Extract "Location: X" pattern and remove it from title
    loc_match = re.search(r'\bLocation:\s*([^\n]+)', title, re.I)
    if loc_match:
        candidate = loc_match.group(1).strip()
        for term in LOCATION_TERMS:
            if term.lower() in candidate.lower():
                location = term
                break
        title = (title[:loc_match.start()] + title[loc_match.end():]).strip()

    # Split on camelCase / uppercase-run boundaries (e.g. "ManagerLondon")
    title = re.sub(r'([a-z])([A-Z][a-z])', r'\1 | \2', title)
    title = re.sub(r'([A-Z]{2,})([A-Z][a-z])', r'\1 | \2', title)
    if " | " in title:
        title = title.split(" | ")[0].strip()

    # Strip trailing UI artifacts
    for suffix in [
        " Full Time", " Part Time", " Contract", " Permanent", " Temporary",
        "Full Time", "Part Time", " View Job", "View Job",
        " Apply Now", "Apply Now", " Learn More", "Learn More",
    ]:
        if title.endswith(suffix):
            title = title[:-len(suffix)].strip()

    # Extract a known location term from the end of the cleaned title
    if not location:
        for term in sorted(LOCATION_TERMS, key=len, reverse=True):
            if title.lower().endswith(term.lower()):
                location = term
                title = title[:-len(term)].strip().rstrip(",– -").strip()
                break

    return title, location


def is_valid_job_title(title: str) -> bool:
    """
    Returns True if the title looks like a real job listing.
    Filters out product names, marketing copy, nav links, culture text.
    """
    if not title or len(title) < 5 or len(title) > 120:
        return False

    title_lower = title.lower().strip()

    # Reject known non-job phrases
    if title_lower in NON_JOB_PHRASES:
        return False
    for phrase in NON_JOB_PHRASES:
        if title_lower.startswith(phrase):
            return False

    # Reject if ends with punctuation suggesting marketing copy
    if title.endswith((".", "!", "?", "...")):
        return False

    # Reject if contains @ with a brand name (product listing pattern)
    if " @ " in title and not any(w in title.lower() for w in ["manager", "engineer", "director"]):
        return False

    # Reject if all words are capitalised (likely a heading, not a title)
    words = title.split()
    if len(words) >= 3 and all(w[0].isupper() for w in words if len(w) > 3):
        # Allow standard title case job titles but reject ALL CAPS
        if title.isupper():
            return False

    # Must contain at least one job title indicator word
    if not any(indicator in title_lower for indicator in JOB_TITLE_INDICATORS):
        return False

    # Reject "Firstname Lastname - Something" pattern (person name, not a job)
    import re as _re
    if _re.match(r'^[A-Z][a-z]+ [A-Z][a-z]+ [-–]', title):
        return False

    # Reject if starts with a gerund (description fragment, not a title)
    gerund_starters = [
        "hiring", "building", "developing", "retaining", "managing",
        "leading", "growing", "driving", "creating", "supporting",
        "working", "helping", "making", "ensuring", "delivering",
    ]
    first_word = title_lower.split()[0] if title_lower.split() else ""
    if first_word in gerund_starters:
        return False

    return True


def _looks_js_rendered(soup: BeautifulSoup) -> bool:
    text = soup.get_text(strip=True)
    if len(text) < 500:
        return True
    js_indicators = [
        "enable javascript", "javascript is required",
        "loading...", "__NEXT_DATA__",
    ]
    if any(phrase in text.lower() for phrase in js_indicators):
        return True
    html = str(soup)
    if any(marker in html for marker in [
        "workable.com/widgets", "greenhouse-io", "lever.co/v0",
        "ashbyhq.com", "app.bamboohr",
    ]):
        return True
    return False


def fetch_page_playwright(url: str) -> Optional[BeautifulSoup]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("Playwright not installed — skipping JS rendering")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
            )
            page.goto(url, wait_until="networkidle", timeout=30000)
            for selector in ["[class*='job']", "[class*='position']", "[class*='opening']"]:
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


def fetch_workable_api(career_url: str) -> Optional[list]:
    """
    Fetch jobs directly from Workable's public JSON API.
    Returns list of job dicts or None if not a Workable page.
    """
    import re
    slug = None
    m = re.search(r"apply\.workable\.com/([^/?#]+)", career_url)
    if m:
        slug = m.group(1)
    else:
        m = re.search(r"([^/?#]+)\.workable\.com", career_url)
        if m:
            slug = m.group(1)

    if not slug:
        return None

    api_url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    try:
        resp = requests.post(
            api_url,
            json={"query": "", "location": [], "department": [], "worktype": [], "remote": []},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"Workable API {resp.status_code} for slug: {slug}")
            return None

        data = resp.json()
        jobs = []
        for job in data.get("results", []):
            location_parts = [
                job.get("location", {}).get("city", ""),
                job.get("location", {}).get("country", ""),
            ]
            location = ", ".join(p for p in location_parts if p)
            remote   = job.get("remote", False)
            if remote and not location:
                location = "Remote"
            elif remote:
                location = f"Remote / {location}"

            jobs.append({
                "title":       job.get("title", ""),
                "location":    location,
                "salary_raw":  "",
                "url":         f"https://apply.workable.com/{slug}/j/{job.get('shortcode', '')}",
                "description": job.get("description", "")[:MAX_DESCRIPTION],
                "extraction_method": "workable-api",
            })

        log.info(f"  Workable API: {len(jobs)} jobs for {slug}")
        return jobs

    except Exception as e:
        log.warning(f"Workable API error for {slug}: {e}")
        return None


def fetch_greenhouse_api(career_url: str) -> Optional[list]:
    """Fetch jobs from Greenhouse public API with full descriptions."""
    import re
    m = re.search(r"greenhouse\.io/([^/?#]+)", career_url)
    if not m:
        return None
    slug = m.group(1)
    try:
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            headers=HEADERS, timeout=15
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        jobs = []
        for job in data.get("jobs", []):
            location = job.get("location", {}).get("name", "")
            # Strip HTML from description
            raw_content = job.get("content", "") or ""
            if raw_content:
                desc_soup = BeautifulSoup(raw_content, "lxml")
                description = desc_soup.get_text(separator=" ", strip=True)[:MAX_DESCRIPTION]
            else:
                description = ""
            jobs.append({
                "title":       job.get("title", ""),
                "location":    location,
                "salary_raw":  "",
                "url":         job.get("absolute_url", career_url),
                "description": description,
                "extraction_method": "greenhouse-api",
            })
        log.info(f"  Greenhouse API: {len(jobs)} jobs for {slug}")
        return jobs
    except Exception as e:
        log.warning(f"Greenhouse API error for {slug}: {e}")
        return None


def fetch_lever_api(career_url: str) -> Optional[list]:
    """Fetch jobs from Lever public API."""
    import re
    m = re.search(r"lever\.co/([^/?#]+)", career_url)
    if not m:
        return None
    slug = m.group(1)
    try:
        resp = requests.get(
            f"https://api.lever.co/v0/postings/{slug}",
            headers=HEADERS, timeout=15
        )
        if resp.status_code != 200:
            return None
        postings = resp.json()
        if not isinstance(postings, list):
            return None
        jobs = []
        for job in postings:
            location = ""
            categories = job.get("categories", {})
            if isinstance(categories, dict):
                location = categories.get("location", "")
            # Strip HTML from description if present
            raw_desc = job.get("descriptionPlain", "") or job.get("description", "") or ""
            if raw_desc and "<" in raw_desc:
                desc_soup = BeautifulSoup(raw_desc, "lxml")
                description = desc_soup.get_text(separator=" ", strip=True)[:MAX_DESCRIPTION]
            else:
                description = raw_desc[:MAX_DESCRIPTION]
            jobs.append({
                "title":       job.get("text", ""),
                "location":    location,
                "salary_raw":  "",
                "url":         job.get("hostedUrl", career_url),
                "description": description,
                "extraction_method": "lever-api",
            })
        log.info(f"  Lever API: {len(jobs)} jobs for {slug}")
        return jobs
    except Exception as e:
        log.warning(f"Lever API error for {slug}: {e}")
        return None


def fetch_page(url: str) -> Optional[BeautifulSoup]:
    """
    Fetch page with requests first, fall back to Playwright for JS-rendered pages.
    For Workable, Greenhouse, and Lever URLs, uses their JSON APIs directly.
    """
    # Workable: use API directly, skip HTML scraping entirely
    if "workable.com" in url:
        jobs = fetch_workable_api(url)
        if jobs is not None:
            return "WORKABLE_API", jobs

    # Greenhouse: use API directly
    if "greenhouse.io" in url:
        jobs = fetch_greenhouse_api(url)
        if jobs is not None:
            return "GREENHOUSE_API", jobs

    # Lever: use API directly
    if "lever.co" in url:
        jobs = fetch_lever_api(url)
        if jobs is not None:
            return "LEVER_API", jobs

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                if not _looks_js_rendered(soup):
                    return soup
                log.info(f"  JS-rendered detected, switching to Playwright: {url}")
                return fetch_page_playwright(url)
            elif resp.status_code in (403, 404):
                log.warning(f"HTTP {resp.status_code}: {url}")
                return None
            else:
                log.warning(f"HTTP {resp.status_code}: {url} (attempt {attempt+1})")
        except requests.exceptions.Timeout:
            log.warning(f"Timeout: {url} (attempt {attempt+1})")
        except requests.exceptions.RequestException as e:
            log.warning(f"Request error: {url} — {e}")
        if attempt < MAX_RETRIES - 1:
            time.sleep(3)
    return None


def extract_jobs_from_page(soup: BeautifulSoup, company_name: str, career_url: str) -> list:
    jobs = []

    # Strategy 1: JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else data.get("@graph", [data])
            for item in items:
                if item.get("@type") == "JobPosting":
                    title = item.get("title", "")
                    location = ""
                    loc_data = item.get("jobLocation", {})
                    if isinstance(loc_data, dict):
                        addr = loc_data.get("address", {})
                        if isinstance(addr, dict):
                            location = addr.get("addressLocality", "") or addr.get("addressRegion", "")
                        elif isinstance(addr, str):
                            location = addr
                    salary_raw = ""
                    salary_data = item.get("baseSalary", {})
                    if isinstance(salary_data, dict):
                        val = salary_data.get("value", {})
                        if isinstance(val, dict):
                            min_v = val.get("minValue", "")
                            max_v = val.get("maxValue", "")
                            if min_v or max_v:
                                salary_raw = f"£{min_v} - £{max_v}"
                    url = item.get("url", career_url)
                    description = item.get("description", "")[:MAX_DESCRIPTION]
                    if title:
                        jobs.append({
                            "title": title.strip(),
                            "location": location.strip(),
                            "salary_raw": salary_raw,
                            "url": url,
                            "description": description,
                            "extraction_method": "json-ld",
                        })
        except (json.JSONDecodeError, AttributeError):
            continue

    if jobs:
        return jobs

    # Shared nav-phrase blocklist used by Strategy 2 and 3
    nav_phrases = {
        "view all jobs", "all jobs", "careers", "jobs", "apply now",
        "see all", "privacy overview", "current job openings", "connect with us",
        "no open positions", "no current openings", "check back later",
        "view openings", "open roles", "see open roles", "explore careers",
        "join our team", "work with us", "our team", "about us", "contact us",
        "learn more", "find out more", "read more", "see more", "view more",
    }

    # Strategy 2: Common CSS patterns
    job_selectors = [
        "[class*='job-item']", "[class*='job-card']", "[class*='job-listing']",
        "[class*='job-row']", "[class*='position']", "[class*='opening']",
        "[class*='vacancy']", "[class*='role-item']", "[class*='career-item']",
        "li.opening", "div.opening", ".job", ".jobs-list li",
        "[data-job-id]", "[data-position]",
    ]
    for selector in job_selectors:
        cards = soup.select(selector)
        if cards:
            for card in cards[:50]:
                title_el = (
                    card.find(["h2", "h3", "h4", "strong"]) or
                    card.find(class_=re.compile(r"title|name|role", re.I)) or
                    card.find("a")
                )
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)

                # Clean concatenated title+location/type garbage
                # Pattern 1: camelCase boundary (ProductManagerLondon)
                title = re.sub(r'([a-z])([A-Z][a-z])', r'\1 | \2', title)
                # Pattern 2: uppercase run into capitalised word (IIIFull, VPHead)
                title = re.sub(r'([A-Z]{2,})([A-Z][a-z])', r'\1 | \2', title)
                # Take only the part before first separator
                if " | " in title:
                    title = title.split(" | ")[0].strip()
                # Remove trailing job type suffixes that leaked in
                for suffix in [" Full Time", " Part Time", " Contract",
                               " Permanent", " Temporary", "Full Time",
                               "Part Time", " View Job", "View Job",
                               " Apply Now", "Apply Now", " Learn More",
                               "Learn More"]:
                    if title.endswith(suffix):
                        title = title[:-len(suffix)].strip()

                if len(title) < 3 or len(title) > 150:
                    continue
                if title.lower() in nav_phrases:
                    continue
                if not is_valid_job_title(title):
                    continue
                loc_el = card.find(class_=re.compile(r"location|city|region", re.I))
                if loc_el:
                    location = loc_el.get_text(strip=True)
                else:
                    _, location = clean_title_extract_location(title_el.get_text(strip=True))
                link = card.find("a", href=True)
                url = career_url
                if link:
                    href = link["href"]
                    if href.startswith("http"):
                        url = href
                    elif href.startswith("/"):
                        base = urlparse(career_url)
                        url = f"{base.scheme}://{base.netloc}{href}"
                jobs.append({
                    "title": title,
                    "location": location,
                    "salary_raw": "",
                    "url": url,
                    "description": card.get_text(separator=" ", strip=True)[:MAX_DESCRIPTION],
                    "extraction_method": "css-selector",
                })
            if jobs:
                return jobs

    # Strategy 3: Link-based detection — fallback only, strict filtering
    job_url_patterns = re.compile(
        r"/(job|jobs|career|careers|position|opening|vacancy|role|apply)/", re.I
    )
    MIN_TITLE_WORDS = 2

    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        if not job_url_patterns.search(href):
            continue
        if href in seen_hrefs:
            continue
        if len(text) < 5 or len(text) > 120:
            continue
        if text.lower() in nav_phrases:
            continue
        if len(text.split()) < MIN_TITLE_WORDS:
            continue
        # Skip if text looks like a nav item (all caps, or ends with arrow/chevron)
        if text.isupper() or text.endswith(("→", "»", ">", "›")):
            continue

        if not is_valid_job_title(text):
            continue

        title, location = clean_title_extract_location(text)
        if len(title) < 5:
            continue

        seen_hrefs.add(href)
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            base = urlparse(career_url)
            url = f"{base.scheme}://{base.netloc}{href}"
        else:
            url = career_url
        jobs.append({
            "title": title,
            "location": location,
            "salary_raw": "",
            "url": url,
            "description": "",
            "extraction_method": "link-detection",
        })

    return jobs


def save_jobs(conn, jobs: list, company: dict) -> tuple:
    new_count = 0
    cursor = conn.cursor()
    for job in jobs:
        fingerprint = make_fingerprint(company["name"], job["title"], job.get("location", ""))
        salary_min, salary_max = parse_salary(job.get("salary_raw", ""))
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
                    url, description, source, sector, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'watchlist', ?, 'dream')
                """,
                (
                    fingerprint, job["title"], company["name"], company["id"],
                    job.get("location", ""), job.get("salary_raw", ""),
                    salary_min, salary_max,
                    job["url"], job.get("description", ""), company["sector"],
                ),
            )
            new_count += 1
    conn.commit()
    return new_count, len(jobs)


def log_watch_result(conn, company_id, jobs_found, jobs_new, status, error=None, duration_ms=0):
    conn.execute(
        """
        INSERT INTO watch_log (company_id, jobs_found, jobs_new, status, error_message, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (company_id, jobs_found, jobs_new, status, error, duration_ms),
    )
    conn.commit()


def scrape_all(limit: Optional[int] = None, priority_filter: Optional[str] = None) -> dict:
    conn = get_conn()
    cursor = conn.cursor()

    query = "SELECT * FROM companies WHERE active = 1"
    params = []
    if priority_filter:
        query += " AND priority = ?"
        params.append(priority_filter)
    query += " ORDER BY priority, name"

    companies = cursor.execute(query, params).fetchall()
    if limit:
        companies = companies[:limit]

    log.info(f"Starting career page scrape — {len(companies)} companies")

    total_new   = 0
    total_found = 0
    errors      = 0
    blocked     = 0

    for i, company in enumerate(companies, 1):
        start_ms = int(time.time() * 1000)
        company  = dict(company)
        log.info(f"[{i}/{len(companies)}] {company['name']} — {company['career_page_url']}")

        result = fetch_page(company["career_page_url"])

        if result is None:
            errors += 1
            log_watch_result(conn, company["id"], 0, 0, "failed",
                             "Could not fetch page", int(time.time() * 1000) - start_ms)
            time.sleep(DELAY_BETWEEN)
            continue

        # ATS API returns structured jobs directly (Workable, Greenhouse, Lever)
        if isinstance(result, tuple) and result[0] in ("WORKABLE_API", "GREENHOUSE_API", "LEVER_API"):
            api_name = result[0].replace("_API", "").capitalize()
            jobs = result[1]
            new_count, found_count = save_jobs(conn, jobs, company)
            total_new   += new_count
            total_found += found_count
            status = "ok" if found_count > 0 else "empty"
            log_watch_result(conn, company["id"], found_count, new_count, status,
                             duration_ms=int(time.time() * 1000) - start_ms)
            if new_count > 0:
                log.info(f"  ✓ {api_name} API: {found_count} jobs, {new_count} NEW")
            else:
                log.info(f"  · {api_name} API: 0 jobs (none posted)")
            time.sleep(DELAY_BETWEEN)
            continue

        soup = result

        page_text = soup.get_text().lower()
        if any(phrase in page_text for phrase in [
            "access denied", "cloudflare", "captcha", "are you human",
            "verify you are", "enable javascript"
        ]):
            blocked += 1
            log.warning(f"  Bot protection detected: {company['name']}")
            log_watch_result(conn, company["id"], 0, 0, "blocked",
                             "Bot protection page", int(time.time() * 1000) - start_ms)
            time.sleep(DELAY_BETWEEN)
            continue

        jobs = extract_jobs_from_page(soup, company["name"], company["career_page_url"])
        new_count, found_count = save_jobs(conn, jobs, company)

        total_new   += new_count
        total_found += found_count

        status = "ok" if found_count > 0 else "empty"
        log_watch_result(conn, company["id"], found_count, new_count, status,
                         duration_ms=int(time.time() * 1000) - start_ms)

        if new_count > 0:
            log.info(f"  ✓ {found_count} jobs found, {new_count} NEW")
        else:
            log.info(f"  · {found_count} jobs found, none new")

        time.sleep(DELAY_BETWEEN)

    conn.close()

    summary = {
        "companies_checked": len(companies),
        "jobs_found":        total_found,
        "jobs_new":          total_new,
        "errors":            errors,
        "blocked":           blocked,
        "timestamp":         datetime.now().isoformat(),
    }

    log.info(
        f"Scrape complete — {len(companies)} companies, "
        f"{total_new} new jobs, {errors} errors, {blocked} blocked"
    )
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scrape company career pages")
    parser.add_argument("--limit", type=int, help="Cap number of companies (for testing)")
    parser.add_argument("--priority", choices=["HIGH", "MEDIUM", "LOW"], help="Filter by priority")
    args = parser.parse_args()
    result = scrape_all(limit=args.limit, priority_filter=args.priority)
    print(f"\nSummary: {result}")
