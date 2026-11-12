# Submission

- **Candidate:** Nishan Kashyap
- **Assignment:** A, Interaction Ledger & Reporting Pipeline
- **Candidate time spent:** I spent approximately 30 minutes setting up the project on Windows
  and reviewing the verification summary. The summary showed that all 13 tests passed and all
  five required reports were generated.

## How to run

From the repository root on Windows PowerShell with Python 3.11 or later (verified locally with
Python 3.13.5):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:LEDGER_KEY = (.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
.\.venv\Scripts\python.exe assignment-a-data/tools/generate_data.py --seed 20261112 --scale small --out data
.\.venv\Scripts\python.exe -m solution --db data/ledger.sqlite3 ingest data/drop_1
.\.venv\Scripts\python.exe -m solution --db data/ledger.sqlite3 ingest data/drop_2
.\.venv\Scripts\python.exe -m solution --db data/ledger.sqlite3 report --out data/reports
.\.venv\Scripts\python.exe -m pytest -q
```

Keep the same key for later ingestion into that database. On macOS or Linux, use
`.venv/bin/python` in place of the Windows executable and export the generated key as
`LEDGER_KEY`. A new key requires a fresh database. Reports only need the database.

## Design

SQLite stores merged leads, row rejections, drop manifests with counters, and an append-only
event ledger. Each drop is an atomic write transaction. A content fingerprint over relative
paths and file hashes makes exact replays no-ops. A unique event ID deduplicates across drops.
A keyed HMAC supports phone matching, and Fernet encrypts schema-marked PII and original events.
Reports use the masked phone and the other projected values they need.

Reports resolve events against the current leads in a database snapshot. An override links
through the workflow and call ID, even when it arrives before the other events for that call.
The latest override takes precedence over any automated disposition. The report pass builds
call facts and aggregates these to leads, batches, and IST dates.

## Decisions and failure modes

- Multiple non-empty alias columns use the leftmost value. Equal disposition timestamps use
  ingestion order. Repeated initiations for the same call use the earliest valid timestamp.
- Unmatched and unknown-disposition events stay in the encrypted ledger. Quality counters apply
  the rules in order using the current lead set.
- An invalid event timestamp cannot establish call timing or disposition precedence. Events with
  usable IDs still participate in deduplication.
- Drops are treated as immutable deliveries. An edited drop is a new delivery, not a patch.
  SQLite rolls back an interrupted drop. A subsequent run skips earlier committed drops.
- Report generation reads the ledger into memory. Files can be regenerated if writing is interrupted.

## Testing and results

Codex CLI verified the existing draft locally on Windows with **Python 3.13.5**, pytest 8.3.4,
and cryptography 44.0.1. The review environment used `python -m venv --system-site-packages .venv`
and checked requirements with `pip --disable-pip-version-check install --no-index -r requirements.txt`.
This reused installed dependencies without downloads; a fully isolated clean installation was
not exercised.

**All 13 tests passed** (`13 passed in 0.97s`; test subprocess elapsed time: 3.018 s). Tests cover
phone formats, day-first dates, rounding, bucket precedence, late event linkage, override
precedence, event and drop deduplication, and protection of test PII. For this run, pytest plugin
autoload was disabled with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and temporary test files were
directed to `data/local_review_seed_20261112/pytest_tmp`.

The supplied generator ran at `--scale small` with fresh **seed 20261112**. Both drops were
ingested, producing 1,897 leads and 2,642 counted calls, including 1,530 connected calls.
An independent check recomputed all five reports directly from the raw synthetic CSV/JSONL
inputs without importing solution functions. Every report matched, including CSV headers,
row ordering and values, and all JSON quality counters:

| Report | Verified result |
| --- | --- |
| `batch_summary.csv` | 6 data rows |
| `daily_overview.csv` | 33 data rows |
| `lead_report.csv` | 1,897 data rows |
| `rejections.csv` | 103 data rows |
| `data_quality.json` | All counters matched the independent calculation |

Replaying both drops left the database and all five reports byte-for-byte unchanged. Ingesting
both drops in one command into a fresh database also produced byte-identical reports. Reporting
worked without `LEDGER_KEY`. SQLite integrity and foreign-key checks passed, ledger update and
delete attempts were blocked in an in-memory database copy, and scans found no plaintext
customer names or canonical phones from the valid leads in the persisted database.

Measured CLI subprocess timings, including process startup, for this local small-scale run:

| Operation | Elapsed time |
| --- | ---: |
| Ingest drop 1 | 1.5187 s |
| Ingest drop 2 | 1.3056 s |
| Total ingestion, sum of the two commands | 2.8243 s |
| Generate final reports after both drops | 0.5716 s |
| Ingest both drops in one command into a fresh database | 2.1754 s |
| Replay both drops | 0.4546 s |

These are measurements from one local run; the large-scale performance stretch was not tested.
No evaluator reference reports were available. Local evidence is retained under
`data/local_review_seed_20261112/`, including `pytest.log`, `pipeline_runs.json`,
`pipeline_verification.json`, and `independent_verification.json`. All 25 original files were
unchanged at the end of verification, before this documentation update. ChatGPT Work drafted
the solution; Codex CLI performed this local verification. See `AI_USAGE.md` for attribution.

## If I had another day

Benchmark the large scale and move reporting aggregation into SQLite if memory becomes a bottleneck.
