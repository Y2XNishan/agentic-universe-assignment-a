import csv
import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from solution.ingest import DDL, Protection, canonical_phone, ingest_drop, load_schemas, normalize
from solution.report import Call, bucket, write_reports


@pytest.mark.parametrize("raw", ["9876543210", "+91 98765-43210", "09876543210"])
def test_phone_formats(raw):
    assert canonical_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", ["5123456789", "987654321", "+1 9876543210"])
def test_invalid_phones(raw):
    with pytest.raises(ValueError):
        canonical_phone(raw)


@pytest.mark.parametrize("raw", ["2026-12-01", "01/12/2026", "01-12-2026", "01-dEc-2026"])
def test_day_first_dates(raw):
    assert normalize(raw, {"type": "date"}) == "2026-12-01"


def test_invalid_date_and_rounding():
    with pytest.raises(ValueError):
        normalize("31/02/2026", {"type": "date"})
    assert normalize("Rs. 1,001.125", {"type": "decimal"}) == "1001.13"


def test_bucket_priority():
    flags = {"renewed": (True, True)}
    assert bucket([], flags) == "yet_to_reach_out"
    assert bucket([Call(failed=True)], flags) == "in_progress_telephony_failed"
    assert bucket([Call(failed=True), Call()], flags) == "in_progress_dialed"
    assert bucket([Call(failed=True, disposition=(True, None, 1, "renewed"))], flags) == "completed"


def write_drop(root, batch, rows, events):
    leads = root / "leads" / "motor_renewal"
    leads.mkdir(parents=True)
    (leads / (batch + ".csv")).write_text(
        "insured_name,Mobile No,policy no,expiry,language\n" + rows, encoding="utf-8"
    )
    (root / "events").mkdir()
    (root / "events" / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n{bad json\n", encoding="utf-8"
    )


def test_incremental_relink_override_and_replay(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript(DDL)
    protection = Protection(Fernet.generate_key())
    schemas = load_schemas()
    base = {"workflow_id": "motor_renewal", "call_id": "call_1", "phone": "+91 98765-43210"}
    events = [
        dict(base, event_id="override", event_type="disposition.override", phone=None,
             occurred_at="2026-10-06 05:28:00", payload={"disposition_code": "renewed"}),
        dict(base, event_id="start", event_type="call.initiated", occurred_at="2026-10-05T19:00:00Z"),
        dict(base, event_id="connect", event_type="call.connected", occurred_at="2026-10-06T00:31:00+05:30"),
        dict(base, event_id="set", event_type="disposition.set", occurred_at="2026-10-06 06:00:00",
             payload={"disposition_code": "not_interested"}),
        dict(base, event_id="unknown", event_type="disposition.set", occurred_at="2026-10-06 07:00:00",
             payload={"disposition_code": "missing"}),
    ]
    first, second = tmp_path / "drop_1", tmp_path / "drop_2"
    out = tmp_path / "reports"
    write_drop(first, "batch_001", ",,,,\n,invalid,REF,,\n", events)
    write_drop(second, "batch_002",
               "Private Name,09876543210,REF-1,01/12/2026,hi\n"
               "Updated Name,9876543210,REF-2,31/02/2026,\n", [events[1]])
    ingest_drop(db, first, schemas, protection)
    write_reports(db, out)
    assert json.loads((out / "data_quality.json").read_text())["orphan_events"] == 5

    ingest_drop(db, second, schemas, protection)
    write_reports(db, out)
    first_result = {path.name: path.read_bytes() for path in out.iterdir()}
    ingest_drop(db, second, schemas, protection)
    write_reports(db, out)
    assert first_result == {path.name: path.read_bytes() for path in out.iterdir()}

    with (out / "lead_report.csv").open(newline="") as stream:
        assert list(csv.DictReader(stream)) == [{
            "workflow_id": "motor_renewal", "external_ref": "REF-2", "batch_id": "batch_002",
            "phone_masked": "+91******3210", "bucket": "completed", "total_calls": "1",
            "connected_calls": "1", "last_call_at": "2026-10-06T00:30:00+05:30",
            "current_disposition": "renewed", "ever_success": "true",
        }]
    assert (out / "daily_overview.csv").read_text().splitlines()[1] == (
        "motor_renewal,2026-10-06,1,1,1.0000,1"
    )
    assert json.loads(first_result["data_quality.json"]) == {
        "lead_rows_total": 3, "lead_rows_rejected": 1,
        "rejections_by_reason": {"missing_required:customer_name": 1},
        "duplicate_lead_rows_merged": 1, "field_warnings": 1, "events_total": 8,
        "malformed_events": 2, "duplicate_events_dropped": 1, "orphan_events": 0,
        "unknown_disposition_events": 1,
    }
    attributes = json.loads(db.execute("SELECT attributes FROM leads").fetchone()[0])
    assert attributes["preferred_language"] == "hi"
    assert attributes["policy_expiry"] == "2026-12-01"
    assert b"Private Name" not in db_path.read_bytes()
    assert b"Updated Name" not in db_path.read_bytes()
    assert b"9876543210" not in db_path.read_bytes()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM events")
    db.close()
