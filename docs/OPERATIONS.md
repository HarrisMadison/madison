# Folder Intelligence — Operator Runbook

**Purpose**: day-to-day operations for the folder intelligence pipeline.
For architecture, see `docs/bigquery_v1_design.md`. For deeper
troubleshooting of older issues, see `docs/TROUBLESHOOTING.md`.

**Last updated**: 2026-05-14 (BigQuery v1 stabilized)

---

## 1. How data flows

```
   Chat (Bob UI)
        │
        │  user asks "summarize <folder>" or "what's missing"
        ▼
   job_intelligence.py
        │
        │  builds structured_summary, normalizes via
        │  _normalize_structured_summary, writes to JSONL
        ▼
   data/structured_summaries/structured_summary_events.jsonl
        │
        │  append-only event log, one JSON object per line
        │  (schema 1.1 contract, see bigquery_v1_design.md)
        ▼
   scripts/bq_loader.py            (manual invocation, v1)
        │
        │  reads JSONL, shapes 4 row sets, MERGE/INSERT into BQ
        ▼
   BigQuery: madison-rag-60.folder_intelligence
   ├── folder_intelligence_events      (parent, 1 row per event)
   ├── structured_fields_long          (1 row per extracted field)
   ├── document_inventory_items        (1 row per file observed)
   └── open_items_long                 (1 row per checklist line)
```

**Key contract**: JSONL is source of truth. BigQuery is a derived
view. The loader can be re-run any time and will not duplicate rows
(idempotent via deterministic `event_id`).

---

## 2. Commands

All commands run from the repo root:
`C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve`

### 2.1 Generate samples (curated folder batch)

Drives the chat endpoint against folders listed in
`selected_folders.txt` (one folder name per line). Each folder gets two
prompts: a folder summary and an open-items check. Records persist to
the JSONL automatically.

```powershell
python scripts/generate_structured_summary_samples.py `
  --folders-file selected_folders.txt
```

**Expect**: ~7–10 minutes for 13 folders. Run report shows attempt
counts and JSONL row delta (e.g. `+21`). Failed prompts are logged but
do not abort the batch.

Optional pre-flight:

```powershell
python scripts/generate_structured_summary_samples.py `
  --folders-file selected_folders.txt --dry-run
```

### 2.2 Analyze JSONL

Two views, same script:

```powershell
# Cumulative history (everything ever written)
python scripts/analyze_structured_summaries.py

# Most recent batch only (auto-detected, 15-min cluster threshold)
python scripts/analyze_structured_summaries.py --latest-run

# Records since a specific timestamp
python scripts/analyze_structured_summaries.py `
  --since 2026-05-14T00:00:00Z
```

**Use `--latest-run` after a parser or classifier change** — cumulative
includes records emitted under the old parser and will under-report
current behavior.

### 2.3 Load JSONL → BigQuery

```powershell
# Verify auth + shape; no writes
python scripts/bq_loader.py --dry-run

# Real load (idempotent; safe to re-run)
python scripts/bq_loader.py

# Load only records newer than TIMESTAMP
python scripts/bq_loader.py --since 2026-05-14T00:00:00Z
```

**Run after every sample batch** (v1 has no scheduled loader). Loader
exits non-zero if anything goes wrong; check stderr.

### 2.4 Smoke queries

```powershell
# Run all 5 queries
python scripts/bq_smoke_queries.py

# Run a specific query (1-5)
python scripts/bq_smoke_queries.py --query 4
```

### 2.5 Direct BigQuery row counts (sanity check)

```powershell
bq query --use_legacy_sql=false --location=us-central1 `
  --project_id=madison-rag-60 "
SELECT
  (SELECT COUNT(*) FROM ``madison-rag-60.folder_intelligence.folder_intelligence_events``) AS events,
  (SELECT COUNT(*) FROM ``madison-rag-60.folder_intelligence.structured_fields_long``)    AS fields,
  (SELECT COUNT(*) FROM ``madison-rag-60.folder_intelligence.document_inventory_items``)  AS inventory,
  (SELECT COUNT(*) FROM ``madison-rag-60.folder_intelligence.open_items_long``)           AS open_items
"
```

