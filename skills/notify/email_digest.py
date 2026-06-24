"""
skills/notify/email_digest.py

Sends a daily email digest of notifiable jobs.
Only sends if there are new notified roles since the last digest.

Run standalone:
    python skills/notify/email_digest.py
    python skills/notify/email_digest.py --dry-run
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

from db import get_conn, get_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("notify.email_digest")

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL       = os.getenv("NOTIFY_EMAIL")


def get_dream_jobs(conn) -> list:
    """Get notifiable dream role jobs ordered by score."""
    rows = conn.execute("""
        SELECT
            j.id, j.title, j.company_name, j.location,
            j.url, j.score, j.sector, j.source,
            j.salary_raw, j.first_seen,
            c.priority
        FROM jobs j
        LEFT JOIN companies c ON j.company_id = c.id
        WHERE j.notified = 1
          AND j.dismissed = 0
          AND j.score > 0
          AND j.mode = 'dream'
        ORDER BY j.score DESC, j.first_seen DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_bridge_jobs(conn) -> list:
    """Get bridge income opportunities ordered by recency."""
    rows = conn.execute("""
        SELECT
            j.id, j.title, j.company_name, j.location,
            j.url, j.score, j.sector, j.source,
            j.salary_raw, j.first_seen
        FROM jobs j
        WHERE j.mode = 'bridge'
          AND j.dismissed = 0
        ORDER BY j.first_seen DESC
        LIMIT 20
    """).fetchall()
    return [dict(r) for r in rows]


def build_html_email(dream_jobs: list, bridge_jobs: list, profile: dict) -> str:
    """Build a clean HTML email digest."""
    date_str = datetime.now().strftime("%A %d %B %Y")
    total    = len(dream_jobs) + len(bridge_jobs)

    # Group by score tier
    top_roles  = [j for j in dream_jobs if j["score"] >= 60]
    good_roles = [j for j in dream_jobs if 40 <= j["score"] < 60]

    def job_row(job: dict) -> str:
        title    = job["title"] or "Untitled"
        company  = job["company_name"] or "Unknown"
        location = job["location"] or "Location not specified"
        sector   = job["sector"] or ""
        score    = job["score"]
        url      = job["url"] or "#"
        priority = job.get("priority") or ""
        salary   = job.get("salary_raw") or ""

        priority_badge = ""
        if priority == "HIGH":
            priority_badge = '<span style="background:#E1F5EE;color:#0F6E56;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:500;">HIGH</span>'
        elif priority == "MEDIUM":
            priority_badge = '<span style="background:#FFF8E6;color:#854F0B;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:500;">MEDIUM</span>'

        salary_line = f'<div style="font-size:12px;color:#6B7280;margin-top:2px;">{salary}</div>' if salary else ""

        return f"""
        <tr>
          <td style="padding:12px 16px;border-bottom:1px solid #F3F4F6;vertical-align:top;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
              <a href="{url}" style="font-size:14px;font-weight:500;color:#111827;text-decoration:none;">{title}</a>
              {priority_badge}
            </div>
            <div style="font-size:13px;color:#374151;">{company}</div>
            <div style="font-size:12px;color:#6B7280;margin-top:2px;">
              {location}{' · ' + sector if sector else ''}
            </div>
            {salary_line}
          </td>
          <td style="padding:12px 16px;border-bottom:1px solid #F3F4F6;vertical-align:top;text-align:right;white-space:nowrap;">
            <span style="font-size:13px;font-weight:500;color:#374151;">{score}</span>
            <span style="font-size:11px;color:#9CA3AF;">/100</span>
            <br>
            <a href="{url}" style="font-size:12px;color:#185FA5;text-decoration:none;">View →</a>
          </td>
        </tr>"""

    def section(title: str, jobs: list, accent: str) -> str:
        if not jobs:
            return ""
        rows = "".join(job_row(j) for j in jobs)
        return f"""
        <div style="margin-bottom:24px;">
          <div style="font-size:11px;font-weight:500;letter-spacing:0.08em;
                      text-transform:uppercase;color:{accent};
                      margin-bottom:8px;padding-bottom:6px;
                      border-bottom:2px solid {accent};">
            {title} ({len(jobs)})
          </div>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #E5E7EB;border-radius:8px;
                        border-collapse:collapse;overflow:hidden;">
            {rows}
          </table>
        </div>"""

    top_section  = section("Strong matches", top_roles, "#0F6E56")
    good_section = section("Worth reviewing", good_roles, "#185FA5")

    # Bridge income section
    def bridge_row(job: dict) -> str:
        title    = job["title"] or "Untitled"
        company  = job["company_name"] or ""
        location = job["location"] or "Remote"
        url      = job["url"] or "#"
        salary   = job.get("salary_raw") or ""
        source   = job.get("source", "").replace("serper_", "").replace("_", " ")
        salary_line = f'<div style="font-size:12px;color:#6B7280;margin-top:2px;">{salary}</div>' if salary else ""
        return f"""
        <tr>
          <td style="padding:12px 16px;border-bottom:1px solid #F3F4F6;vertical-align:top;">
            <a href="{url}" style="font-size:14px;font-weight:500;color:#111827;text-decoration:none;">{title}</a>
            <div style="font-size:12px;color:#6B7280;margin-top:2px;">{location} · via {source}</div>
            {salary_line}
          </td>
          <td style="padding:12px 16px;border-bottom:1px solid #F3F4F6;vertical-align:top;text-align:right;">
            <a href="{url}" style="font-size:12px;color:#185FA5;text-decoration:none;">View →</a>
          </td>
        </tr>"""

    bridge_section = ""
    if bridge_jobs:
        bridge_rows = "".join(bridge_row(j) for j in bridge_jobs)
        bridge_section = f"""
        <div style="margin-bottom:24px;">
          <div style="font-size:11px;font-weight:500;letter-spacing:0.08em;
                      text-transform:uppercase;color:#6B21A8;
                      margin-bottom:8px;padding-bottom:6px;
                      border-bottom:2px solid #6B21A8;">
            Bridge income — freelance / contract / fractional ({len(bridge_jobs)})
          </div>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #E5E7EB;border-radius:8px;
                        border-collapse:collapse;overflow:hidden;">
            {bridge_rows}
          </table>
        </div>"""

    all_jobs = dream_jobs + bridge_jobs

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#F9FAFB;margin:0;padding:0;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="margin-bottom:24px;">
      <div style="font-size:20px;font-weight:500;color:#111827;">
        Job Agent Digest
      </div>
      <div style="font-size:13px;color:#6B7280;margin-top:4px;">
        {date_str} · {total} role{'s' if total != 1 else ''} found
      </div>
    </div>

    {top_section}
    {good_section}
    {bridge_section}

    <!-- Footer -->
    <div style="font-size:12px;color:#9CA3AF;margin-top:24px;
                padding-top:16px;border-top:1px solid #E5E7EB;">
      Sourced from {len(set(j['source'] for j in all_jobs))} sources
      across {len(set(j['company_name'] for j in all_jobs))} companies.
      <br>Your watchlist has {get_watchlist_count()} active companies.
    </div>

  </div>
