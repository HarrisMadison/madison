# Folder Intelligence — Operator Runbook

**Purpose**: day-to-day operations for the folder intelligence pipeline.
For architecture, see `docs/bigquery_v1_design.md`. For deeper
troubleshooting of older issues, see `docs/TROUBLESHOOTING.md`.

**Last updated**: 2026-05-19 (admin/template ship + sidecar revert incident + §5.6–§5.9 documentation gap noted; see §5.6, §5.10, §5.11, §5.12)

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

### 5.6 Project history — §5.6 through §5.9 documentation lost (2026-05-19)

**Status**: documentation gap. **This is not a record of work that did
not happen.** It is a record of documentation that was lost because
of a working-tree commit-hygiene failure on 2026-05-19.

Between 2026-05-15 and 2026-05-18, four workstreams happened and were
documented in `OPERATIONS.md` as sections §5.6, §5.7, §5.8, and §5.9.
Those edits were never committed to git. On 2026-05-19, during the
§5.10 ship, a `Filesystem:edit_file` corruption to `OPERATIONS.md`
required `git checkout docs/OPERATIONS.md` to recover. The checkout
reverted to the last committed version (2026-05-14), which predates
§5.6. The four sections of prose documentation were lost in the same
operation. The session in progress chose not to reconstruct them from
memory, because reconstruction from session-window recollection would
record imperfect details as authoritative documentation.

What the four sections covered, at a summary level (no specifics
reconstructed from memory):

- **§5.6** — diagnosis that the doc_type classifier was the upstream
  bottleneck for `folder_purpose=unknown`. Identified
  `phase6_ocr_metadata._classify_doc_type` as the place to fix.
- **§5.7** — sidecar reclassification ship that added Madison-specific
  filename rules (5A: anchored `irs/ein`; 5B: `packout`, `packback`,
  `concln`, `strcleaning`, `reb-revise` etc. as `estimate`; 5C: fixed
  `\bestimate\b` word boundary).
- **§5.8** — proposed insurance_policy ambiguity refinement; simulated
  against the full corpus; rejected because it regressed canonical
  claim folders.
- **§5.9** — folder_key normalization probe; identified one collision
  (`Pack Out - Jeremy` / `Pack out - Jeremy`); deferred implementation
  because the migration cost (re-derive every event_id) was not
  justified by one duplicate row.

**Authoritative sources for these workstreams (durable, on disk):**

| Source | Lives at |
|---|---|
| §5.7 5A/5B/5C regex rules | `Phase5_oneDrive/phase6_ocr_metadata.py` |
| §5.7 ship vehicle (also used for §5.11 recovery) | `scripts/reclassify_doc_types_sidecar_only.py` |
| §5.7 pre-ship sidecar backup | `gs://madison-rag-60-rag-raw/manifests/doc_type_index.backup-20260518-170150.json` |
| §5.7 local pre-rebuild backup | `backups/doc_type_index.pre-rebuild.json` |
| §5.7 baseline snapshots | `backups/baseline_folder_purpose.txt`, `backups/baseline_doc_type.txt`, `backups/baseline_row_counts.txt` |
| §5.8 simulation probe | `scripts/probe_classifier_insurance_policy.py` |
| §5.9 inventory probe | `scripts/probe_folder_key_variants.py` |
| Static-probe variants used during scoping | `scripts/probe_doctype_static_v1.py`, `scripts/probe_doctype_static_v2.py`, `scripts/probe_classifier_simulation.py` |
| BigQuery state reflecting all four ships/probes | `madison-rag-60.folder_intelligence.*` (events, structured_fields_long, document_inventory_items, open_items_long, folder_latest_state) |
| JSONL ground truth | `data/structured_summaries/structured_summary_events.jsonl` (append-only) |
| Validation pack | `docs/VALIDATION_PACK.md`, `docs/validation_pack_folders.txt` |

**How to reconstruct these sections in the future:** read the code,
run the probes against current BigQuery state, inspect the BigQuery
distribution and `folder_latest_state` rows, then write the prose
from those observations. Do NOT reconstruct from session memory or
from this stub. The code and data are the authority.