---

## 3. What success looks like

### 3.1 After `generate_structured_summary_samples.py`

The run report should show:

```
Folders attempted:        N      (= line count of selected_folders.txt)
Summary prompts ok:       N/N    (every folder produced a summary)
Open-items prompts ok:    M/N    (M ≤ N; some folders skip the checklist)
Summary prompts failed:   0      (anything > 0 needs investigation)
JSONL rows: X -> Y        (+delta)   (delta ≈ 2 × N if all prompts succeeded)
```

A delta below `2 × N` means one or more prompts failed silently
(usually a Gemini 429 or upstream Vertex hiccup). Re-running the batch
will retry those folders.

### 3.2 After `bq_loader.py`

Expect:

```
folder_intelligence_events:   MERGE affected K row(s)   (staging: K)
structured_fields_long:       INSERT M row(s)
document_inventory_items:     INSERT P row(s)
open_items_long:              INSERT Q row(s)
```

Where K = total events read (143 as of v1 baseline).

**Idempotency check**: run the loader twice. The second run prints
the same numbers for staging counts; net BigQuery row counts (from
section 2.5) should be unchanged.

Note: parent `MERGE affected K row(s)` is K on every run, not 0. The
loader rewrites `loaded_at` even when content is unchanged. This is
expected; see "Known follow-up items" below.

### 3.3 After `bq_smoke_queries.py`

All 5 queries return non-empty results. Specifically:

| Query | Expect |
|---|---|
| Q1 — latest state per folder | One row per distinct folder; `folder_purpose` populated for all rows |
| Q2 — folders by purpose | Three or fewer rows (the three purpose enum values); totals match Q1 |
| Q3 — open items distribution | Each `label` appears with one or more `status` values |
| Q4 — extracted field population | `folder_name` and `folder_purpose` at 100%; `property_address` should be highest data-extracted field |
| Q5 — marker file inventory | Folders with markers appear at the top; non-marker folders show `marker_docs=0` |

### 3.4 Baseline row counts (post-load, v1)

As of 2026-05-14 with 143 events loaded:

```
folder_intelligence_events:   143
structured_fields_long:       596
document_inventory_items:     1242
open_items_long:              438
```

Growth is expected; absolute numbers will rise. Watch the **ratios** —
they should stay roughly proportional. If `document_inventory_items`
suddenly jumps much faster than `folder_intelligence_events`, a folder
with hundreds of files entered the corpus (not a bug, just notable).

---

## 4. When things go wrong

### 4.1 Malformed JSONL row

**Symptom**: `bq_loader.py` or `analyze_structured_summaries.py` prints
`WARN: line N malformed JSON`.

**Action**:
1. Look at the line: `Get-Content data/structured_summaries/structured_summary_events.jsonl | Select-Object -Index (N-1)`
2. The line will not load into BigQuery. JSONL is append-only; we do
   not edit historical rows.
3. If the malformation came from a writer bug, fix the writer. The
   malformed line stays in the file as a forensic artifact.
4. If the line is recoverable, append a corrected version to the end
   of the JSONL. The original stays; the corrected line is treated as a
   new event (different `event_id` if the timestamp differs).

**Do not** delete or rewrite the JSONL file. It's append-only by
contract.

### 4.2 Loader auth error

**Symptom**:
```
ERROR: Google Application Default Credentials not found.
       Exact error: <message>
```

**Action**: Run one of:

```powershell
gcloud auth application-default login
```

or set a service-account JSON key:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\service-account.json"
```

Then re-run the loader.

### 4.3 BigQuery dataset or table missing

**Symptom**:
```
ERROR: Dataset madison-rag-60.folder_intelligence does not exist.
```
or
```
ERROR: Target tables missing in BigQuery: <list>
```

**Action** (one-time setup, only needed if dataset/tables don't exist):

```powershell
# Create the dataset
bq mk --dataset --location=us-central1 madison-rag-60:folder_intelligence

# Create the four tables (safe to re-run; uses CREATE IF NOT EXISTS)
Get-Content infra/bigquery/schema.sql -Raw | `
  bq query --use_legacy_sql=false --location=us-central1 `
  --project_id=madison-rag-60
