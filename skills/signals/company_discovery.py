"""
skills/signals/company_discovery.py

Discovers new climate/sustainability companies not already on the watchlist.
Runs weekly. Flags candidates in the signals table for manual review.

Sources:
  - Serper/Google searches by sector and funding stage
  - Crunchbase public company pages (no API key needed)
  - Climate-specific directories

Run standalone:
    python skills/signals/company_discovery.py
    python skills/signals/company_discovery.py --dry-run
    python skills/signals/company_discovery.py --report
"""

import sys
import time
import logging
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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
log = logging.getLogger("signals.company_discovery")

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
SERPER_URL     = "https://google.serper.dev/search"
DELAY_BETWEEN  = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Sector search queries ─────────────────────────────────────────────────────

SECTOR_QUERIES = [
    # Alt Materials & Packaging
    ("Alt Materials & Packaging", 'inurl:careers "alternative materials" OR "biomaterials" OR "bioplastics" UK Europe'),
    ("Alt Materials & Packaging", 'inurl:careers "sustainable packaging" OR "compostable packaging" OR "biodegradable packaging"'),
    ("Alt Materials & Packaging", 'inurl:careers "mycelium" OR "seaweed materials" OR "bio-based materials" UK'),
    ("Alt Materials & Packaging", 'inurl:careers "textile recycling" OR "fibre recycling" OR "material recovery" UK'),

    # Ocean & Blue Economy
    ("Ocean & Blue Economy", 'inurl:careers "ocean technology" OR "marine technology" OR "blue economy" UK'),
    ("Ocean & Blue Economy", 'inurl:careers "tidal energy" OR "wave energy" OR "offshore renewable" UK'),
    ("Ocean & Blue Economy", 'inurl:careers "seaweed" OR "kelp farming" OR "ocean carbon" UK'),
    ("Ocean & Blue Economy", 'inurl:careers "ocean conservation" OR "marine conservation" OR "ocean plastics" UK'),

    # Food Systems & Waste
    ("Food Systems & Waste", 'inurl:careers "food waste" OR "surplus food" OR "food recovery" UK'),
    ("Food Systems & Waste", 'inurl:careers "alternative protein" OR "cultivated meat" OR "precision fermentation" UK'),
    ("Food Systems & Waste", 'inurl:careers "vertical farming" OR "indoor farming" OR "controlled environment agriculture" UK'),
    ("Food Systems & Waste", 'inurl:careers "plant-based food" OR "plant based protein" OR "meat alternative" UK'),
    ("Food Systems & Waste", 'inurl:careers "regenerative agriculture" OR "soil health" OR "agroforestry" UK Australia'),
    ("Food Systems & Waste", 'inurl:careers "insect protein" OR "black soldier fly" OR "insect farming" UK Europe'),

    # Circular Economy
    ("Circular Economy", 'inurl:careers "circular economy" OR "closed loop" OR "zero waste" UK'),
    ("Circular Economy", 'inurl:careers "reusable packaging" OR "reuse platform" OR "take-back" UK'),
    ("Circular Economy", 'inurl:careers "textile recycling" OR "circular fashion" OR "clothing resale" UK'),
    ("Circular Economy", 'inurl:careers "product lifecycle" OR "reverse logistics" OR "recommerce" UK'),

    # Carbon Removal & Capture
    ("Carbon Removal & Capture", 'inurl:careers "carbon removal" OR "direct air capture" OR "carbon sequestration" UK'),
    ("Carbon Removal & Capture", 'inurl:careers "nature based solutions" OR "reforestation" OR "peatland restoration" UK'),
    ("Carbon Removal & Capture", 'inurl:careers "enhanced weathering" OR "biochar" OR "soil carbon" UK Australia'),
    ("Carbon Removal & Capture", 'inurl:careers "carbon credits" OR "voluntary carbon market" OR "carbon registry" UK'),

    # Renewable Energy
    ("Renewable Energy", 'inurl:careers "green hydrogen" OR "electrolysis" OR "hydrogen fuel" UK Australia'),
    ("Renewable Energy", 'inurl:careers "energy storage" OR "battery storage" OR "grid storage" UK Australia'),
    ("Renewable Energy", 'inurl:careers "solar energy" OR "wind energy" OR "offshore wind" UK Australia'),
    ("Renewable Energy", 'inurl:careers "demand flexibility" OR "virtual power plant" OR "smart grid" UK'),

    # Australia specific
    ("Food Systems & Waste", 'inurl:careers "agtech" OR "precision agriculture" OR "sustainable farming" Australia'),
    ("Renewable Energy", 'inurl:careers "renewable energy" OR "clean energy" OR "net zero" Australia'),
    ("Circular Economy", 'inurl:careers "circular economy" OR "sustainable packaging" OR "waste technology" Australia'),
]

