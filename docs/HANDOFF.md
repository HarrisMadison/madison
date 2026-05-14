# Folder Intelligence — Handoff Summary

**Read this first.** It's the entry point to the project's documentation.

**Status as of 2026-05-14**: v1 pipeline is live, BigQuery is loaded with
143 events, no code changes pending. Project is in validation/usage mode.

---

## 1. What this project does

Madison Ave Construction has ~2,346 property/claim folders living in
OneDrive, each containing a mix of PDFs, DOCXs, spreadsheets, photos,
and CompanyCam HTML reports. Operators ask questions like "summarize
27 Manor Drive" or "what's missing from the Tanya Harris claim?" in a
chat interface (Bob).

The folder intelligence pipeline classifies each folder by purpose
(claim_restoration / property_appraisal / unknown), extracts structured
facts from the documents, runs a purpose-aware checklist of expected
documents, and persists the result to JSONL and then BigQuery so the
data is queryable beyond the chat session.

---

## 2. Architecture (one diagram)

```
  Chat (Bob UI, http://localhost:5000)
        │
        ▼
  job_intelligence.py
    ├── classifier (folder_purpose: claim / property / unknown)
    ├── dossier builder (fetches snippets, marker-aware)
    ├── Gemini Flash (synthesis: overview + key_facts + timeline)
    ├── checklist (open_items by purpose)
    └── normalizer (schema 1.1 contract, derives structured_fields)
        │
        ▼
  data/structured_summaries/structured_summary_events.jsonl
        │  (append-only event log; source of truth)
        ▼
  scripts/bq_loader.py            (manual invocation)
        │
        ▼
  BigQuery: madison-rag-60.folder_intelligence (us-central1)
    ├── folder_intelligence_events    (parent; one row per event)
    ├── structured_fields_long        (one row per extracted field)
    ├── document_inventory_items      (one row per file; is_marker flag)
    └── open_items_long               (one row per checklist line)
```

**Key contract**: JSONL is source of truth. BigQuery is derived. The
loader can be re-run any time without duplicating data.

---

## 3. What was built (chronological highlights)

The path from "structured_summary is leaking metadata" to "BigQuery v1
ships" took multiple iterations, each verified before the next. The
discipline of verifying every scope is why the data going into BigQuery
is now trustworthy.

1. **Schema 1.1 contract** — locked down what a structured_summary
   record looks like: 19 required top-level keys, deterministic
   normalization through `_normalize_structured_summary`.

2. **Classifier rule expansion** — narrowed false-positive P&L
   matches, added settlement/AOB/Encircle/IICRC/Xactimate doc types,
   carrier names. Sidecar reclassification moved 1,060 files from
   generic `document` to specific types.

3. **Folder-purpose classifier** — name signals + supporting bucket
   evidence. Word-boundary guards prevent over-firing on folders like
   "Waterview Drive" or "Smokerise Trail."

4. **Marker file support** — `.html` (CompanyCam) added to enumeration
   as marker files. Marker-only folders enter the pipeline as
   claim_restoration without inventing facts from un-readable HTML.

5. **estimate_total parser fix** — caught 21 emitted labels the old
   parser missed (`Asbestos Abatement Estimate Total`, `Total Repair
   Estimate (RCV)`, etc.). Field went 0% → ~10% on latest-run view.

6. **Analyzer time filter** — `--since` and `--latest-run` so parser
   improvements can be measured against current-parser behavior, not
   diluted by historical records.

7. **BigQuery v1 design + DDL** — 4 tables, append-only, partitioned
   by date and clustered by folder_key. Documented in
   `docs/bigquery_v1_design.md`.

8. **Marker persistence fix** — closed two leaks (`_norm_inventory`
   stripped `is_marker`; open_items path never set it). Now 100% of
   inventory items carry the flag. The BigQuery `is_marker` column is
   meaningful.

9. **BigQuery loader** — idempotent (deterministic event_id), handles
   schema 1.0 legacy records, fast-fails on auth/dataset issues with
   actionable error messages. 143 events live in BigQuery.

10. **Smoke queries** — 5 read-only validation queries covering
    latest-state, purpose counts, open items, field population, and
    marker counts.