```

The loader's error message includes these commands inline, so this is
also recoverable without consulting the runbook.

### 4.4 Row-count mismatch

**Symptom**: BigQuery row counts (section 2.5) don't match the loader
report's staging counts.

**Diagnose**:

1. **Is the loader running against the right JSONL?** Confirm the
   "Source:" path printed by the loader. If you have multiple checkouts
   of the repo, the relative path `data/structured_summaries/...` is
   relative to the loader, not your shell.

2. **Are there duplicate event_ids in staging?** The loader warns about
   this. Two records with identical `(folder_key, generated_at,
   response_kind, query)` hash to the same `event_id`. Last-write-wins
   on the parent table; child rows are replaced wholesale.

3. **Is the JSONL line count itself growing during the load?** If the
   chat is actively writing records while the loader runs, you'll see
   apparent drift. v1 has no locking. Workaround: run the loader when
   chat is idle.

4. **Compare expected vs. actual**:
   ```powershell
   # JSONL row count
   (Get-Content data/structured_summaries/structured_summary_events.jsonl | Measure-Object -Line).Lines

   # BigQuery row count (from section 2.5)
   ```
   These should match within the duplicate-event_id count noted in the
   loader's warning. If they don't, run with `--dry-run` and compare
   "Records read" to the BigQuery count.

### 4.5 Rerun / rollback behavior

**To re-load everything from scratch** (after fixing a loader bug, for
example):

```powershell
# Drop and recreate target tables (DESTRUCTIVE; child rows lost)
bq rm -f -t madison-rag-60:folder_intelligence.folder_intelligence_events
bq rm -f -t madison-rag-60:folder_intelligence.structured_fields_long
bq rm -f -t madison-rag-60:folder_intelligence.document_inventory_items
bq rm -f -t madison-rag-60:folder_intelligence.open_items_long

# Recreate from DDL
Get-Content infra/bigquery/schema.sql -Raw | `
  bq query --use_legacy_sql=false --location=us-central1 `
  --project_id=madison-rag-60

