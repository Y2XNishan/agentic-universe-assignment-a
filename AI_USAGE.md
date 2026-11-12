# AI Usage Disclosure

## Tools used

- **ChatGPT Work:** drafted the initial solution and supporting documentation.
- **Codex CLI:** reviewed and verified the existing draft locally, then updated `SUBMISSION.md`
  and this disclosure with the verified results.

## What I used it for

ChatGPT Work read the briefs, recommended Assignment A, and drafted the SQLite schema, lead and
event ingestion, report generation, CLI, tests, and submission notes.

Codex CLI read Assignment A and the Python draft, set up the local Python environment, ran the
existing tests, generated fresh synthetic data, exercised the pipeline, and independently
checked the reports against the raw inputs. It also checked replay behavior, ingestion-mode
consistency, database integrity, append-only protection, and plaintext PII exposure in the
database. The verification was local, used no downloads, and made no source-code changes.

## What I wrote or decided myself

I spent approximately 30 minutes setting up the project on Windows and reviewing the
verification summary. The summary showed that all 13 tests passed and all five required
reports were generated.

The automated checks below were performed by Codex CLI.

## Where the AI was wrong or unhelpful

The earlier draft recorded an attempt to use an unavailable timing executable. Timings in the
current local verification were measured with Python's `time.perf_counter()` around CLI
subprocesses. No functional failures were found on seed 20261112; this run did not exercise
the large-scale dataset or compare with evaluator reference reports.

## How I verified AI-generated code

Codex CLI performed the following checks on Windows with **Python 3.13.5**, pytest 8.3.4, and
cryptography 44.0.1. The `.venv` reused installed packages via `--system-site-packages`, and
requirements were checked with `--no-index`; a fully isolated clean installation was not tested.

- **All 13 existing tests passed:** pytest reported 0.97 s; the test subprocess took 3.018 s.
  Pytest plugin autoload was disabled for this run.
- Generated the supplied small-scale dataset with fresh **seed 20261112** and ingested both drops.
- Independently recomputed `batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`,
  `rejections.csv`, and `data_quality.json` from raw CSV/JSONL inputs, without importing solution
  functions. All five matched, including headers, sorted CSV rows, values, and quality counters.
- Confirmed that replay left the database and reports byte-for-byte unchanged, and that ingesting
  both drops in one command into a fresh database produced byte-identical reports.
- Confirmed SQLite integrity, foreign-key integrity, reporting without a ledger key, blocked
  ledger updates/deletes in an in-memory copy, and absence of plaintext names and canonical
  phones from valid leads in the persisted database.

Measured timings were 1.5187 s for drop 1 ingestion and 1.3056 s for drop 2, totaling **2.8243 s**;
final report generation took **0.5716 s**. Combined ingestion into a fresh database took 2.1754 s,
and replay took 0.4546 s. These CLI subprocess measurements include startup and describe one
local small-scale run.

Local verification evidence is in `data/local_review_seed_20261112/`. Hash checks confirmed all
25 original files were unchanged when verification finished, before these two Markdown files
were updated.