**Note on §5.5 status:** the committed §5.5 above says the dashboard
layer was unbuilt at the time of the 2026-05-14 commit. Looker Studio
Page 1 (Folder Portfolio) was built around 2026-05-15 and is live in
production. The §5.5 prose in the committed file is stale.
`infra/bigquery/views.sql` documents the three views that back Page 1
(`folder_latest_event`, `folder_latest_state`,
`folder_latest_open_items`). Future runbook revisions should update
§5.5 to reflect the live dashboard.

---

### 5.10 admin/template folder detection v1 (2026-05-19)

**Status**: shipped. Two-edit code change to
`scripts/job_intelligence.py`. Validated via
`scripts/probe_classifier_admin_folder.py` (7/7 pass criteria) and
confirmed in production via manual chat verification plus BigQuery
`folder_latest_state` confirmation after a close-out batch.

#### Scope (deliberately narrow)

This ship is **backend / data-hygiene cleanup only**. Specifically:

- **Changed:** `folder_purpose` for admin/template folders flips to
  `unknown`. Open-items routing for those folders changes to the
  cautious unknown-envelope (`response_kind=open_items_unknown`).
  BigQuery `folder_latest_state` counts shift accordingly. Looker
  Page 1 reflects the new distribution.
- **NOT changed:** the chat summary prose for admin folders.
  `_build_folder_summary_response` still calls Gemini and renders the
  overview + key_facts the same way for admin folders as for work
  folders. `summarize 4. Bob Sheets - Spreadsheets` still produces a
  summary with extracted facts; only the trailing Open Items section
  is suppressed (because `show_open_items` is derived from
  `folder_purpose`).
- **Deferred:** any summary-prose admin behavior — warning headers,
  refusal to extract from template folders, etc. — is out of scope.
  When prioritized, that work is a separate workstream in
  `_build_folder_summary_response`, not in the classifier.

This ship is **not a search/relevance improvement.** It corrects the
classification and dashboard data, nothing else. Relevance work
(visible source grounding, retrieval quality) is a separate track.

#### What changed

Two additions to `scripts/job_intelligence.py`:

**5.10A — `_ADMIN_NAME_RE` constant** (inserted after
`_CLAIM_SUPPORTING_BUCKETS`, before the structured_summary schema
contract divider):

```python
_ADMIN_NAME_RE = re.compile(
    r"(?:templates?|forms?|boilerplate|reference)\s*$"
    r"|\bbob[\s_]+sheets?\b"
    r"|\bmerge[\s_]+form\b"
    r"|^\s*\d{4}\s+payroll\b",
    re.IGNORECASE,
)
```

Four pattern families, joined by alternation:

1. `(templates?|forms?|boilerplate|reference)\s*$` — folder name ENDS
   with the admin keyword. Catches `Merge Form Templates`,
   hypothetical `Closing Forms`, `Madison Boilerplate`. Trailing
   anchor prevents firing on `Reference Documents for Smith`.
2. `\bbob[\s_]+sheets?\b` — operator's admin compound. Compound
   match prevents firing on a customer named "Bob" or street
   "Sheets".
3. `\bmerge[\s_]+form\b` — template-aggregator compound.
4. `^\s*\d{4}\s+payroll\b` — anchored year+payroll, e.g.
   `2020 Payroll`. Bare `\bpayroll\b` was rejected; a customer at
   "Payroll Lane" or a payroll-office claim would over-fire.

Explicitly rejected from the surface:

- `^\s*\d+\.\s` numeric prefix — false-positives
  `1. Bathroom Remodel - Bolen`, a real customer work-unit folder.
- standalone `\bsheets?\b`, `\bmerge\b`, `\bpayroll\b` — over-broad.
- broad `\b(documents?|docs?|logs?)\b` patterns.

**5.10B — short-circuit in `_classify_folder_purpose`** (inserted at
the top of the function body, after the docstring, before the
existing rule cascade):

```python
if folder_name and _ADMIN_NAME_RE.search(folder_name):
    return "unknown"
```

Short-circuits to `unknown` rather than any concrete purpose.
`unknown` is the safe sink — flipping a wrong classification to
another wrong classification breaks neighbors.

#### Validation

