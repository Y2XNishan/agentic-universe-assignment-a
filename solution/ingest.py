"""Normalize incoming drops and append their events to SQLite."""

import base64
import csv
import hashlib
import hmac
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from cryptography.fernet import Fernet

IST = timezone(timedelta(hours=5, minutes=30))
SCHEMAS = Path(__file__).resolve().parents[1] / "assignment-a-data" / "schemas"

DDL = """
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drops (
    digest TEXT PRIMARY KEY, counters TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    workflow TEXT NOT NULL, phone_key TEXT NOT NULL, batch TEXT NOT NULL,
    phone_masked TEXT NOT NULL, attributes TEXT NOT NULL,
    PRIMARY KEY (workflow, phone_key)
);
CREATE TABLE IF NOT EXISTS rejections (
    drop_digest TEXT NOT NULL REFERENCES drops(digest), source TEXT NOT NULL,
    workflow TEXT NOT NULL, batch TEXT NOT NULL, row_number INTEGER NOT NULL,
    reason TEXT NOT NULL, PRIMARY KEY (drop_digest, source, row_number)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
    workflow TEXT NOT NULL, phone_key TEXT, call_id TEXT NOT NULL,
    kind TEXT NOT NULL, occurred_at TEXT, code TEXT, protected_event TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'interaction ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'interaction ledger is append-only'); END;
"""


class Protection:
    def __init__(self, key):
        self.cipher = Fernet(key)
        secret = base64.urlsafe_b64decode(key)
        self.index_key = hmac.digest(secret, b"phone-index-v1", "sha256")
        self.key_id = hmac.digest(secret, b"key-check-v1", "sha256").hex()

    def phone_key(self, workflow, phone):
        return hmac.digest(self.index_key, f"{workflow}\0{phone}".encode(), "sha256").hex()

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()


def header(value):
    return re.sub(r"[_\s]+", " ", value.strip().lower())


def canonical_phone(value):
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"[6-9][0-9]{9}", digits):
        raise ValueError("invalid phone")
    return "+91" + digits


def normalize(value, field):
    match field["type"]:
        case "string":
            return value
        case "phone":
            return canonical_phone(value)
        case "date":
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y"):
                try:
                    return datetime.strptime(value, fmt).date().isoformat()
                except ValueError:
                    continue
            raise ValueError("invalid date")
        case "decimal":
            amount = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", value, flags=re.I)
            if not re.fullmatch(r"\+?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", amount):
                raise ValueError("invalid decimal")
            return str(Decimal(amount.replace(",", "")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ))
        case "enum":
            candidate = value.lower()
            if candidate in {option.lower() for option in field["values"]}:
                return candidate
            raise ValueError("invalid enum")
        case _:
            raise ValueError(f"unsupported field type: {field['type']}")


def load_schemas():
    schemas = {}
    for path in sorted(SCHEMAS.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[schema["workflow_id"]] = schema["fields"]
    return schemas


def lead_values(cells, columns, fields):
    values = {}
    warnings = 0
    for field in fields:
        name = field["name"]
        aliases = {header(label) for label in [name, *field.get("aliases", [])]}
        raw = next(
            (value.strip() for column, value in zip(columns, cells)
             if column in aliases and value.strip()), ""
        )
        if not raw:
            if field.get("required"):
                return {}, 0, f"missing_required:{name}"
            continue
        try:
            values[name] = normalize(raw, field)
        except (ValueError, InvalidOperation):
            if field["type"] == "phone":
                return {}, 0, "invalid_phone"
            if field.get("required"):
                return {}, 0, f"invalid_required:{name}"
            warnings += 1
    return values, warnings, None


def ingest_leads(db, root, digest, paths, schemas, protection, counts):
    for path in paths:
        workflow, batch = path.parent.name, path.stem
        fields = schemas[workflow]
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream)
            columns = [header(value) for value in next(rows, [])]
            nonblank = (cells for cells in rows if any(value.strip() for value in cells))
            for row_number, cells in enumerate(nonblank, 1):
                counts["lead_rows_total"] += 1
                values, warnings, reason = lead_values(cells, columns, fields)
                if reason:
                    db.execute("INSERT INTO rejections VALUES (?, ?, ?, ?, ?, ?)",
                               (digest, path.relative_to(root).as_posix(), workflow,
                                batch, row_number, reason))
                    continue
                counts["field_warnings"] += warnings
                phone = values["phone"]
                key = protection.phone_key(workflow, phone)
                previous = db.execute(
                    "SELECT attributes FROM leads WHERE workflow=? AND phone_key=?", (workflow, key)
                ).fetchone()
                merged = json.loads(previous["attributes"]) if previous else {}
                for field in fields:
                    name = field["name"]
                    if name in values:
                        value = values[name]
                    elif name not in merged and "default" in field:
                        value = field["default"]
                    else:
                        continue
                    merged[name] = protection.encrypt(value) if field.get("pii") else value
                if previous:
                    counts["duplicate_lead_rows_merged"] += 1
                    db.execute("UPDATE leads SET attributes=? WHERE workflow=? AND phone_key=?",
                               (json.dumps(merged), workflow, key))
                else:
                    db.execute("INSERT INTO leads VALUES (?, ?, ?, ?, ?)",
                               (workflow, key, batch, "+91******" + phone[-4:], json.dumps(merged)))


def ist_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=IST)
        return timestamp.astimezone(IST).isoformat()
    except (ValueError, OverflowError):
        return None


def ingest_events(db, paths, protection, counts):
    for path in paths:
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                counts["events_total"] += 1
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    event = None
                event_id = event.get("event_id") if isinstance(event, dict) else None
                if not isinstance(event_id, str) or not event_id.strip():
                    counts["malformed_events"] += 1
                    continue
                if db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                    counts["duplicate_events_dropped"] += 1
                    continue
                workflow = event.get("workflow_id")
                workflow = workflow if isinstance(workflow, str) else ""
                phone_key = None
                if isinstance(event.get("phone"), str):
                    try:
                        phone_key = protection.phone_key(workflow, canonical_phone(event["phone"]))
                    except ValueError:
                        pass
                payload = event.get("payload")
                code = payload.get("disposition_code") if isinstance(payload, dict) else None
                call_id = event.get("call_id")
                kind = event.get("event_type")
                db.execute(
                    """INSERT INTO events
                       (event_id, workflow, phone_key, call_id, kind, occurred_at, code, protected_event)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, workflow, phone_key, call_id if isinstance(call_id, str) else "",
                     kind if isinstance(kind, str) else "", ist_timestamp(event.get("occurred_at")),
                     code if isinstance(code, str) else None, protection.encrypt(line)),
                )


def ingest_drop(db, root, schemas, protection):
    leads = sorted(root.glob("leads/*/*.csv"))
    events = sorted(root.glob("events/*.jsonl"))
    if not leads and not events:
        raise ValueError(f"No lead or event files found in {root}")
    manifest = [(path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                for path in [*leads, *events]]
    digest = hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
    with db:
        db.execute("BEGIN IMMEDIATE")
        inserted = db.execute("INSERT OR IGNORE INTO drops VALUES (?, '{}')", (digest,))
        if not inserted.rowcount:
            return
        counts = Counter()
        ingest_leads(db, root, digest, leads, schemas, protection, counts)
        ingest_events(db, events, protection, counts)
        db.execute("UPDATE drops SET counters=? WHERE digest=?", (json.dumps(counts), digest))
