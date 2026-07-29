"""
skills/profile/init_db.py

Run once to:
1. Create the SQLite database with full schema
2. Import the company watchlist from CSV

Usage:
    python skills/profile/init_db.py

Run from the job_agent/ root directory.
"""

import sqlite3
import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
DB_PATH = ROOT / "data" / "agent.db"
WATCHLIST_PATH = ROOT / "data" / "companies.csv"
PROFILE_PATH = Path(__file__).parent / "profile.json"

sys.path.insert(0, str(Path(__file__).parent))
from schema import SCHEMA


def init_db():
    print(f"Initialising database at: {DB_PATH}")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    print("  Schema created.")
    return conn


def import_watchlist(conn):
    if not WATCHLIST_PATH.exists():
        print(f"  WARNING: Watchlist CSV not found at {WATCHLIST_PATH}")
        print("  Copy Shannon_Company_Watchlist CSV to data/companies.csv and re-run.")
        return 0

    cursor = conn.cursor()
    imported = 0
    skipped = 0

    priority_map = {
        "HIGH":   "HIGH",
        "MEDIUM": "MEDIUM",
        "LOW":    "LOW",
    }

    # Derive priority from sector using profile config
    with open(PROFILE_PATH) as f:
        profile = json.load(f)
    sector_priorities = {
        s: d["priority"] for s, d in profile["sectors"].items()
    }

    with open(WATCHLIST_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Company Name", "").strip()
            url  = row.get("Career Page URL", "").strip()
            sector = row.get("Sector", "").strip()
            category = row.get("Category", "").strip() or "climate"

            if not name or not url:
                skipped += 1
                continue

            priority = sector_priorities.get(sector, "LOW")

            try:
                cursor.execute(
                    """
                    INSERT INTO companies (name, career_page_url, sector, category, priority)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        career_page_url = excluded.career_page_url,
                        sector          = excluded.sector,
                        category        = excluded.category,
                        priority        = excluded.priority
                    """,
                    (name, url, sector, category, priority),
                )
                imported += 1
            except sqlite3.Error as e:
                print(f"  Error importing {name}: {e}")
                skipped += 1

    conn.commit()
    print(f"  Watchlist imported: {imported} companies, {skipped} skipped.")
    return imported


def verify(conn):
    cursor = conn.cursor()
    tables = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(f"\nTables created: {[t[0] for t in tables]}")

    count = cursor.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"Companies in watchlist: {count}")

    print("\nSector breakdown:")
    rows = cursor.execute(
        "SELECT sector, priority, COUNT(*) as n FROM companies GROUP BY sector ORDER BY priority, sector"
    ).fetchall()
    for sector, priority, n in rows:
        print(f"  [{priority:6}] {sector}: {n}")


def main():
    if DB_PATH.exists():
        ans = input(f"\nDatabase already exists at {DB_PATH}.\nReinitialise? This will NOT delete existing data. [y/N]: ")
        if ans.strip().lower() != "y":
            print("Aborted.")
            return

    conn = init_db()
    import_watchlist(conn)
    verify(conn)
    conn.close()
    print(f"\nDone. Database ready at: {DB_PATH}")


if __name__ == "__main__":
    main()