`scripts/probe_classifier_admin_folder.py` — read-only simulation
against the 54 BQ-tracked folders. 7/7 pass criteria.

#### Post-ship verification (manual)

- `summarize 4. Bob Sheets - Spreadsheets` → renders summary with no
  trailing Open Items section. Summary prose unchanged from pre-ship.
- `what's missing for 4. Bob Sheets - Spreadsheets` → cautious
  unknown-folder envelope. Admin short-circuit fires through
  `_build_open_items_only_response`.
- Same for `Merge Form Templates`.
- `summarize KALTSAS - 68 BUSHWOOD DR, SHIRLEY` → full claim summary
  with Open Items checklist rendered. Unchanged.

#### Post-ship BigQuery state

After the close-out batch (2026-05-19 14:12 UTC) and loader run:

| folder_name | folder_purpose |
|---|---|
| 4. Bob Sheets - Spreadsheets | unknown |
| KALTSAS - 68 BUSHWOOD DR, SHIRLEY | claim_restoration |
| Merge Form Templates | unknown |

KALTSAS confirmation is non-trivial — see §5.11 for the sidecar
revert incident that delayed this ship by approximately three hours
and required regenerating the §5.7 patched sidecar.

#### Artifacts retained

- `scripts/probe_admin_folder_detection.py` — scoping probe.
- `scripts/probe_classifier_admin_folder.py` — simulation probe;
  7/7 PASS gate before code edit.
- `scripts/probe_targeted_verification.py` — 8-folder targeted probe
  used during the §5.11 sidecar restore. **Superseded** by
  `probe_full_corpus_verification.py`. Retained for forensic record
  only; reason text in the older probe overstates "held by rule"
  for folders the classifier was never going to flip.
- `scripts/probe_full_corpus_verification.py` — 54-folder
  production-classifier probe. Authoritative for predicting
  `folder_purpose` outcomes; supersedes `--diff` mode of
  `scripts/reclassify_doc_types_sidecar_only.py` (see §5.11).
- `docs/admin_rule_close_out_folders.txt` — three-folder input file
  for the close-out batch.

---

### 5.11 doc_type sidecar revert incident (2026-05-19)

**Status**: incident closed with full data recovery. Root cause not
fully determined. Watch procedure documented; recurrence will trigger
forensic investigation.

#### What happened

On 2026-05-19 at approximately 04:37 UTC, the live
`gs://madison-rag-60-rag-raw/manifests/doc_type_index.json` was found
to have content equivalent to the pre-§5.7 classification state.
KALTSAS pack-out files that the §5.7 ship had promoted to
`doc_type=estimate` reverted to `doc_type=document`. The §5.7 5A
`tax_document` false-positive fix on `KALTSAS-REPAIRS_Final Draft.pdf`
also reverted. Effect: BigQuery `folder_latest_state` rows generated
after the revert (during the §5.10 close-out batch) classified
KALTSAS as `unknown`, surfacing as a hard-rule violation in the
admin/template ship validation.

#### What was ruled out

- The §5.7 code edits in `Phase5_oneDrive/phase6_ocr_metadata.py`
  remained intact on disk (`grep` of lines 139–146 confirmed all five
  5B regex rules present: packout, packback, concln, strcleaning,
  reb-revise).
