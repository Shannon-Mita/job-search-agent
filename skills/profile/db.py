"""
skills/profile/db.py

Shared database connection utility.
Every skill imports get_conn() from here — never open sqlite3 directly elsewhere.
"""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
DB_PATH = ROOT / "data" / "agent.db"


def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}.\n"
            "Run: python skills/profile/init_db.py"
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # rows accessible as dicts
    conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_profile() -> dict:
    import json
    profile_path = Path(__file__).parent / "profile.json"
    with open(profile_path) as f:
        return json.load(f)