11. **Operations runbook** — `docs/OPERATIONS.md` covers commands,
    success criteria, failure modes, and the deferred list.

---

## 4. Files that matter

### Documentation (read in this order)

| File | Read when |
|---|---|
| `docs/HANDOFF.md` | First time orienting (this file) |
| `docs/OPERATIONS.md` | Running the pipeline or debugging |
| `docs/bigquery_v1_design.md` | Designing schema changes |
| `docs/TROUBLESHOOTING.md` | Older issues, pre-v1 |
| `docs/LESSONS_LEARNED.md` | Historical context |

### Code (entry points)

| File | Role |
|---|---|
| `scripts/job_intelligence.py` | Main pipeline (~3,300 lines) |
| `scripts/phase4_routes.py` | Flask routes for chat |
| `scripts/templates/bob_chat.html` | Chat UI |
| `scripts/local_index.py` | Folder/file index, marker-aware |
| `scripts/bq_loader.py` | JSONL → BigQuery (idempotent) |
| `scripts/bq_smoke_queries.py` | Read-only BigQuery validation |
| `scripts/analyze_structured_summaries.py` | JSONL analyzer |
| `scripts/generate_structured_summary_samples.py` | Batch sample driver |
| `Phase5_oneDrive/phase6_ocr_metadata.py` | Sidecar classifier rules |

### Infrastructure

| File | Role |
|---|---|
| `infra/bigquery/schema.sql` | DDL for the 4 BigQuery tables |

### Data

| File | Role |
|---|---|
| `data/structured_summaries/structured_summary_events.jsonl` | Source of truth |
| `selected_folders.txt` | Curated 13 folders for batch sampling |

---

## 5. Commands cheat sheet

All commands from repo root:
`C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve`

```powershell
# Generate fresh samples (8-10 min for 13 folders)
python scripts/generate_structured_summary_samples.py `
  --folders-file selected_folders.txt

# Check what just got produced
python scripts/analyze_structured_summaries.py --latest-run

# Load into BigQuery (idempotent; safe to re-run)
python scripts/bq_loader.py