# Re-load from JSONL
python scripts/bq_loader.py
```

The JSONL is the source of truth; recreating BigQuery from it is
always safe and produces a complete state.

**To roll back to a specific point in time**:

The loader doesn't have a "load up to TIMESTAMP" mode (only `--since`).
For point-in-time rollback, drop and recreate the tables, then use a
filtered loader run. If this becomes a recurring need, the loader
should grow an `--until` flag — currently not implemented.

**Per-event rollback**: there is no per-event undo. If a bad batch
landed in BigQuery and you want it gone:

1. Identify the bad `event_id`s (by `loaded_at` or `generated_at` range).
2. `DELETE FROM <table> WHERE event_id IN (...)` against all four tables.
3. The JSONL retains the bad records; the loader will reinsert them on
   the next run unless you also fix the upstream cause or use `--since`
   to exclude their timestamp range.

This is intentionally low-ergonomics for v1 — bad batches are rare and
the safest recovery is to drop & re-load from a fixed JSONL.

### 4.6 Schema 1.0 records appearing

**Symptom**: analyzer reports `schema_version=1.0` records; BigQuery
loader shows them with NULL promoted scalars.

**Status**: expected. 4 legacy records (3% of corpus) predate the v1.1
schema contract. The loader handles them gracefully — they appear in
`folder_intelligence_events` with their `payload_json` preserved but
contribute zero rows to `structured_fields_long`. Promoted scalar
columns are NULL for these rows. No action needed.

### 4.7 `pip install google-cloud-bigquery` not present

**Symptom**:
```
ERROR: google-cloud-bigquery not installed: cannot import name 'bigquery'
```

**Action**:
```powershell
pip install google-cloud-bigquery
```

This is per-environment; if you switch Python virtualenvs you may need
to re-install.

---

## 5. Known follow-up items

These are deferred by choice, not by accident. None of them are
blocking v1 operations.

### 5.1 Loader trigger / schedule

**Status**: manual only. The loader runs when an operator invokes it.
BigQuery drifts behind the JSONL between runs.

**Options if/when this matters**:
- **Windows Task Scheduler**: run `python scripts/bq_loader.py` nightly
- **Cloud Run job**: containerize the loader, schedule via Cloud Scheduler
- **In-process side effect**: have the structured_summary writer push to
  BigQuery directly (more invasive; couples chat latency to BigQuery
  availability)

Recommended path: Task Scheduler nightly when corpus growth justifies
it. Until then, "run after a batch" is fine.

### 5.2 MERGE refinement (parent table)

**Current behavior**: parent MERGE rewrites `loaded_at` on every run,
so the loader always reports `affected K row(s)` rather than 0 on a
no-op re-run.

**Fix** (when desired): condition the `WHEN MATCHED` clause on actual
content change, e.g. `WHEN MATCHED AND T.payload_json != S.payload_json
THEN UPDATE`. Estimated 15 minutes; not worth doing until the table
is large enough that the wasted DML matters.

### 5.3 HTML / CompanyCam extraction

**Status**: not implemented. Marker files are correctly identified
(`is_marker=true` in inventory) but their text is not extracted. Three
folders in the curated set are marker-only or marker-heavy
(Michelle Berry, Trish Wallace, Chris Simon).

**When to revisit**: when the production corpus reveals enough
marker-only folders to make extraction worthwhile, or when a user
specifically asks "what's in the CompanyCam report for X." The
instrumentation to measure impact is already in place via
`structured_fields_long` field counts for marker folders.

### 5.4 Appraisal dossier tier-bump

**Status**: not implemented. `appraised_value` populates at ~7-8% of
property folders in the curated set because appraisal-bucket files
that rank below the top 5 get truncated to 800 chars (often missing
the value).

**Fix**: bump appraisal-bucket files to a higher char tier regardless
of rank. Localized change in `_build_folder_dossier`'s tiering. The
field-population impact is measurable post-load via
`structured_fields_long`.

### 5.5 Dashboard layer

**Status**: smoke queries are the only consumer. Useful, but operator-
facing.

**Options**:
- **Looker Studio**: free, plugs directly into BigQuery, gives non-
  operator stakeholders a folder-state view
- **Custom HTML page**: read-only Flask route that renders the smoke
  queries' output. Lives alongside the chat UI.

Either is half a day's work when prioritized.

---

## 6. Quick reference card

A copy-pasteable cheat sheet for the common workflows.

**"I want to generate fresh sample data and load it":**
```powershell
python scripts/generate_structured_summary_samples.py --folders-file selected_folders.txt
python scripts/analyze_structured_summaries.py --latest-run
python scripts/bq_loader.py
python scripts/bq_smoke_queries.py
```

**"I want to check what BigQuery currently has":**
```powershell
python scripts/bq_smoke_queries.py
```

**"The loader is failing — where do I look?":**
1. Read the stderr line. The loader's error messages include the
   exact command to fix common cases (missing dataset/tables, auth).
2. If unclear, run `python scripts/bq_loader.py --dry-run`. If dry-run
   succeeds, the failure is BigQuery-side (auth, permissions, dataset).
   If dry-run fails, the failure is JSONL-side (malformed lines).

**"I think rows are wrong":**
1. Count JSONL lines (section 4.4 command).
2. Count BigQuery rows (section 2.5 query).
3. Compare. Difference should equal `bad_lines + duplicate_event_ids`
   reported by the loader.

---

## 7. Where things live

| What | Where |
|---|---|
| Architecture | `docs/bigquery_v1_design.md` |
| This runbook | `docs/OPERATIONS.md` |
| Older troubleshooting | `docs/TROUBLESHOOTING.md` |
| DDL | `infra/bigquery/schema.sql` |
| Loader | `scripts/bq_loader.py` |
| Smoke queries | `scripts/bq_smoke_queries.py` |
| Analyzer | `scripts/analyze_structured_summaries.py` |
| Sample generator | `scripts/generate_structured_summary_samples.py` |
| Curated folder list | `selected_folders.txt` |
| JSONL (source of truth) | `data/structured_summaries/structured_summary_events.jsonl` |
| Main pipeline | `scripts/job_intelligence.py` |
| Routes | `scripts/phase4_routes.py` |
| Chat UI | `scripts/templates/bob_chat.html` |
