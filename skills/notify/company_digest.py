"""
skills/notify/company_digest.py

Sends a weekly email of newly discovered companies for watchlist review.
Runs Sundays alongside company_discovery.py.

Run standalone:
    python skills/notify/company_digest.py
    python skills/notify/company_digest.py --dry-run
"""

import sys
import os
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "skills" / "profile"))

from db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("notify.company_digest")

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL       = os.getenv("NOTIFY_EMAIL")


def get_pending_companies(conn) -> list:
    rows = conn.execute("""
        SELECT company_name, source, url, summary, spotted_at
        FROM signals
        WHERE signal_type = 'hiring_post'
          AND actioned = 0
        ORDER BY spotted_at DESC
        LIMIT 100
    """).fetchall()
    return [dict(r) for r in rows]


def build_company_email(companies: list) -> str:
    date_str  = datetime.now().strftime("%A %d %B %Y")
    count     = len(companies)

    # Group by sector (extracted from summary)
    by_sector = {}
    for c in companies:
        summary = c.get("summary", "")
        sector_match = __import__("re").search(r"Sector: ([^\.]+)", summary)
        sector = sector_match.group(1).strip() if sector_match else "Other"
        by_sector.setdefault(sector, []).append(c)

    def company_row(c: dict) -> str:
        name    = c["company_name"]
        url     = c["url"] or "#"
        summary = c.get("summary", "")
        # Remove sector prefix from summary for display
        summary = __import__("re").sub(r"^Sector: [^\.]+\.\s*", "", summary)
        source  = c.get("source", "").replace("_", " ").replace("serper ", "")

        funding_match = __import__("re").search(r"Funding: (.+)$", summary)
        funding_line  = ""
        if funding_match:
            funding_line = f'<div style="font-size:11px;color:#0F6E56;margin-top:2px;">💰 {funding_match.group(1)}</div>'
            summary = summary[:summary.rfind("Funding:")].strip()

        return f"""
        <tr>
          <td style="padding:10px 16px;border-bottom:1px solid #F3F4F6;vertical-align:top;">
            <div style="font-size:13px;font-weight:500;color:#111827;">{name}</div>
            <div style="font-size:11px;color:#6B7280;margin-top:2px;">{summary[:120] if summary else ''}</div>
            {funding_line}
            <div style="font-size:11px;color:#9CA3AF;margin-top:2px;">via {source}</div>
          </td>
          <td style="padding:10px 16px;border-bottom:1px solid #F3F4F6;vertical-align:top;text-align:right;white-space:nowrap;">
            <a href="{url}" style="font-size:12px;color:#185FA5;text-decoration:none;">Visit →</a>
          </td>
        </tr>"""

    def sector_section(sector: str, companies: list) -> str:
        rows = "".join(company_row(c) for c in companies)
        return f"""
        <div style="margin-bottom:20px;">
          <div style="font-size:11px;font-weight:500;letter-spacing:0.08em;
                      text-transform:uppercase;color:#534AB7;
                      margin-bottom:8px;padding-bottom:6px;
                      border-bottom:2px solid #534AB7;">
            {sector} ({len(companies)})
          </div>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #E5E7EB;border-radius:8px;
                        border-collapse:collapse;overflow:hidden;">
            {rows}
          </table>
        </div>"""

    sections = "".join(
        sector_section(sector, cos)
        for sector, cos in sorted(by_sector.items())
    )

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#F9FAFB;margin:0;padding:0;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;">

    <div style="margin-bottom:24px;">
      <div style="font-size:20px;font-weight:500;color:#111827;">
        New Companies to Review
      </div>
      <div style="font-size:13px;color:#6B7280;margin-top:4px;">
        {date_str} · {count} climate companies not yet on your watchlist
      </div>
    </div>

    {sections}

    <div style="font-size:12px;color:#9CA3AF;margin-top:24px;
                padding-top:16px;border-top:1px solid #E5E7EB;">
      To add a company: open data/companies.csv, add a row, commit and push.<br>
      To dismiss all: run <code>python skills/signals/company_discovery.py --clear</code>
    </div>

  </div>
</body>
</html>"""


def send_company_digest(dry_run: bool = False) -> dict:
    conn      = get_conn()
    companies = get_pending_companies(conn)
    conn.close()

    if not companies:
        log.info("No pending companies — skipping")
        return {"sent": False, "reason": "no_candidates", "count": 0}

    html = build_company_email(companies)

    if dry_run:
        print(f"\nDRY RUN — {len(companies)} companies to review")
        by_sector = {}
        for c in companies:
            summary = c.get("summary", "")
            import re
            m = re.search(r"Sector: ([^\.]+)", summary)
            sector = m.group(1).strip() if m else "Other"
            by_sector.setdefault(sector, []).append(c["company_name"])
        for sector, names in sorted(by_sector.items()):
            print(f"\n  [{sector}]")
            for name in names[:5]:
                print(f"    {name}")
            if len(names) > 5:
                print(f"    ... and {len(names)-5} more")
        return {"sent": False, "reason": "dry_run", "count": len(companies)}

    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL]):
        log.error("Gmail credentials not set")
        return {"sent": False, "reason": "no_credentials", "count": 0}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Job Agent — {len(companies)} new companies to review · {datetime.now().strftime('%d %b')}"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())
        log.info(f"Company digest sent — {len(companies)} companies")
        return {"sent": True, "count": len(companies)}
    except Exception as e:
        log.error(f"Failed: {e}")
        return {"sent": False, "reason": str(e), "count": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = send_company_digest(dry_run=args.dry_run)
    print(f"\nResult: {result}")