# Crunchbase searches by category
CRUNCHBASE_SEARCHES = [
    "https://www.crunchbase.com/discover/organization.companies/field/organizations/categories/sustainable-packaging",
    "https://www.crunchbase.com/discover/organization.companies/field/organizations/categories/biomass-energy",
    "https://www.crunchbase.com/discover/organization.companies/field/organizations/categories/clean-energy",
    "https://www.crunchbase.com/discover/organization.companies/field/organizations/categories/food-and-beverage",
    "https://www.crunchbase.com/discover/organization.companies/field/organizations/categories/circular-economy",
]


# ── Utilities ─────────────────────────────────────────────────────────────────

def serper_search(query: str, num: int = 10) -> list:
    if not SERPER_API_KEY:
        return []
    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num, "gl": "uk", "hl": "en"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("organic", [])
    except Exception as e:
        log.warning(f"Serper error: {e}")
    return []


def extract_domain(url: str) -> str:
    """Extract clean domain from URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        domain = re.sub(r"^www\.", "", domain)
        return domain
    except Exception:
        return ""


def is_company_website(url: str, name: str) -> bool:
    """Check if a URL looks like a company website rather than news/directory."""
    skip_domains = [
        "techcrunch.com", "crunchbase.com", "linkedin.com", "twitter.com",
        "instagram.com", "facebook.com", "youtube.com", "wikipedia.org",
        "gov.uk", "bbc.co.uk", "theguardian.com", "reuters.com",
        "bloomberg.com", "forbes.com", "businessgreen.com", "edie.net",
        "sustainablebrands.com", "greenbiz.com", "cleantech.com",
        "angel.co", "seedrs.com", "kickstarter.com", "indiegogo.com",
        "glassdoor.com", "indeed.com", "reed.co.uk",
    ]
    domain = extract_domain(url)
    return not any(skip in domain for skip in skip_domains)


def find_career_page(website_url: str) -> str:
    """Try to find the careers page URL for a company website."""
    common_paths = [
        "/careers", "/jobs", "/join", "/join-us", "/work-with-us",
        "/about/careers", "/company/careers", "/pages/careers",
    ]
    base = website_url.rstrip("/")
    for path in common_paths:
        url = f"{base}{path}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
            if resp.status_code == 200:
                return url
        except Exception:
            pass
        time.sleep(0.3)

    # Default to /careers if nothing found
    return f"{base}/careers"


def load_existing_watchlist(conn) -> tuple[set, set]:
    """Load existing company names and domains for deduplication."""
    rows = conn.execute("SELECT name, career_page_url FROM companies").fetchall()
    names   = {r["name"].lower().strip() for r in rows}
    domains = set()
    for r in rows:
        domain = extract_domain(r["career_page_url"] or "")
        if domain:
            domains.add(domain)
    return names, domains


def extract_company_from_result(result: dict, sector: str) -> Optional[dict]:
    """
    Extract a company candidate from a Serper search result.
    Returns company dict or None.
    """
    title   = result.get("title", "")
    url     = result.get("link", "")
    snippet = result.get("snippet", "")

    if not url or not title:
        return None

    # Skip non-company pages
    if not is_company_website(url, title):
        return None

    # Skip job boards, directories
    skip_titles = [
        "jobs at", "careers at", "working at", "review",
        "funding", "raises", "announces", "acquires",
        "top 10", "best ", "list of", "guide to",
    ]
    if any(p in title.lower() for p in skip_titles):
        return None

    # Skip if URL is clearly a directory, news, or investor site
    skip_url_patterns = [
        "techcrunch", "crunchbase", "dealroom", "sifted", "beauhurst",
        "startups.co.uk", "uktech.news", "businessgreen", "edie.net",
        "greenqueen", "greenbiz", "climatetechvc", "ctvc.co",
        "cleantech", "planettracker", "carbontrust", "/blog/", "/news/",
        "/article/", "/report/", "/insight/", "/resource/",
        "investor", "venture", "capital", "fund", "vc.",
        "accelerator", "incubator", "index.", "list-of",
        "top-", "best-", "directory",
        "welpmagazine", "startus-insights", "f6s.com", "wellfound.com",
        "eu-startups", "sifted.eu", "tech.eu", "maddyness",
        "listings", "job-board", "jobboard", "work-at", "hiring-now",
    ]
    url_lower = url.lower()
    if any(p in url_lower for p in skip_url_patterns):
        return None

    # Skip if title looks like a list or article
    list_patterns = [
        "funded", "startups 20", "companies to watch", "investment resources",
        "strategies", "1156+", "top ", "best ", "list of", "guide to",
        "how to", "what is", "why ", "report:", "roundup",
    ]
    if any(p in title.lower() for p in list_patterns):
        return None

    # Clean company name from title
    company_name = title
    for sep in [" | ", " - ", " — ", " · ", ": "]:
        if sep in company_name:
            company_name = company_name.split(sep)[0].strip()

    # Remove common suffixes
    for suffix in [" Ltd", " Limited", " Inc", " Corp", " GmbH", " SAS",
                   " Technologies", " Technology", " Solutions"]:
        if company_name.endswith(suffix):
            company_name = company_name[:-len(suffix)].strip()

    if len(company_name) < 2 or len(company_name) > 60:
        return None

    # Extract funding info from snippet if available
    funding = ""
    funding_match = re.search(
        r'(?:raised?|secured?|closed?)\s+(?:a\s+)?([£$€][\d.]+[MBK]|\$[\d.]+[MBK])',
        snippet, re.I
    )
    if funding_match:
        funding = funding_match.group(0)

    return {
        "name":      company_name,
        "website":   url,
        "sector":    sector,
        "snippet":   snippet[:500],
        "funding":   funding,
        "source":    "serper_discovery",
    }


# ── Main discovery functions ──────────────────────────────────────────────────

def search_serper_companies() -> list:
    """Search Google via Serper for climate companies by sector."""
    candidates = []
    log.info(f"Running {len(SECTOR_QUERIES)} sector searches")

    for sector, query in SECTOR_QUERIES:
        log.info(f"  [{sector}] {query[:60]}...")
        results = serper_search(query, num=10)

        for result in results:
            company = extract_company_from_result(result, sector)
            if company:
                candidates.append(company)

        time.sleep(DELAY_BETWEEN)

    log.info(f"Serper discovery: {len(candidates)} candidates found")
    return candidates


def search_crunchbase_companies() -> list:
    """
    Fetch company names from Crunchbase public pages.
    No API key needed — scrapes public discovery pages.
    """
    candidates = []
    log.info("Searching Crunchbase public pages")

    sector_map = {
        "sustainable-packaging": "Alt Materials & Packaging",
        "biomass-energy":        "Renewable Energy",
        "clean-energy":          "Renewable Energy",
        "food-and-beverage":     "Food Systems & Waste",
        "circular-economy":      "Circular Economy",
    }

    for url in CRUNCHBASE_SEARCHES:
        category = url.split("/")[-1]
        sector   = sector_map.get(category, "Other Impact")

        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                log.warning(f"Crunchbase {resp.status_code}: {url}")
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # Crunchbase company cards
            for card in soup.select("[class*='entity-link'], [class*='company'], a[href*='/organization/']"):
                name = card.get_text(strip=True)
                href = card.get("href", "")

                if not name or len(name) < 2 or len(name) > 60:
                    continue
                if not href:
                    continue

                full_url = f"https://www.crunchbase.com{href}" if href.startswith("/") else href

                candidates.append({
                    "name":    name,
                    "website": full_url,
                    "sector":  sector,
                    "snippet": f"Found on Crunchbase in {category} category",
                    "funding": "",
                    "source":  "crunchbase",
                })

        except Exception as e:
            log.warning(f"Crunchbase error for {url}: {e}")

        time.sleep(DELAY_BETWEEN)

    log.info(f"Crunchbase discovery: {len(candidates)} candidates found")
    return candidates


def deduplicate_candidates(candidates: list, existing_names: set, existing_domains: set) -> list:
    """Remove candidates already on the watchlist or duplicates within the batch."""
    seen_names   = set()
    seen_domains = set()
    unique = []

    for c in candidates:
        name   = c["name"].lower().strip()
        domain = extract_domain(c["website"])

        # Skip if already on watchlist
        if name in existing_names:
            continue
        if domain and domain in existing_domains:
            continue

        # Skip duplicates within batch
        if name in seen_names:
            continue
        if domain and domain in seen_domains:
            continue

        seen_names.add(name)
        if domain:
            seen_domains.add(domain)
        unique.append(c)

    return unique


def save_candidates(conn, candidates: list) -> int:
    """Save new company candidates to signals table."""
    cursor = conn.cursor()
    saved  = 0

    for c in candidates:
        try:
            # Check if already signalled recently
            existing = cursor.execute(
                """
                SELECT id FROM signals
                WHERE company_name = ?
                AND spotted_at > datetime('now', '-30 days')
                """,
                (c["name"],)
            ).fetchone()

            if existing:
                continue

            summary = f"Sector: {c['sector']}. {c['snippet']}"
            if c.get("funding"):
                summary += f" Funding: {c['funding']}"

            cursor.execute(
                """
                INSERT INTO signals (
                    company_name, signal_type, source, url, summary
                ) VALUES (?, 'hiring_post', ?, ?, ?)
                """,
                (c["name"], c["source"], c["website"], summary[:500])
            )
            saved += 1
        except Exception as e:
            log.warning(f"Error saving {c['name']}: {e}")

    conn.commit()
    return saved


def generate_report(conn) -> str:
    """Generate a text report of pending company candidates."""
    rows = conn.execute("""
        SELECT company_name, source, url, summary, spotted_at
        FROM signals
        WHERE signal_type = 'hiring_post'
          AND actioned = 0
        ORDER BY spotted_at DESC
        LIMIT 100
    """).fetchall()

    if not rows:
        return "No pending company candidates."

    lines = [
        f"COMPANY DISCOVERY REPORT — {datetime.now().strftime('%d %B %Y')}",
        f"{'='*60}",
        f"{len(rows)} companies pending review",
        f"",
    ]

    # Group by source
    by_source = {}
    for r in rows:
        src = r["source"]
        by_source.setdefault(src, []).append(r)

    for source, companies in by_source.items():
        lines.append(f"\n[{source.upper()}] — {len(companies)} companies")
        lines.append("-" * 40)
        for c in companies:
            lines.append(f"  {c['company_name']}")
            lines.append(f"    {c['url']}")
            summary = c['summary'] or ""
            if summary:
                lines.append(f"    {summary[:120]}")
            lines.append("")

    lines.append(f"\nTo add a company to your watchlist:")
    lines.append(f"  Edit data/companies.csv and run: python skills/profile/init_db.py")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_discovery(dry_run: bool = False) -> dict:
    """Run full company discovery pipeline."""
    conn = get_conn()

    log.info("Starting company discovery")
    existing_names, existing_domains = load_existing_watchlist(conn)
    log.info(f"Existing watchlist: {len(existing_names)} companies")

    # Search all sources
    candidates = []
    candidates.extend(search_serper_companies())
    candidates.extend(search_crunchbase_companies())

    log.info(f"Total candidates before dedup: {len(candidates)}")

    # Deduplicate
    unique = deduplicate_candidates(candidates, existing_names, existing_domains)
    log.info(f"New candidates after dedup: {len(unique)}")

    if dry_run:
        print(f"\nDRY RUN — {len(unique)} new companies found")
        by_sector = {}
        for c in unique:
            by_sector.setdefault(c["sector"], []).append(c)
        for sector, companies in sorted(by_sector.items()):
            print(f"\n  [{sector}] — {len(companies)} companies")
            for c in companies[:5]:
                print(f"    {c['name']} — {c['website']}")
            if len(companies) > 5:
                print(f"    ... and {len(companies)-5} more")
        conn.close()
        return {"dry_run": True, "found": len(unique)}

    # Save to signals
    saved = save_candidates(conn, unique)
    conn.close()

    summary = {
        "candidates_found": len(unique),
        "candidates_saved": saved,
        "timestamp":        datetime.now().isoformat(),
    }
    log.info(f"Discovery complete — {saved} new candidates saved to signals table")
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Discover new climate companies")
    parser.add_argument("--dry-run", action="store_true", help="Find without saving")
    parser.add_argument("--report",  action="store_true", help="Show pending candidates")
    args = parser.parse_args()

    if args.report:
        conn = get_conn()
        print(generate_report(conn))
        conn.close()
    else:
        result = run_discovery(dry_run=args.dry_run)
        print(f"\nSummary: {json.dumps(result, indent=2)}")