- No Madison-scheduled task wrote the sidecar. Windows
  `Get-ScheduledTask` shows only `MadisonAve_BqLoader` (BigQuery
  loader, doesn't touch sidecars). Microsoft's own OneDrive client
  tasks ran in the time window but do not write to GCS.
- The live sidecar was not byte-identical to the pre-§5.7 backup
  (`backups/doc_type_index.pre-rebuild.json`). `FC.EXE /B` reported
  divergence starting at byte 0x7FA3 — same overall structure and
  size class, different file ordering and content. The live sidecar
  was a fresh build, not a backup restore.
- The post-§5.7 patched sidecar was never separately preserved. The
  `doc_type_index.backup-20260518-170150.json` artifact named in the
  §5.7 rollback procedure is the pre-§5.7 state (saved by
  `--upload --backup-prev` BEFORE the §5.7 upload). There is no GCS
  artifact of the §5.7 patched sidecar to restore from.

#### Recovery

The §5.7 patched sidecar was regenerated from scratch by re-running
`scripts/reclassify_doc_types_sidecar_only.py` (the same tool that
did the original §5.7 ship). The regeneration uses the still-intact
`phase6_ocr_metadata.py` rules and produces a fresh patched sidecar
in ~5 seconds:

```powershell
python scripts/reclassify_doc_types_sidecar_only.py --preview
python scripts/reclassify_doc_types_sidecar_only.py --diff
python scripts/reclassify_doc_types_sidecar_only.py --upload --backup-prev
Invoke-RestMethod -Uri http://localhost:5000/api/admin/reload-index -Method POST
```

The `--backup-prev` step preserved the reverted (pre-§5.7-equivalent)
sidecar as
`gs://madison-rag-60-rag-raw/manifests/doc_type_index.backup-20260519-134225.json`
before overwriting with the patched version. Rollback to the reverted
state, if ever needed, uses that artifact.

After the upload + reload,
`scripts/probe_full_corpus_verification.py` confirmed 54/54 folders
pass acceptability rules. 6 intended improvements landed (KALTSAS,
Pack Out - Jeremy, 916950_Labon, 930262_Tanya Harris, 221 Center
Street, 9 Hamilton Place). 0 concerning changes, 0 hard-rule
violations.

The §5.10 close-out batch then ran cleanly. KALTSAS landed at
`claim_restoration` in `folder_latest_state`; Bob Sheets and Merge
Form Templates at `unknown`.

#### Tool-quality finding: `reclassify_doc_types_sidecar_only.py --diff`

During the incident triage, `--diff` predicted 8 BQ-tracked folders
would flip under the regenerated sidecar. The production classifier
produces flips on only 4 of those 8. `--diff` disagreed with
production on:

- `4. Bob Sheets - Spreadsheets` — predicted unknown →
  claim_restoration (production: unknown, because §5.10 admin
  short-circuit overrides)
- `Bolen, Barbara -appraisal - Jeremy Wolf` — predicted
  property_appraisal → claim_restoration (production: unchanged,
  because folder name lacks claim vocabulary and the appraisal bucket
  dominates)
- `Bonilla - appraisal - need to follow up - Bob` — predicted
  claim_restoration → property_appraisal (production: unchanged,
  because Rule 1 still fires on the estimate+insurance buckets)
- `Rawls, Anthony` — predicted claim_restoration → property_appraisal
  (production: unchanged, same reason)

**Do not trust `--diff` for folder-purpose prediction.** Use
`scripts/probe_full_corpus_verification.py` instead — it loads the
live sidecar, mirrors `_classify_folder_purpose` verbatim (including
the §5.10 admin rule), and produces honest per-folder verdicts.

Fixing `--diff` to match production is a future workstream; for now,
the documented best practice is: run `--preview` for the doc_type
flips inventory, run `--diff` only for the bucket transition matrix
(which it computes correctly), and use the full-corpus probe for
folder-purpose impact.

#### Watch procedure (lightweight)

A full monitoring system is out of scope. Instead,
`scripts/check_sidecar_md5.py` (to be created as part of this ship's
post-write tasks) provides a one-shot read-only check that compares
the live GCS sidecar's MD5 against an expected value captured at the
time of the regeneration. Run before any session that depends on
§5.7 doc_types being active:

```powershell
python scripts/check_sidecar_md5.py
```

The expected MD5 lives in `backups/doc_type_index.expected_md5.txt`
and was captured from the regenerated sidecar on 2026-05-19. If the
live sidecar's MD5 diverges, the script exits non-zero with the
observed vs expected values. At that point: re-read this section,
then decide whether to regenerate via
`reclassify_doc_types_sidecar_only.py` or to investigate the writer
path.

The watch is **not scheduled**. It is a manual check before
classifier-sensitive work.

#### What this incident did NOT cause

- The §5.10 admin/template ship itself was unaffected by the revert.
  `_ADMIN_NAME_RE` operates on folder names, not doc_types. Bob
  Sheets and Merge Form Templates classify as `unknown` either way;
  the §5.10 ship makes the reason explicit.
- No data loss in BigQuery or JSONL. The reverted sidecar produced
  one batch of incorrect events (the first §5.10 close-out attempt);
  those events were superseded by the second close-out batch after
  the restore. The JSONL retains both batches as forensic history.

#### Future work (deferred)

- Identify the writer that produced the reverted sidecar on
  2026-05-19 04:37 UTC. Not investigated further at the time — triage
  chose to restore + watch rather than continue forensic
  investigation.
- Fix `reclassify_doc_types_sidecar_only.py --diff` folder-purpose
  simulation to match production. Low priority; the workaround (use
  `probe_full_corpus_verification.py`) is sufficient.

---

### 5.12 OPERATIONS.md and project commit hygiene (2026-05-19)

**Status**: operational rule, effective immediately. Adopted in
response to the §5.6 documentation loss.

#### What happened

The §5.6 stub in this revision documents the loss of §5.6–§5.9
prose. Root cause: `OPERATIONS.md` was edited across multiple
sessions between 2026-05-15 and 2026-05-19 but never `git commit`ed.
When a `Filesystem:edit_file` corruption required `git checkout
docs/OPERATIONS.md` to recover, the checkout reverted to the last
committed version (2026-05-14), discarding all four sections of
intermediate prose along with the corruption.

The same `git status` check that surfaced the OPERATIONS.md problem
also revealed that the broader project state has the same pattern:

- The §5.7 5A/5B/5C edits to `Phase5_oneDrive/phase6_ocr_metadata.py`
  are in the working tree but not committed.
- The §5.10 edits to `scripts/job_intelligence.py` are in the working
  tree but not committed.
- ~28 untracked files include every probe script from §5.7 through
  §5.10, `scripts/reclassify_doc_types_sidecar_only.py`,
  `infra/bigquery/views.sql`, `scripts/Invoke-BqLoader.ps1`,
  `docs/VALIDATION_PACK.md`, the entire `backups/` directory, and
  the `docs/admin_rule_close_out_folders.txt` close-out input file.

If a `git checkout` had touched any of these files instead of just
`OPERATIONS.md`, the corresponding code or data would have been lost
the same way. The project is one accidental `git checkout` or `git
reset --hard` away from losing two weeks of work.

#### Operational rule (effective 2026-05-19)

Every workstream must commit its artifacts before the workstream is
considered closed. Specifically:

1. **At session start**, run `git status` and review what's
   uncommitted from prior sessions. Read the list before doing
   anything else. If untracked files or unstaged changes from prior
   work are present, they should either be committed or the operator
   should confirm they're intentionally in-flight before the new
   session adds more uncommitted state.

2. **At workstream close**, before claiming the workstream is
   complete:
   - Run `git status` and confirm the expected files are listed as
     modified or untracked.
   - Run `git diff` on modified files and visually confirm the diff
     matches what the workstream intended.
   - `git add` the affected files (code edits, probe scripts, docs,
     SQL, infra files).
   - `git commit` with a message that references the OPERATIONS.md
     section number (e.g. "§5.10 admin/template folder detection
     ship"). One commit per workstream is fine; multiple are fine
     too.
   - Optionally `git push` if the project's remote-tracking policy
     calls for it.

3. **For documentation specifically**: `OPERATIONS.md`, validation
   pack docs, and any other narrative documentation must be
   committed as part of the same workstream that produced them. Do
   not leave docs as uncommitted working-tree edits across sessions.

4. **If a corruption or accidental revert happens**: prefer paths
   that reconstruct from durable artifacts (code, probes, BigQuery,
   JSONL, sidecar) over paths that reconstruct from session memory.
   The 2026-05-19 incident showed that session-memory reconstruction
   risks recording inaccurate detail as authoritative.

#### Backfill scope (deferred)

This revision adds §5.10, §5.11, §5.12 and the §5.6 stub. It does
NOT backfill §5.6, §5.7, §5.8, or §5.9 from session memory. When a
future session has bandwidth, those sections can be re-authored from
the durable artifacts listed in the §5.6 stub. The artifacts
themselves are the authoritative record; the prose is a convenience
on top of them.

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
