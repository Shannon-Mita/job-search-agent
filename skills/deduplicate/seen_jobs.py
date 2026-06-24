"""
skills/deduplicate/seen_jobs.py

Persistent memory for the job agent.
Stores fingerprints of jobs already sent in the digest.
File is committed back to GitHub after each run so memory
persists across daily GitHub Actions runs.

Run standalone to inspect:
    python skills/deduplicate/seen_jobs.py --stats
    python skills/deduplicate/seen_jobs.py --clear
"""

import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "skills" / "profile"))

from db import get_conn

log = logging.getLogger("deduplicate.seen_jobs")

SEEN_FILE = ROOT / "data" / "seen_jobs.json"
RETENTION_DAYS = 60  # forget jobs after 60 days so they can resurface if still relevant


def load_seen() -> dict:
    """Load seen jobs from file. Returns {fingerprint: date_seen}."""
    if not SEEN_FILE.exists():
        return {}
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen: dict):
    """Save seen jobs to file."""
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def prune_old(seen: dict) -> dict:
    """Remove entries older than RETENTION_DAYS."""
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    return {fp: date for fp, date in seen.items() if date > cutoff}


def mark_seen(fingerprints: list):
    """Mark a list of fingerprints as seen today."""
    seen = load_seen()
    today = datetime.now().isoformat()
    for fp in fingerprints:
        seen[fp] = today
    seen = prune_old(seen)
    save_seen(seen)
    log.info(f"Marked {len(fingerprints)} jobs as seen. Total in memory: {len(seen)}")


def filter_unseen(conn, jobs: list) -> list:
    """
    Filter a list of job dicts to only unseen ones.
    Jobs are identified by their DB fingerprint.
    """
    seen = load_seen()
    unseen = []
    for job in jobs:
        fp = job.get("fingerprint")
        if fp and fp not in seen:
            unseen.append(job)
    return unseen


def get_unseen_notifiable_jobs(conn) -> list:
    """Get notifiable dream jobs that haven't been seen before."""
    seen = load_seen()
    rows = conn.execute("""
        SELECT
            j.id, j.fingerprint, j.title, j.company_name, j.location,
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

    unseen = []
    for row in rows:
        row = dict(row)
        if row["fingerprint"] not in seen:
            unseen.append(row)
    return unseen


def get_unseen_bridge_jobs(conn) -> list:
    """Get bridge jobs that haven't been seen before."""
    seen = load_seen()
    rows = conn.execute("""
        SELECT
            j.id, j.fingerprint, j.title, j.company_name, j.location,
            j.url, j.score, j.sector, j.source,
            j.salary_raw, j.first_seen
        FROM jobs j
        WHERE j.mode = 'bridge'
          AND j.dismissed = 0
        ORDER BY j.first_seen DESC
        LIMIT 50
    """).fetchall()

    unseen = []
    for row in rows:
        row = dict(row)
        if row["fingerprint"] not in seen:
            unseen.append(row)
    return unseen[:20]  # cap at 20


def mark_digest_sent(dream_jobs: list, bridge_jobs: list):
    """Mark all jobs in a sent digest as seen."""
    all_fps = (
        [j["fingerprint"] for j in dream_jobs if j.get("fingerprint")] +
        [j["fingerprint"] for j in bridge_jobs if j.get("fingerprint")]
    )
    mark_seen(all_fps)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--stats", action="store_true", help="Show memory stats")
    parser.add_argument("--clear", action="store_true", help="Clear seen jobs memory")
    args = parser.parse_args()

    if args.clear:
        save_seen({})
        print("Cleared seen jobs memory")
    elif args.stats:
        seen = load_seen()
        seen = prune_old(seen)
        print(f"Jobs in memory: {len(seen)}")
        print(f"Retention: {RETENTION_DAYS} days")
        if seen:
            dates = sorted(seen.values())
            print(f"Oldest: {dates[0][:10]}")
            print(f"Newest: {dates[-1][:10]}")
    else:
        seen = load_seen()
        print(f"Seen jobs: {len(seen)}")