# Validate BigQuery state
python scripts/bq_smoke_queries.py
```

For failure modes and recovery, see `docs/OPERATIONS.md` section 4.

---

## 6. Current row counts (v1 baseline, 2026-05-14)

```
JSONL rows:                  143
folder_intelligence_events:  143
structured_fields_long:      596
document_inventory_items:   1242
open_items_long:             438
```

Distinct folders represented: 13 (curated sample).

Growth is expected; the ratios should stay roughly proportional. If
`document_inventory_items` jumps far faster than
`folder_intelligence_events`, a folder with hundreds of files entered
the corpus — notable, not a bug.

---

## 7. Where to verify things are working

Run all four in sequence; if any output looks wrong, see
`docs/OPERATIONS.md` section 3 for what success looks like and section
4 for failure-mode actions.

| Check | Command | What to look for |
|---|---|---|
| Chat is up | Browse http://localhost:5000 | Bob UI renders, accepts a query |
| JSONL is growing | `(Get-Content data/structured_summaries/structured_summary_events.jsonl).Count` | Row count rises after chat use |
| Schema contract holds | `python scripts/analyze_structured_summaries.py --latest-run` | `schema_version=1.1` rows, no malformed warnings |
| Marker fix works | `python scripts/bq_smoke_queries.py --query 5` | Michelle Berry, Trish Wallace show `marker_docs=1` |
| BigQuery is current | `python scripts/bq_smoke_queries.py --query 1` | Latest folder events appear; `generated_at` is recent |
| Loader is idempotent | Run `python scripts/bq_loader.py` twice | Row counts unchanged on the second run |

---

## 8. Deferred items (ordered by likely value)

None of these are blocking. Each is genuinely optional and was
deferred by choice, not by accident.

### 8.1 Dashboard layer (Looker Studio)
**What**: Plug Looker Studio directly into the 4 BigQuery tables. Give
non-operator stakeholders a folder-state view without a custom UI.
**Effort**: ~half a day.
**Trigger**: someone asks "can I see folder status without using chat?"

### 8.2 HTML / CompanyCam extraction
**What**: Read text from `report.html` files (CompanyCam exports) so
marker-only folders produce real `key_facts`. The instrumentation to
measure impact (`is_marker` column in BigQuery) is already live.
**Effort**: 1-2 days. Needs sample HTML to design extractors.
**Trigger**: production corpus reveals significant marker-only folders,
or a user asks "what's in the CompanyCam report for X."

### 8.3 Appraisal dossier tier-bump
**What**: Bump appraisal-bucket files to a higher char budget
regardless of rank, so `appraised_value` doesn't get truncated when
the appraisal isn't in the top 5 docs.
**Effort**: ~1 hour. Localized change in `_build_folder_dossier`.
**Trigger**: `appraised_value` field population matters and is stuck
at corpus reality due to truncation (currently ~8%).

### 8.4 Loader trigger / schedule
**What**: Automate the loader instead of running it manually. Windows
Task Scheduler is the cheapest path; Cloud Run is the cleanest.
**Effort**: ~1 hour for Task Scheduler.
**Trigger**: forgetting to run the loader becomes a real annoyance, or
chat-batch volume outpaces operator attention.

### 8.5 MERGE refinement (parent table)
**What**: Make the parent MERGE conditional on actual content change
so re-runs report 0 affected when nothing changed. Currently rewrites
`loaded_at` every run.
**Effort**: ~15 minutes.
**Trigger**: table volume grows enough that the wasted DML matters.

---

## 9. What I'd watch for over the next few weeks of usage

Patterns worth noting during validation mode. None require code
changes immediately; they help inform which deferred item to pick up.

- **How often do new folders appear in the JSONL?** Tells us how much
  corpus growth happens via normal chat use vs. curated batches.

- **Which structured_fields stay sparse?** If `property_address`
  population drops below 30% over a real-usage sample, something
  changed upstream (Gemini variance, parser regression, or corpus
  composition shift). The analyzer's `--since` lets you bound the
  comparison.

- **How many real folders end up as `folder_purpose=unknown`?** If
  this is a small fraction, the classifier is working. If it's the
  majority, the supporting-bucket rule needs widening.

- **Do marker-only folders accumulate?** If many production folders
  are HTML-only, that elevates the HTML extraction deferred item.

- **Do operators run the loader, or does it drift?** If the loader
  goes more than a few days between runs, scheduling becomes worth
  building.

---

## 10. Project principles (worth preserving)

These came up repeatedly during the work. They're not rules, but they
shaped every decision and are worth carrying forward.

1. **No hardcoded folder names anywhere.** All rules are generic. The
   curated 13 folders are test cases, not exceptions. Future folders
   benefit from the same logic.

2. **Verify every scope before the next.** Every fix had a measurement
   step — usually the analyzer or smoke queries — that confirmed the
   change did what was claimed before moving on. This is why the data
   is trustworthy.

3. **JSONL is append-only.** Historical records are forensic
   artifacts. Parser improvements don't rewrite history; they show up
   in new records and the latest-run analyzer view.

4. **Honest about ceilings.** When a field is at corpus reality
   (`invoice_total` ~5% because that's how often invoices exist),
   don't chase the percentage. When a field has a real parser gap
   (`estimate_total` was at 0% because patterns missed real labels),
   fix the parser.

5. **Smallest correct change.** Most scopes ended up being 1-3 file
   edits. The marker-persistence fix was 2 small additions. Big
   rewrites were avoided unless the architecture demanded them.

6. **Defer concretely.** The deferred items list above isn't a wish
   list — each item has a trigger that says when to revisit. "Maybe
   we'll need this someday" gets dropped; "revisit if X happens" gets
   kept.

---

## 11. Contact / continuation

When picking up the project later:

1. Re-read `docs/HANDOFF.md` (this file). 5 minutes.
2. Skim `docs/OPERATIONS.md` for current commands.
3. Run the four verification commands in section 7.
4. If something's off, the analyzer's `--latest-run` view + the smoke
   queries will localize the issue fast.
5. If everything's clean, pick a deferred item from section 8 based on
   what the corpus is actually showing you, not what's interesting.

The system is in a known-good state. Resume from here.
