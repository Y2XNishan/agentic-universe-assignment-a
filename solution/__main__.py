"""CLI for the Interaction Ledger & Reporting Pipeline."""

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .ingest import DDL, Protection, ingest_drop, load_schemas
from .report import write_reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("ledger.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("paths", nargs="+", type=Path, help="Drop paths or a directory containing drop_*")
    report = commands.add_parser("report")
    report.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "ingest":
        try:
            protection = Protection(os.environ.get("LEDGER_KEY", "").encode())
        except ValueError:
            parser.error("Set LEDGER_KEY to a valid Fernet key before ingesting.")
    elif not args.db.is_file():
        parser.error(f"Database does not exist: {args.db}")

    with closing(sqlite3.connect(args.db, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if args.command == "report":
            write_reports(db, args.out)
            return

        db.executescript(DDL)
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('key_id', ?)", (protection.key_id,))
            key_id = db.execute("SELECT value FROM settings WHERE name='key_id'").fetchone()[0]
            if key_id != protection.key_id:
                parser.error("LEDGER_KEY does not match the key used to initialize this database.")
        schemas = load_schemas()
        for path in args.paths:
            if (path / "leads").is_dir() or (path / "events").is_dir():
                drops = [path]
            else:
                drops = sorted(child for child in path.glob("drop_*") if child.is_dir())
            if not drops:
                parser.error(f"No drops found at {path}")
            for drop in drops:
                try:
                    ingest_drop(db, drop, schemas, protection)
                except (ValueError, KeyError) as error:
                    parser.error(str(error))


if __name__ == "__main__":
    main()
