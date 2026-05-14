-- ─────────────────────────────────────────────────────────────────────────
-- folder_intelligence v1 schema
--
-- Target:  project=madison-rag-60  dataset=folder_intelligence  region=us-central1
-- Source:  data/structured_summaries/structured_summary_events.jsonl
-- Design:  docs/bigquery_v1_design.md
--
-- All tables CREATE IF NOT EXISTS so the DDL is safe to run any number of
-- times. Loader expects these tables to exist; running this SQL is the
-- one-time setup step before the first loader invocation.
--
-- Run via:
--   bq query --use_legacy_sql=false --location=us-central1 < infra/bigquery/schema.sql
-- or paste into the BigQuery console.
-- ─────────────────────────────────────────────────────────────────────────

-- ─── Parent: one row per emitted structured_summary event ───────────────
CREATE TABLE IF NOT EXISTS `madison-rag-60.folder_intelligence.folder_intelligence_events` (
  -- Event identity
  event_id              STRING    NOT NULL OPTIONS(description="Deterministic content hash: sha256(folder_key|generated_at|response_kind|query). Stable across re-loads so MERGE is idempotent."),
  generated_at          TIMESTAMP NOT NULL OPTIONS(description="Event emission time, parsed from JSONL ISO-8601 UTC."),
  schema_version        STRING    NOT NULL OPTIONS(description="structured_summary contract version (1.0 or 1.1). Filter on this when consumers need a specific shape."),
  response_kind         STRING    NOT NULL OPTIONS(description="Enum: folder_summary | open_items_only | open_items_unknown"),

  -- Folder identity (soft key in v1: folder_key == folder_name)
  folder_key            STRING    NOT NULL OPTIONS(description="Soft folder key. In v1 this is folder_name verbatim. When upstream emits a stable folder ID, this column switches over without renaming."),
  folder_name           STRING    NOT NULL OPTIONS(description="Display name from the JSONL (may drift over time if folders are renamed)."),
  folder_purpose        STRING             OPTIONS(description="Enum: claim_restoration | property_appraisal | unknown"),
  checklist_name        STRING             OPTIONS(description="Enum: claim_default | property_default | unknown"),

  -- Query
  query                 STRING             OPTIONS(description="Originating user query text."),
  confidence            STRING             OPTIONS(description="Envelope-level confidence: high | medium | low."),
  show_open_items       BOOL               OPTIONS(description="Whether the chat client rendered the open-items checklist."),

  -- Counts
  file_count_total      INT64              OPTIONS(description="Total files in the folder."),
  file_count_in_dossier INT64              OPTIONS(description="Files included in the dossier sent to Gemini for this event."),

  -- Narrative
  overview              STRING             OPTIONS(description="Gemini-generated overview paragraph. High variance; treat as text not data."),
  observations          ARRAY<STRING>      OPTIONS(description="Free-text observations emitted by Gemini."),

  -- Promoted scalars (per analyzer's >=40% population threshold).
  -- Long form in structured_fields_long is canonical; these are convenience.
  -- Marker fix: as of the marker-persistence change, these will reflect
  -- the current parser. Backfilled rows from older runs may be NULL.
  property_address                STRING OPTIONS(description="Promoted from structured_fields; long form is canonical."),
  property_address_confidence     STRING OPTIONS(description="high|medium|low; companion to property_address."),
  property_address_source_file    STRING OPTIONS(description="Source filename for property_address."),
  contract_status                 STRING OPTIONS(description="Enum: found|needs_review|not_found. Promoted from structured_fields."),
  contract_status_confidence      STRING OPTIONS(description="Companion confidence."),
  inspection_status               STRING OPTIONS(description="Enum: found|needs_review|not_found. Promoted from structured_fields."),
  inspection_status_confidence    STRING OPTIONS(description="Companion confidence."),

  -- Inline ARRAY<STRUCT> for event-scoped variable structures.
  -- key_facts, timeline, sources stay nested because they're not joinable
  -- entities -- they're attributes of an event.
  key_facts ARRAY<STRUCT<
    label      STRING,
    value      STRING,
    confidence STRING,
    sources    ARRAY<STRING>
  >>,
  timeline ARRAY<STRUCT<
    date       STRING,
    event      STRING,
    confidence STRING,
    sources    ARRAY<STRING>
  >>,
  sources ARRAY<STRUCT<
    title     STRING,
    uri       STRING,
    subfolder STRING
  >>,

  -- Full original record (verbatim JSONL line) for replay & forensics.
  -- STRING not JSON: cheaper, queries that need to introspect can use
  -- JSON_EXTRACT/JSON_VALUE on demand.
  payload_json STRING NOT NULL OPTIONS(description="Verbatim JSONL line as a JSON string. Source of truth for replay; columns above are derived."),

  -- Load-time metadata
  loaded_at      TIMESTAMP NOT NULL OPTIONS(description="When this row was MERGE-loaded into BigQuery."),
  loader_version STRING             OPTIONS(description="Version tag of the loader script that wrote this row.")
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, response_kind
OPTIONS(
  description="One row per emitted structured_summary event from the chat pipeline. Append-only with idempotent MERGE on event_id. See docs/bigquery_v1_design.md."
);

-- ─── Child: long-form structured_fields ─────────────────────────────────
-- One row per extracted field per event. New fields require no DDL.
CREATE TABLE IF NOT EXISTS `madison-rag-60.folder_intelligence.structured_fields_long` (
  event_id     STRING    NOT NULL OPTIONS(description="Joins to folder_intelligence_events.event_id."),
  generated_at TIMESTAMP NOT NULL OPTIONS(description="Copied from parent for partition pruning."),
  folder_key   STRING    NOT NULL OPTIONS(description="Copied from parent for cluster pruning."),
  field_name   STRING    NOT NULL OPTIONS(description="e.g. appraised_value, claim_number, property_address."),
  field_value  STRING             OPTIONS(description="Raw value as emitted; cast in SELECT when typed math is needed."),
  confidence   STRING             OPTIONS(description="high|medium|low."),
  source_file  STRING             OPTIONS(description="Filename the value was extracted from.")
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, field_name
OPTIONS(
  description="Long-form structured field extractions. One row per (event, field). Only emitted when field has a non-null value -- absence of a row means the field did not populate."
);

-- ─── Child: flattened document inventory ────────────────────────────────
CREATE TABLE IF NOT EXISTS `madison-rag-60.folder_intelligence.document_inventory_items` (
  event_id     STRING    NOT NULL OPTIONS(description="Joins to folder_intelligence_events.event_id."),
  generated_at TIMESTAMP NOT NULL OPTIONS(description="Copied from parent for partition pruning."),
  folder_key   STRING    NOT NULL OPTIONS(description="Copied from parent for cluster pruning."),
  doc_name     STRING    NOT NULL OPTIONS(description="Filename as observed in the folder."),
  doc_uri      STRING             OPTIONS(description="GCS URI of the document."),
  doc_type     STRING             OPTIONS(description="Sidecar/filename tag (e.g. invoice, appraisal, document)."),
  bucket       STRING             OPTIONS(description="Normalized display bucket: appraisal, contract, invoice, report, photos, spreadsheet, other, etc."),
  is_marker    BOOL      NOT NULL OPTIONS(description="TRUE for marker files like HTML/CompanyCam reports (presence-only evidence; text not extractable). FALSE for normal text-extractable files. Default FALSE for legacy rows written before marker persistence landed.")
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, bucket
OPTIONS(
  description="One row per document observed in a folder during an event. Flattens the bucket-keyed dict from JSONL."
);

-- ─── Child: long-form open_items checklist ──────────────────────────────
CREATE TABLE IF NOT EXISTS `madison-rag-60.folder_intelligence.open_items_long` (
  event_id       STRING    NOT NULL OPTIONS(description="Joins to folder_intelligence_events.event_id."),
  generated_at   TIMESTAMP NOT NULL OPTIONS(description="Copied from parent for partition pruning."),
  folder_key     STRING    NOT NULL OPTIONS(description="Copied from parent for cluster pruning."),
  checklist_name STRING             OPTIONS(description="claim_default|property_default|unknown."),
  label          STRING    NOT NULL OPTIONS(description="Checklist line label, e.g. 'Insurance / claim document'."),
  bucket         STRING             OPTIONS(description="Document bucket this line tracks."),
  status         STRING    NOT NULL OPTIONS(description="Enum: found|needs_review|not_found."),
  strict_count   INT64              OPTIONS(description="Files strictly matching the checklist bucket."),
  total_count    INT64              OPTIONS(description="Total files in the relevant bucket.")
)
PARTITION BY DATE(generated_at)
CLUSTER BY folder_key, status
OPTIONS(
  description="One row per checklist line per event. Tracks whether expected documents were found, need review, or are missing."
);
