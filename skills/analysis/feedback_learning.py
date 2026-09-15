"""
skills/analysis/feedback_learning.py

Reads accumulated feedback (with reasons) from the Google Sheet, sends patterns
to Claude, and automatically updates search query terms and the company watchlist.

Runs weekly. Auto-added companies/terms are logged for audit, not gated on
manual approval, per the automation-first design agreed for this project.
"""

import sys, os, csv, json, logging, requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("analysis.feedback_learning")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
FEEDBACK_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1nMskXZE8O6QMwo_dE2laG6B8EamlYo-14-aJSzGWzH4/gviz/tq?tqx=out:csv&sheet=Feedback"
)
QUERY_TERMS_PATH = ROOT / "skills" / "search" / "query_terms.json"
COMPANIES_CSV_PATH = ROOT / "data" / "companies.csv"


def fetch_feedback() -> list:
    """Pull the Feedback tab as CSV, referenced by name (not gid)."""
    resp = requests.get(FEEDBACK_SHEET_URL, timeout=15)
    if resp.status_code != 200:
        log.warning(f"Could not fetch feedback sheet: {resp.status_code}")
        return []
    lines = resp.text.splitlines()
    reader = csv.DictReader(lines)
    return list(reader)


def analyse_with_claude(feedback_rows: list) -> dict:
    relevant = [r for r in feedback_rows if r.get("feedback") == "yes"]
    dismissed = [r for r in feedback_rows if r.get("feedback") == "no"]

    if len(relevant) + len(dismissed) < 5:
        log.info("Not enough feedback yet to analyse (need 5+)")
        return {"new_search_terms": {"climate": [], "tech": []}, "new_companies": [], "summary": "insufficient data"}

    def fmt(r):
        reason = r.get("reason", "")
        return f"- {r.get('title','')} @ {r.get('company','')} ({r.get('category','')}){f' [reason: {reason}]' if reason else ''}"

    prompt = f"""Job seeker feedback analysis. They're targeting senior BD/commercial/GTM roles
in two verticals: climate/sustainability, and AI/tech (specifically AI orchestration,
automation, agent roles, and AI-adjacent BD — not engineering roles).

Each entry may include a reason: Role (title/duties wrong), Salary (comp not meeting
target), Company (wrong type of company), or Location (not UK/remote).

RELEVANT:
{chr(10).join(fmt(r) for r in relevant) or 'none'}

NOT RELEVANT:
{chr(10).join(fmt(r) for r in dismissed) or 'none'}

Pay close attention to the reasons. If dismissals are mostly "Location", don't suggest
new search terms — note in the summary that the location filter needs tightening instead.
If mostly "Company", search terms are fine but the wrong type of company is showing up —
suggest what to exclude, and don't suggest new_companies. If mostly "Role", the search
terms need refining — this is where new_search_terms matters most. If "Salary", note it
but don't invent search terms to fix a comp mismatch.

Identify: (1) new search query phrases likely to surface more roles like the relevant
ones, split by category climate/tech — only when Role-reason dismissals or Role-driven
relevance patterns justify it, (2) specific company names worth adding to the watchlist,
based on relevant roles' companies not already obviously covered, (3) a one-sentence
summary naming the dominant reason pattern if there is one.

Respond with only JSON, no markdown fences, no preamble:
{{"new_search_terms": {{"climate": ["term1"], "tech": ["term1"]}}, "new_companies": ["Company Name"], "summary": "..."}}"""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        log.error(f"Anthropic API error: {resp.status_code} {resp.text[:200]}")
        return {"new_search_terms": {"climate": [], "tech": []}, "new_companies": [], "summary": "api error"}

    text = resp.json()["content"][0]["text"]
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.error(f"Could not parse Claude response: {clean[:300]}")
        return {"new_search_terms": {"climate": [], "tech": []}, "new_companies": [], "summary": "parse error"}


def update_query_terms(new_terms: dict) -> int:
    with open(QUERY_TERMS_PATH) as f:
        terms = json.load(f)

    added = 0
    for category in ["climate", "tech"]:
        key = f"{category}_queries"
        existing = set(t.lower() for t in terms.get(key, []))
        for term in new_terms.get(category, []):
            if term.lower() not in existing:
                terms.setdefault(key, []).append(term)
                existing.add(term.lower())
                added += 1

    with open(QUERY_TERMS_PATH, "w") as f:
        json.dump(terms, f, indent=2)
    return added


def update_watchlist(new_companies: list) -> int:
    if not new_companies:
        return 0

    with open(COMPANIES_CSV_PATH) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        existing = set(row["Company Name"].lower().strip() for row in reader)

    added = 0
    with open(COMPANIES_CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        for name in new_companies:
            if not name or name.lower().strip() in existing:
                continue
            guess_url = f"https://{name.lower().replace(' ', '')}.com/careers"
            row = {fn: "" for fn in fieldnames}
            row["Company Name"] = name
            row["Career Page URL"] = guess_url
            row["Sector"] = "Other Impact"
            if "Category" in fieldnames:
                row["Category"] = "tech"
            writer.writerow(row)
            added += 1
    return added


def run() -> dict:
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return {"error": "no_api_key"}

    feedback = fetch_feedback()
    log.info(f"Fetched {len(feedback)} feedback rows")

    result = analyse_with_claude(feedback)
    terms_added = update_query_terms(result.get("new_search_terms", {}))
    companies_added = update_watchlist(result.get("new_companies", []))

    log.info(f"Summary: {result.get('summary', '')}")
    log.info(
        f"Added {terms_added} search terms, {companies_added} companies "
        f"(career URLs are guesses — weekly url_checker will validate/fix)"
    )

    return {
        "feedback_rows": len(feedback),
        "terms_added": terms_added,
        "companies_added": companies_added,
        "summary": result.get("summary", ""),
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
