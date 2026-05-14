# BigQuery v1 Schema Design — folder_intelligence_events

Status: **proposal** (no code written, no tables created)
Source data: `data/structured_summaries/structured_summary_events.jsonl`
Schema contract version targeted: `structured_summary` v1.1

## Why this design

The JSONL is already an append-only event log. BigQuery v1 mirrors that
shape: one row per emitted structured_summary, plus child tables that
flatten the nested arrays. No write-time aggregation; "latest state per
folder" is computed at read time via window function so the raw history
stays the source of truth and replays are always possible.

The design has four hard constraints from the roadmap:
1. No hardcoded folders or houses anywhere in the schema.
2. New extracted fields must work without DDL changes.
3. Latest-state and historical queries must both be cheap.
4. Full JSON payload preserved for replays and debugging.

The first three drive the choice of one event table plus long-form child
tables. The fourth drives a `payload_json` STRING column on the parent.

## Tables (4)

### 1. `folder_intelligence_events` — parent event table

One row per emitted structured_summary. Append-only. Source of truth.

Includes denormalized scalar promotions for the 5 highest-population
structured_fields (per the analyzer's `--latest-run` recommendation) so
common dashboard queries don't need to join. The long-form
`structured_fields_long` table handles every other field including ones
that don't exist yet.

```sql
CREATE TABLE `madison.folder_intelligence_events` (
  -- Event identity
  event_id              STRING NOT NULL,        -- hash(folder_key,generated_at,response_kind)
                                                -- stable across re-loads
  generated_at          TIMESTAMP NOT NULL,     -- from JSONL, parsed ISO-8601 UTC
  schema_version        STRING NOT NULL,        -- structured_summary contract version ("1.1")
  response_kind         STRING NOT NULL,        -- folder_summary | open_items_only | open_items_unknown

  -- Folder identity (soft key for now -- folder_name doubles as folder_key
  -- because we have no upstream stable ID. When the sync starts emitting
  -- stable IDs, folder_key can switch over without renaming this column.)
  folder_key            STRING NOT NULL,        -- == folder_name in v1; future-proofed
  folder_name           STRING NOT NULL,
  folder_purpose        STRING,                 -- claim_restoration | property_appraisal | unknown
  checklist_name        STRING,                 -- claim_default | property_default | unknown

  -- Query that triggered the event
  query                 STRING,
  confidence            STRING,                 -- envelope-level confidence
  show_open_items       BOOL,

  -- Counts
  file_count_total      INT64,
  file_count_in_dossier INT64,

  -- Narrative
  overview              STRING,
  observations          ARRAY<STRING>,

  -- Promoted scalars (denormalized convenience; long form is canonical)
  -- Picked from analyzer's >=40% population recommendation. Companions
  -- (_source_file, _confidence) preserve provenance.
  property_address                  STRING,
  property_address_confidence       STRING,
  property_address_source_file      STRING,
  contract_status                   STRING,     -- found|needs_review|not_found
  contract_status_confidence        STRING,
  inspection_status                 STRING,
  inspection_status_confidence      STRING,
  -- folder_name and folder_purpose already live above; no duplication.

  -- Variable nested structures kept inline (event-scoped, not joinable)
  key_facts             ARRAY<STRUCT<
                          label       STRING,
                          value       STRING,
                          confidence  STRING,
                          sources     ARRAY<STRING>
                        >>,
  timeline              ARRAY<STRUCT<
                          date        STRING,    -- raw string; cast in SELECT
                          event       STRING,
                          confidence  STRING,
                          sources     ARRAY<STRING>
                        >>,
  sources               ARRAY<STRUCT<
                          title       STRING,
                          uri         STRING,
                          subfolder   STRING
                        >>,

  -- Full original record (for replay, debugging, schema-drift forensics)
  payload_json          STRING NOT NULL,        -- exact JSONL line as a string

  -- Load-time metadata
  loaded_at             TIMESTAMP NOT NULL,     -- when this row was inserted into BQ
  loader_version        STRING                  -- version of the loader script that wrote this
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, response_kind;
```

Notes:
- `event_id` is content-derived (deterministic). Re-running the loader
  on the same JSONL produces the same event_id, enabling MERGE-style
  idempotent loads.
- `payload_json` stored as STRING (not JSON type) for cost simplicity.
  Queries that need it parse on read.
- `observations` stays inline as `ARRAY<STRING>` — free-text, not
  joinable, no value in a child table.

### 2. `structured_fields_long` — flexible extraction layer

One row per extracted field per event. Adding new fields to
`_KEY_FACT_FIELD_PATTERNS` writes new rows; no DDL change required.

```sql
CREATE TABLE `madison.structured_fields_long` (
  event_id              STRING NOT NULL,    -- joins to folder_intelligence_events.event_id
  generated_at          TIMESTAMP NOT NULL, -- copied from parent for partition pruning
  folder_key            STRING NOT NULL,    -- copied for cluster pruning without join
  field_name            STRING NOT NULL,    -- e.g. "appraised_value", "claim_number"
  field_value           STRING,             -- raw value as emitted; CAST in queries
  confidence            STRING,             -- high|medium|low (or null)
  source_file           STRING              -- filename the value was extracted from
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, field_name;
```

Rows are emitted ONLY for fields where `value IS NOT NULL`. Null fields
are absent — saves storage, and "did this field populate" queries become
`WHERE field_name = X` (a row exists ⇔ field populated).

### 3. `document_inventory_items` — flattened inventory

One row per document observed in an event.

```sql
CREATE TABLE `madison.document_inventory_items` (
  event_id      STRING NOT NULL,
  generated_at  TIMESTAMP NOT NULL,
  folder_key    STRING NOT NULL,
  doc_name      STRING NOT NULL,
  doc_uri       STRING,
  doc_type      STRING,    -- sidecar/filename tag (e.g. "invoice", "appraisal")
  bucket        STRING,    -- normalized bucket (e.g. "invoice", "appraisal", "other")
  is_marker     BOOL       -- TRUE for HTML/CompanyCam markers; FALSE for text-extractable
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, bucket;
```

`is_marker` propagates the dossier's marker flag so consumers can
distinguish "we have evidence this folder exists" from "we read the
file's text." Inventory items without a parent dossier entry write
`FALSE`.

### 4. `open_items_long` — flattened checklist items

One row per checklist line per event.

```sql
CREATE TABLE `madison.open_items_long` (
  event_id        STRING NOT NULL,
  generated_at    TIMESTAMP NOT NULL,
  folder_key      STRING NOT NULL,
  checklist_name  STRING,                   -- claim_default|property_default|unknown
  label           STRING NOT NULL,          -- e.g. "Insurance / claim document"
  bucket          STRING,                   -- the bucket key this checklist item watches
  status          STRING NOT NULL,          -- found|needs_review|not_found
  strict_count    INT64,                    -- # of strict-matching files in this bucket
  total_count     INT64                     -- # of total files in this bucket
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, status;
```

## Partitioning & clustering rationale

All four tables partition by `DATE(generated_at)`. Daily partitions are
small (probably <1MB each in the near term), but the partition pruning
is automatic on time-bounded queries — the most common pattern after
`--latest-run`-style "what's current" queries.

Clustering is per-table:
- Events: by `folder_key, response_kind`. Most reads filter by folder.
- structured_fields_long: by `folder_key, field_name`. "Show me
  appraised_value for folder X" prunes hard.
- document_inventory_items: by `folder_key, bucket`. "All photos for
  folder X" prunes by both keys.
- open_items_long: by `folder_key, status`. "Folders with not_found
  insurance" prunes by both.

## Schema evolution rules

1. **structured_summary v1.1 → v1.2**: any new top-level key persists
   via `payload_json`. Add a column on `folder_intelligence_events`
   later (with a backfill) once it stabilizes.
2. **New extracted field**: writes a row to `structured_fields_long`
   with the new `field_name`. No DDL. Becomes queryable immediately.
3. **Promotion to scalar column**: when a field's population stabilizes
   above 40% (the analyzer's threshold), `ALTER TABLE ADD COLUMN <field>
   STRING` plus a one-shot backfill from `structured_fields_long`. Then
   forward-write both.
4. **Schema-breaking change**: bump `schema_version`. Consumers filter
   `WHERE schema_version IN ('1.1', '1.2')` for the shapes they handle.
5. **Folder rename upstream**: `folder_key` stays the soft key (old
   name) until we have a real stable ID. Document this gap; don't
   pretend it doesn't exist.

## Example queries

### Q1. Latest state per folder

```sql
WITH ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY folder_key
      ORDER BY generated_at DESC
    ) AS rn
  FROM `madison.folder_intelligence_events`
  WHERE response_kind = 'folder_summary'
    AND DATE(generated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
)
SELECT
  folder_key,
  folder_purpose,
  property_address,
  contract_status,
  inspection_status,
  file_count_total,
  generated_at
FROM ranked
WHERE rn = 1
ORDER BY folder_key;
```

Notes: 90-day window keeps partition scan bounded. Drop the window if
you really want all-time, but you'll pay for it.

### Q2. Folders with open checklist items

```sql
WITH latest_event_per_folder AS (
  SELECT
    folder_key,
    MAX(generated_at) AS latest_at
  FROM `madison.folder_intelligence_events`
  WHERE response_kind IN ('folder_summary', 'open_items_only')
  GROUP BY folder_key
),
latest_open_items AS (
  SELECT
    o.folder_key,
    o.label,
    o.status,
    o.strict_count,
    o.total_count
  FROM `madison.open_items_long` o
  JOIN latest_event_per_folder l
    ON o.folder_key = l.folder_key
   AND o.generated_at = l.latest_at
)
SELECT
  folder_key,
  COUNTIF(status = 'not_found')    AS not_found_count,
  COUNTIF(status = 'needs_review') AS needs_review_count,
  ARRAY_AGG(STRUCT(label, status) ORDER BY label) AS items
FROM latest_open_items
GROUP BY folder_key
HAVING not_found_count > 0 OR needs_review_count > 0
ORDER BY not_found_count DESC, folder_key;
```

### Q3. Folder counts by purpose (latest snapshot)

```sql
WITH ranked AS (
  SELECT
    folder_key,
    folder_purpose,
    ROW_NUMBER() OVER (PARTITION BY folder_key ORDER BY generated_at DESC) AS rn
  FROM `madison.folder_intelligence_events`
)
SELECT
  folder_purpose,
  COUNT(*) AS folder_count
FROM ranked
WHERE rn = 1
GROUP BY folder_purpose
ORDER BY folder_count DESC;
```

### Q4. Every extracted field across all folders

```sql
WITH latest_per_folder AS (
  SELECT
    folder_key,
    MAX(generated_at) AS latest_at
  FROM `madison.folder_intelligence_events`
  WHERE response_kind = 'folder_summary'
  GROUP BY folder_key
)
SELECT
  sf.folder_key,
  sf.field_name,
  sf.field_value,
  sf.confidence,
  sf.source_file
FROM `madison.structured_fields_long` sf
JOIN latest_per_folder l
  ON sf.folder_key = l.folder_key
 AND sf.generated_at = l.latest_at
ORDER BY sf.folder_key, sf.field_name;
```

### Q5. Folders whose status changed over time

Shows the last two events per folder where any status field flipped.
Useful for "what changed since last time we looked."

```sql
WITH events_per_folder AS (
  SELECT
    folder_key,
    generated_at,
    contract_status,
    inspection_status,
    LAG(contract_status)   OVER (PARTITION BY folder_key ORDER BY generated_at) AS prev_contract_status,
    LAG(inspection_status) OVER (PARTITION BY folder_key ORDER BY generated_at) AS prev_inspection_status
  FROM `madison.folder_intelligence_events`
  WHERE response_kind = 'folder_summary'
    AND DATE(generated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
)
SELECT
  folder_key,
  generated_at,
  contract_status,
  prev_contract_status,
  inspection_status,
  prev_inspection_status
FROM events_per_folder
WHERE (contract_status   IS DISTINCT FROM prev_contract_status)
   OR (inspection_status IS DISTINCT FROM prev_inspection_status)
ORDER BY folder_key, generated_at DESC;
```

### Q6. Field population over time (parser drift detection)

Useful for catching the kind of regression we just diagnosed
(estimate_total going from 0% to 9.5% after the parser fix).

```sql
SELECT
  DATE(generated_at)       AS day,
  field_name,
  COUNT(DISTINCT event_id) AS rows_with_value
FROM `madison.structured_fields_long`
WHERE DATE(generated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
GROUP BY day, field_name
ORDER BY field_name, day;
```

## What's explicitly NOT in v1

- **No materialized `folder_latest_state` table.** v1 computes latest via
  ROW_NUMBER. Add a materialized view only after query volume justifies
  it. v1 must be debuggable from raw events.
- **No promoted columns below the 40% population line.** Those live in
  `structured_fields_long` exclusively. Promotion is a deliberate later
  step with backfill.
- **No `key_facts_long` table.** Gemini's free-text key_facts vary too
  much to be worth flattening — they're not a stable schema and don't
  warrant their own table until we know how dashboards want to consume
  them. Stays as `ARRAY<STRUCT>` on the events table.
- **No views.** No `folder_latest_v1` view yet. The example queries
  above are what consumers write. Add views after a few real consumers
  exist and we see which patterns get repeated.
- **No CDC / change-tracking infrastructure.** The append-only model is
  the change log. Q5 above is the change-detection query; no triggers,
  no extra tables.
- **No PII or auth model decisions yet.** This proposal assumes the
  bucket has the same access control as the rest of the project. If
  BigQuery access needs differ from GCS access, that's a separate scope.

## Open questions before writing the loader

1. **Dataset name and region**: `madison` in `us-central1`? Confirm.
2. **event_id derivation**: I proposed `hash(folder_key, generated_at,
   response_kind)`. Three records emitted in the same second with the
   same kind+folder would collide — possible if the user spams the same
   query. Should we include `query` in the hash? My take: yes, include
   `query` so identical re-asks produce the same event_id (idempotent),
   but distinct queries produce distinct event_ids.
3. **Loader idempotency**: MERGE on `event_id`? Or always INSERT and
   trust the hash for de-dup? MERGE is cleaner; INSERT is faster. I
   lean MERGE for v1 since correctness > throughput at this scale.
4. **Backfill plan**: load the existing 122 JSONL records first as the
   v1 seed, then start incremental loading. Single shot, no special
   logic needed.
5. **Loader trigger**: cron job that reads the JSONL tail since last
   load? Or one-shot daily? I'd say daily one-shot is plenty for now
   given record volume.

These need to be answered before writing the loader. Schema design
itself is complete.
