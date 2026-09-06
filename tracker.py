#!/usr/bin/env python3
"""
tracker.py
----------
Minimal application tracker on SQLite (one local file, no server).

Usage:
    python tracker.py add --company "Acme" --role "Backend Engineer" \
        --link "https://..." --cv-version "acme_backend"
    python tracker.py list
    python tracker.py list --status applied
    python tracker.py update --id 3 --status "colloquio_1"
    python tracker.py stats
"""

import argparse
import sqlite3
from pathlib import Path
from datetime import datetime

# Agreed path: ogunjob.db at repo root (see schema.sql). File-relative so
# the DB does not depend on cwd.
DB_PATH = Path(__file__).resolve().parent / "ogunjob.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    link TEXT,
    cv_version TEXT,
    status TEXT DEFAULT 'da_inviare',
    applied_on TEXT,
    last_update TEXT,
    notes TEXT
);
"""

# Suggested statuses (free text; you can use others):
# da_inviare -> applied -> screening -> colloquio_1 -> colloquio_2 ->
# offerta -> rifiutato -> ritirato


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    return conn


def cmd_add(args):
    conn = get_conn()
    now = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT INTO applications (company, role, link, cv_version, status, applied_on, last_update)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (args.company, args.role, args.link, args.cv_version, args.status, now, now),
    )
    conn.commit()
    print(f"Added application: {args.company} - {args.role}")


def cmd_list(args):
    conn = get_conn()
    query = "SELECT id, company, role, status, applied_on, cv_version FROM applications"
    params = ()
    if args.status:
        query += " WHERE status = ?"
        params = (args.status,)
    query += " ORDER BY applied_on DESC"
    rows = conn.execute(query, params).fetchall()
    if not rows:
        print("No applications found.")
        return
    print(f"{'ID':<4}{'Company':<20}{'Role':<28}{'Status':<15}{'Date':<12}{'CV':<15}")
    print("-" * 94)
    for r in rows:
        print(f"{r[0]:<4}{r[1][:19]:<20}{r[2][:27]:<28}{r[3]:<15}{r[4]:<12}{(r[5] or ''):<15}")


def cmd_update(args):
    conn = get_conn()
    now = datetime.now().strftime("%Y-%m-%d")
    fields, values = [], []
    if args.status:
        fields.append("status = ?")
        values.append(args.status)
    if args.notes:
        fields.append("notes = ?")
        values.append(args.notes)
    fields.append("last_update = ?")
    values.append(now)
    values.append(args.id)
    conn.execute(f"UPDATE applications SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    print(f"Application {args.id} updated.")


def cmd_stats(args):
    conn = get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM applications GROUP BY status ORDER BY COUNT(*) DESC"
    ).fetchall()
    total = sum(r[1] for r in rows)
    print(f"Total applications: {total}\n")
    for status, count in rows:
        print(f"  {status:<15} {count}")


def main():
    parser = argparse.ArgumentParser(description="Application tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--company", required=True)
    p_add.add_argument("--role", required=True)
    p_add.add_argument("--link", default="")
    p_add.add_argument("--cv-version", default="")
    p_add.add_argument("--status", default="applied")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.set_defaults(func=cmd_list)

    p_update = sub.add_parser("update")
    p_update.add_argument("--id", required=True, type=int)
    p_update.add_argument("--status", default=None)
    p_update.add_argument("--notes", default=None)
    p_update.set_defaults(func=cmd_update)

    p_stats = sub.add_parser("stats")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