</body>
</html>"""


def get_watchlist_count() -> int:
    try:
        conn = get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE active = 1"
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def send_digest(dry_run: bool = False) -> dict:
    conn    = get_conn()
    profile = get_profile()

    # Import seen jobs memory
    seen_jobs_path = str(ROOT / "skills" / "deduplicate")
    if seen_jobs_path not in sys.path:
        sys.path.insert(0, seen_jobs_path)
    from seen_jobs import get_unseen_notifiable_jobs, get_unseen_bridge_jobs, mark_digest_sent

    dream_jobs  = get_unseen_notifiable_jobs(conn)
    bridge_jobs = get_unseen_bridge_jobs(conn)
    conn.close()

    if not dream_jobs and not bridge_jobs:
        log.info("No new jobs since last digest — skipping")
        return {"sent": False, "reason": "no_new_jobs", "count": 0}

    html  = build_html_email(dream_jobs, bridge_jobs, profile)
    total = len(dream_jobs) + len(bridge_jobs)

    if dry_run:
        print(f"\nDRY RUN — {len(dream_jobs)} new dream roles, {len(bridge_jobs)} new bridge")
        print("\nDREAM ROLES (new only):")
        for j in dream_jobs:
            print(f"  [{j['score']}] {j['title']} @ {j['company_name']} ({j['location'] or 'no location'})")
        print("\nBRIDGE INCOME (new only):")
        for j in bridge_jobs:
            print(f"  {j['title']} via {j['source']}")
        return {"sent": False, "reason": "dry_run", "count": total}

    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL]):
        log.error("Gmail credentials not set in .env")
        return {"sent": False, "reason": "no_credentials", "count": 0}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Job Agent — {len(dream_jobs)} new roles · {len(bridge_jobs)} bridge · {datetime.now().strftime('%d %b')}"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())

        # Only mark as seen after successful send
        mark_digest_sent(dream_jobs, bridge_jobs)
        log.info(f"Digest sent — {len(dream_jobs)} dream, {len(bridge_jobs)} bridge")
        return {"sent": True, "count": total}
    except Exception as e:
        log.error(f"Failed to send: {e}")
        return {"sent": False, "reason": str(e), "count": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send job digest email")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print digest without sending")
    args = parser.parse_args()
    result = send_digest(dry_run=args.dry_run)
    print(f"\nResult: {result}")
