"""
tests/test_step1_profile.py

Tests for Step 1: profile config and database initialisation.
Run from job_agent/ root: python -m pytest tests/test_step1_profile.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "skills" / "profile"))

import pytest
from db import get_conn, get_profile


# ── Profile config ──────────────────────────────────────────────────────────

def test_profile_loads():
    profile = get_profile()
    assert isinstance(profile, dict)

def test_profile_has_required_keys():
    profile = get_profile()
    for key in ["dream_role", "target_titles", "sectors", "scoring", "bridge_income"]:
        assert key in profile, f"Missing key: {key}"

def test_salary_floor_is_sensible():
    profile = get_profile()
    assert profile["dream_role"]["min_total_package"] == 100000
    assert profile["dream_role"]["min_salary_base"] == 50000

def test_all_sectors_have_weight():
    profile = get_profile()
    for sector, config in profile["sectors"].items():
        assert "weight" in config, f"Sector '{sector}' missing weight"
        assert 0 <= config["weight"] <= 1.0, f"Sector '{sector}' weight out of range"

def test_scoring_weights_sum_to_100():
    profile = get_profile()
    total = sum(profile["scoring"]["weights"].values())
    assert total == 100, f"Scoring weights sum to {total}, expected 100"

def test_no_excluded_sector_in_sectors():
    profile = get_profile()
    excluded = set(s.lower() for s in profile["excluded_sectors"])
    for sector in profile["sectors"]:
        assert sector.lower() not in excluded, \
            f"Sector '{sector}' is both in sectors and excluded_sectors"


# ── Database ─────────────────────────────────────────────────────────────────

def test_db_connects():
    conn = get_conn()
    assert conn is not None
    conn.close()

def test_all_tables_exist():
    conn = get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {"companies", "jobs", "applications", "watch_log", "run_log", "signals"}
    for t in expected:
        assert t in tables, f"Missing table: {t}"
    conn.close()

def test_companies_imported():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    assert count >= 300, f"Expected 300+ companies, got {count}"
    conn.close()

def test_all_companies_have_url():
    conn = get_conn()
    bad = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE career_page_url IS NULL OR career_page_url = ''"
    ).fetchone()[0]
    assert bad == 0, f"{bad} companies missing career page URL"
    conn.close()

def test_priority_values_valid():
    conn = get_conn()
    bad = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE priority NOT IN ('HIGH','MEDIUM','LOW')"
    ).fetchone()[0]
    assert bad == 0, f"{bad} companies have invalid priority value"
    conn.close()

def test_no_duplicate_company_names():
    conn = get_conn()
    dupes = conn.execute(
        "SELECT name, COUNT(*) as n FROM companies GROUP BY name HAVING n > 1"
    ).fetchall()
    assert len(dupes) == 0, f"Duplicate companies: {[r[0] for r in dupes]}"
    conn.close()

def test_high_priority_sectors_present():
    conn = get_conn()
    high_sectors = conn.execute(
        "SELECT DISTINCT sector FROM companies WHERE priority = 'HIGH'"
    ).fetchall()
    sectors = {r[0] for r in high_sectors}
    expected = {"Alt Materials & Packaging", "Ocean & Blue Economy",
                "Food Systems & Waste", "Circular Economy"}
    for s in expected:
        assert s in sectors, f"Expected HIGH sector missing: {s}"
    conn.close()

def test_jobs_table_empty_on_init():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 0, "Jobs table should be empty before first run"
    conn.close()

def test_row_factory_returns_dict_like():
    conn = get_conn()
    row = conn.execute("SELECT * FROM companies LIMIT 1").fetchone()
    assert row["name"] is not None
    assert row["career_page_url"] is not None
    conn.close()
