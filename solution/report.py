"""Derive reports from a consistent snapshot of the ledger."""

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from .ingest import SCHEMAS, load_schemas

BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")
COUNTERS = (
    "lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
    "duplicate_lead_rows_merged", "field_warnings", "events_total",
    "malformed_events", "duplicate_events_dropped", "orphan_events",
    "unknown_disposition_events",
)


@dataclass
class Call:
    initiated: datetime | None = None
    connected: bool = False
    failed: bool = False
    disposition: tuple | None = None

    @property
    def code(self):
        return self.disposition[3] if self.disposition else ""


def disposition_catalogue():
    with (SCHEMAS / "dispositions.csv").open(encoding="utf-8", newline="") as stream:
        return {(r["workflow_id"], r["code"]):
                (r["is_terminal"] == "true", r["is_success"] == "true")
                for r in csv.DictReader(stream)}


def collect_calls(leads, events, catalogue, counts):
    # Resolve call-to-lead links before processing overrides, even if an override arrived first.
    call_owners = {}
    for event in events:
        identity = (event["workflow"], event["phone_key"])
        if event["kind"] != "disposition.override" and identity in leads and event["call_id"]:
            call_owners.setdefault((event["workflow"], event["call_id"]), identity)

    calls = defaultdict(Call)
    for event in events:
        workflow, kind, code = event["workflow"], event["kind"], event["code"]
        identity = (workflow, event["phone_key"])
        if kind == "disposition.override":
            identity = call_owners.get((workflow, event["call_id"]))
        if identity not in leads:
            counts["orphan_events"] += 1
            continue
        is_disposition = kind.startswith("disposition.")
        if is_disposition and (workflow, code) not in catalogue:
            counts["unknown_disposition_events"] += 1
            continue
        if not event["call_id"]:
            continue

        call = calls[(identity, event["call_id"])]
        timestamp = datetime.fromisoformat(event["occurred_at"]) if event["occurred_at"] else None
        match kind:
            case "call.initiated":
                if timestamp and (call.initiated is None or timestamp < call.initiated):
                    call.initiated = timestamp
            case "call.connected":
                call.connected = True
            case "call.telephony_failed":
                call.failed = True
            case "disposition.set" | "disposition.override":
                if timestamp:
                    candidate = (kind == "disposition.override", timestamp, event["seq"], code)
                    if call.disposition is None or candidate > call.disposition:
                        call.disposition = candidate

    by_lead = defaultdict(list)
    for (identity, call_id), call in calls.items():
        if call.initiated is not None:
            by_lead[identity].append((call.initiated, call_id, call))
    return by_lead


def bucket(calls, dispositions):
    if not calls:
        return BUCKETS[0]
    if any(dispositions.get(call.code, (False, False))[0] for call in calls):
        return BUCKETS[3]
    if all(call.failed for call in calls):
        return BUCKETS[1]
    return BUCKETS[2]


def write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns.split(","))
        writer.writerows(rows)


def write_reports(db, out):
    schemas = load_schemas()
    reference = {workflow: next(f["name"] for f in fields if f.get("reference"))
                 for workflow, fields in schemas.items()}
    catalogue = disposition_catalogue()
    by_workflow = defaultdict(dict)
    for (workflow, code), flags in catalogue.items():
        by_workflow[workflow][code] = flags

    db.execute("BEGIN")
    with db:
        leads = {(row["workflow"], row["phone_key"]): row
                 for row in db.execute("SELECT * FROM leads")}
        events = list(db.execute(
            "SELECT seq, workflow, phone_key, call_id, kind, occurred_at, code FROM events ORDER BY seq"
        ))
        counts = Counter()
        for drop in db.execute("SELECT counters FROM drops"):
            counts.update(json.loads(drop["counters"]))
        rejections = list(db.execute(
            "SELECT workflow,batch,row_number,reason FROM rejections "
            "ORDER BY workflow,batch,row_number,source,drop_digest"
        ))

    calls_by_lead = collect_calls(leads, events, catalogue, counts)
    batches = defaultdict(Counter)
    days = defaultdict(Counter)
    success_leads = defaultdict(set)
    lead_rows = []
    for identity, lead in leads.items():
        workflow = lead["workflow"]
        dispositions = by_workflow[workflow]
        attributes = json.loads(lead["attributes"])
        calls = [entry[2] for entry in sorted(calls_by_lead[identity], reverse=True)]
        category = bucket(calls, dispositions)
        batches[(workflow, lead["batch"])][category] += 1
        ever_success = False
        for call in calls:
            day_key = (workflow, call.initiated.date().isoformat())
            days[day_key]["total"] += 1
            days[day_key]["connected"] += call.connected
            if dispositions.get(call.code, (False, False))[1]:
                ever_success = True
                success_leads[day_key].add(identity)
        lead_rows.append((
            workflow, attributes[reference[workflow]], lead["batch"], lead["phone_masked"],
            category, len(calls), sum(call.connected for call in calls),
            calls[0].initiated.isoformat() if calls else "",
            next((call.code for call in calls if call.code), ""), str(ever_success).lower(),
        ))

    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "batch_summary.csv", ",".join(("workflow_id", "batch_id", "total_leads", *BUCKETS)),
              ((*key, sum(group.values()), *(group[name] for name in BUCKETS))
               for key, group in sorted(batches.items())))
    write_csv(out / "daily_overview.csv",
              "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads",
              ((*key, group["total"], group["connected"],
                f"{group['connected'] / group['total']:.4f}", len(success_leads[key]))
               for key, group in sorted(days.items())))
    write_csv(out / "lead_report.csv",
              "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,"
              "last_call_at,current_disposition,ever_success",
              sorted(lead_rows))
    write_csv(out / "rejections.csv", "workflow_id,batch_id,row_number,reason", rejections)
    quality = {name: counts[name] for name in COUNTERS}
    quality["lead_rows_rejected"] = len(rejections)
    quality["rejections_by_reason"] = dict(sorted(Counter(r["reason"] for r in rejections).items()))
    (out / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
