"""BigQuery v1 loader for folder_intelligence events.

Reads:
    data/structured_summaries/structured_summary_events.jsonl

Writes (via MERGE / DELETE+INSERT, idempotent):
    madison-rag-60.folder_intelligence.folder_intelligence_events
    madison-rag-60.folder_intelligence.structured_fields_long
    madison-rag-60.folder_intelligence.document_inventory_items
    madison-rag-60.folder_intelligence.open_items_long

Usage:
    python scripts/bq_loader.py             # load whole JSONL
    python scripts/bq_loader.py --dry-run   # parse & shape rows, no writes
    python scripts/bq_loader.py --since 2026-05-14T00:00:00Z  # load only newer

Design contract (see docs/bigquery_v1_design.md):
    - event_id is deterministic: sha256(folder_key|generated_at|response_kind|query)[:32]
    - same JSONL line -> same event_id -> idempotent re-load
    - child rows for an event are rewritten on each load of that event
    - schema 1.0 records (no structured_fields) load with empty long-form rows
    - malformed JSONL lines are skipped with a warning, not fatal
    - auth failure is fatal: stop and print the exact error

Authentication:
    Uses Google Application Default Credentials (ADC). If the user has
    `gcloud auth application-default login` set up, that works. If a
    service-account key is set via GOOGLE_APPLICATION_CREDENTIALS, that
    works too. The loader does NOT manage credentials directly.

Pre-flight: the four target tables MUST exist. Run infra/bigquery/schema.sql
once before the first loader run.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ─── Configuration ─────────────────────────────────────────────────────────
PROJECT = "madison-rag-60"
DATASET = "folder_intelligence"
LOCATION = "us-central1"

TABLE_EVENTS = f"{PROJECT}.{DATASET}.folder_intelligence_events"
TABLE_FIELDS = f"{PROJECT}.{DATASET}.structured_fields_long"
TABLE_INVENTORY = f"{PROJECT}.{DATASET}.document_inventory_items"
TABLE_OPEN_ITEMS = f"{PROJECT}.{DATASET}.open_items_long"

# Staging tables live in the same dataset; CREATE OR REPLACE each run.
STAGING_SUFFIX = "_staging"

LOADER_VERSION = "bq_loader_v1.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = REPO_ROOT / "data" / "structured_summaries" / "structured_summary_events.jsonl"


# ─── Auth & client init ────────────────────────────────────────────────────
def _init_bigquery_client():
    """Initialize a BigQuery client using Application Default Credentials.

    If auth fails, print the exact error and exit non-zero per project
    contract (user instruction: "stop and show the exact auth error").
    """
    try:
        from google.cloud import bigquery
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError as e:
        print(f"ERROR: google-cloud-bigquery not installed: {e}", file=sys.stderr)
        print("       Install with: pip install google-cloud-bigquery", file=sys.stderr)
        sys.exit(2)

    try:
        client = bigquery.Client(project=PROJECT, location=LOCATION)
        # Force a tiny round-trip to surface auth errors here, not mid-load.
        # Listing one dataset is the cheapest authenticated call.
        _ = list(client.list_datasets(max_results=1))
        return client
    except DefaultCredentialsError as e:
        print("ERROR: Google Application Default Credentials not found.", file=sys.stderr)
        print(f"       Exact error: {e}", file=sys.stderr)
        print("       Fix one of:", file=sys.stderr)
        print("         (a) run: gcloud auth application-default login", file=sys.stderr)
        print("         (b) set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"ERROR: BigQuery client initialization failed: {e}", file=sys.stderr)
        print(f"       (type: {type(e).__name__})", file=sys.stderr)
        sys.exit(2)


# ─── JSONL loading & filtering ─────────────────────────────────────────────
def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse the JSONL generated_at value to a tz-aware UTC datetime.

    Matches the analyzer's parsing rules so filtering is consistent.
    Returns None on failure (callers decide whether that's fatal).
    """
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _load_jsonl(path: Path, since: Optional[datetime]) -> Tuple[List[Dict], int, int]:
    """Load JSONL records. Returns (records, malformed_count, skipped_by_since)."""
    if not path.exists():
        print(f"ERROR: JSONL file not found: {path}", file=sys.stderr)
        sys.exit(2)

    records: List[Dict] = []
    malformed = 0
    skipped = 0
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                malformed += 1
                print(f"  WARN: line {lineno} malformed JSON: {e}", file=sys.stderr)
                continue
            if not isinstance(rec, dict):
                malformed += 1
                continue
            # Preserve the verbatim line for payload_json. Each line ends with \n
            # in the source; we strip and re-emit JSON, but to be safe we store
            # the original stripped line.
            rec["__verbatim_line"] = line
            if since is not None:
                ts = _parse_ts(rec.get("generated_at"))
                if ts is None or ts < since:
                    skipped += 1
                    continue
            records.append(rec)
    return records, malformed, skipped


# ─── Event-ID derivation ───────────────────────────────────────────────────
def _event_id(rec: Dict) -> str:
    """Deterministic event_id: sha256 of (folder_key|generated_at|response_kind|query).

    Truncated to 32 chars for readability; collision risk is negligible at
    100x current corpus scale (sha256 collision space is enormous).
    Including query in the hash means re-asking the same question to the
    same folder at a different time produces a different event_id (correct;
    Gemini emits new content). Re-running the loader on the same JSONL
    produces the same event_id (correct; MERGE deduplicates).
    """
    folder_key = (rec.get("folder_name") or "").strip()
    generated_at = (rec.get("generated_at") or "").strip()
    response_kind = (rec.get("response_kind") or "").strip()
    query = (rec.get("query") or "").strip()
    payload = f"{folder_key}|{generated_at}|{response_kind}|{query}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ─── Row shaping ───────────────────────────────────────────────────────────
def _coerce_str(v: Any) -> Optional[str]:
    """Coerce to STRING or None. Empty string -> None (BQ semantics)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v if v.strip() else None
    return str(v)


def _coerce_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_bool(v: Any) -> bool:
    """Strict bool coercion. Anything truthy except 'false'/'0' -> True."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "none", "null")
    return bool(v)


def _shape_event_row(rec: Dict, loaded_at_iso: str) -> Dict:
    """Map a JSONL record to a folder_intelligence_events row dict.

    Returns a dict matching the table schema (one row). Uses ISO strings
    for TIMESTAMP fields; load_table_from_json parses them.
    """
    event_id = _event_id(rec)
    folder_name = _coerce_str(rec.get("folder_name")) or ""
    folder_key = folder_name  # v1 soft key

    # Promoted scalars from structured_fields (where present).
    sf = rec.get("structured_fields") or {}

    def _promoted(field: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (value, confidence, source_file) for a structured_field."""
        entry = sf.get(field) if isinstance(sf, dict) else None
        if not isinstance(entry, dict):
            return None, None, None
        return (
            _coerce_str(entry.get("value")),
            _coerce_str(entry.get("confidence")),
            _coerce_str(entry.get("source_file")),
        )

    prop_val, prop_conf, prop_src = _promoted("property_address")
    contract_val, contract_conf, _ = _promoted("contract_status")
    inspection_val, inspection_conf, _ = _promoted("inspection_status")

    # ARRAY<STRUCT> for key_facts / timeline / sources.
    # The JSON loader expects these as Python lists of dicts with the
    # struct field names exactly matching the schema.
    key_facts = []
    for kf in (rec.get("key_facts") or []):
        if not isinstance(kf, dict):
            continue
        key_facts.append({
            "label":      _coerce_str(kf.get("label")),
            "value":      _coerce_str(kf.get("value")),
            "confidence": _coerce_str(kf.get("confidence")),
            "sources":    [_coerce_str(s) for s in (kf.get("sources") or []) if _coerce_str(s)],
        })

    timeline = []
    for t in (rec.get("timeline") or []):
        if not isinstance(t, dict):
            continue
        timeline.append({
            "date":       _coerce_str(t.get("date")),
            "event":      _coerce_str(t.get("event")),
            "confidence": _coerce_str(t.get("confidence")),
            "sources":    [_coerce_str(s) for s in (t.get("sources") or []) if _coerce_str(s)],
        })

    sources = []
    for s in (rec.get("sources") or []):
        if not isinstance(s, dict):
            continue
        sources.append({
            "title":     _coerce_str(s.get("title")),
            "uri":       _coerce_str(s.get("uri")),
            "subfolder": _coerce_str(s.get("subfolder")),
        })

    # Observations: ARRAY<STRING>
    observations = [_coerce_str(o) for o in (rec.get("observations") or []) if _coerce_str(o)]

    # payload_json: the verbatim JSONL line. Drop the marker we added.
    payload = dict(rec)
    payload.pop("__verbatim_line", None)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    return {
        "event_id":              event_id,
        "generated_at":          _coerce_str(rec.get("generated_at")),
        "schema_version":        _coerce_str(rec.get("schema_version")) or "unknown",
        "response_kind":         _coerce_str(rec.get("response_kind")) or "unknown",
        "folder_key":            folder_key,
        "folder_name":           folder_name,
        "folder_purpose":        _coerce_str(rec.get("folder_purpose")),
        "checklist_name":        _coerce_str(rec.get("checklist_name")),
        "query":                 _coerce_str(rec.get("query")),
        "confidence":            _coerce_str(rec.get("confidence")),
        "show_open_items":       _coerce_bool(rec.get("show_open_items")),
        "file_count_total":      _coerce_int(rec.get("file_count_total")),
        "file_count_in_dossier": _coerce_int(rec.get("file_count_in_dossier")),
        "overview":              _coerce_str(rec.get("overview")),
        "observations":          observations,
        "property_address":               prop_val,
        "property_address_confidence":    prop_conf,
        "property_address_source_file":   prop_src,
        "contract_status":                contract_val,
        "contract_status_confidence":     contract_conf,
        "inspection_status":              inspection_val,
        "inspection_status_confidence":   inspection_conf,
        "key_facts":             key_facts,
        "timeline":              timeline,
        "sources":               sources,
        "payload_json":          payload_json,
        "loaded_at":             loaded_at_iso,
        "loader_version":        LOADER_VERSION,
    }


def _shape_structured_field_rows(rec: Dict, event_id: str) -> List[Dict]:
    """Emit one row per non-null structured_field. Skips fields with no value."""
    out: List[Dict] = []
    sf = rec.get("structured_fields") or {}
    if not isinstance(sf, dict):
        return out
    folder_key = _coerce_str(rec.get("folder_name")) or ""
    generated_at = _coerce_str(rec.get("generated_at"))
    for field_name, entry in sf.items():
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if value is None or value == "":
            continue  # skip empty -- absence of row = field didn't populate
        out.append({
            "event_id":     event_id,
            "generated_at": generated_at,
            "folder_key":   folder_key,
            "field_name":   field_name,
            "field_value":  _coerce_str(value),
            "confidence":   _coerce_str(entry.get("confidence")),
            "source_file":  _coerce_str(entry.get("source_file")),
        })
    return out


def _shape_inventory_rows(rec: Dict, event_id: str) -> List[Dict]:
    """One row per document in document_inventory. is_marker default False for legacy."""
    out: List[Dict] = []
    inv = rec.get("document_inventory") or {}
    if not isinstance(inv, dict):
        return out
    folder_key = _coerce_str(rec.get("folder_name")) or ""
    generated_at = _coerce_str(rec.get("generated_at"))
    for bucket, items in inv.items():
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            doc_name = _coerce_str(it.get("name"))
            if not doc_name:
                continue
            out.append({
                "event_id":     event_id,
                "generated_at": generated_at,
                "folder_key":   folder_key,
                "doc_name":     doc_name,
                "doc_uri":      _coerce_str(it.get("uri")),
                "doc_type":     _coerce_str(it.get("doc_type")),
                "bucket":       _coerce_str(it.get("bucket") or bucket),
                # Legacy rows (pre-marker-persistence) lack the key. Default False.
                "is_marker":    _coerce_bool(it.get("is_marker", False)),
            })
    return out


def _shape_open_items_rows(rec: Dict, event_id: str) -> List[Dict]:
    """One row per checklist line."""
    out: List[Dict] = []
    folder_key = _coerce_str(rec.get("folder_name")) or ""
    generated_at = _coerce_str(rec.get("generated_at"))
    checklist_name = _coerce_str(rec.get("checklist_name"))
    for oi in (rec.get("open_items") or []):
        if not isinstance(oi, dict):
            continue
        label = _coerce_str(oi.get("label"))
        status = _coerce_str(oi.get("status"))
        if not label or not status:
            continue
        out.append({
            "event_id":       event_id,
            "generated_at":   generated_at,
            "folder_key":     folder_key,
            "checklist_name": checklist_name,
            "label":          label,
            "bucket":         _coerce_str(oi.get("bucket")),
            "status":         status,
            "strict_count":   _coerce_int(oi.get("strict_count")),
            "total_count":    _coerce_int(oi.get("total_count")),
        })
    return out


# ─── BigQuery operations ───────────────────────────────────────────────────
def _dataset_exists(client) -> bool:
    """Verify the parent dataset exists. Returns True/False.

    CREATE TABLE doesn't create the dataset; that's a separate step. So we
    check the dataset before checking tables to produce a clearer error if
    the user skipped `bq mk --dataset`.
    """
    from google.cloud.exceptions import NotFound
    try:
        client.get_dataset(f"{PROJECT}.{DATASET}")
        return True
    except NotFound:
        return False


def _tables_exist(client) -> List[str]:
    """Verify all four target tables exist. Returns list of missing tables."""
    from google.cloud import bigquery
    from google.cloud.exceptions import NotFound
    missing = []
    for tbl in (TABLE_EVENTS, TABLE_FIELDS, TABLE_INVENTORY, TABLE_OPEN_ITEMS):
        try:
            client.get_table(tbl)
        except NotFound:
            missing.append(tbl)
    return missing


def _load_to_staging(client, table_target: str, rows: List[Dict], schema) -> str:
    """Load rows into a per-run staging table. Returns staging table name.

    Uses load_table_from_json with WRITE_TRUNCATE so each run starts fresh.
    """
    from google.cloud import bigquery
    staging = f"{table_target}{STAGING_SUFFIX}"
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    job = client.load_table_from_json(rows, staging, job_config=job_config)
    job.result()  # wait; raises on failure
    return staging


def _merge_events(client, staging: str) -> int:
    """MERGE staging -> events on event_id. Returns rows affected (best effort)."""
    sql = f"""
    MERGE `{TABLE_EVENTS}` T
    USING `{staging}` S
      ON T.event_id = S.event_id
    WHEN MATCHED THEN UPDATE SET
      generated_at = S.generated_at,
      schema_version = S.schema_version,
      response_kind = S.response_kind,
      folder_key = S.folder_key,
      folder_name = S.folder_name,
      folder_purpose = S.folder_purpose,
      checklist_name = S.checklist_name,
      query = S.query,
      confidence = S.confidence,
      show_open_items = S.show_open_items,
      file_count_total = S.file_count_total,
      file_count_in_dossier = S.file_count_in_dossier,
      overview = S.overview,
      observations = S.observations,
      property_address = S.property_address,
      property_address_confidence = S.property_address_confidence,
      property_address_source_file = S.property_address_source_file,
      contract_status = S.contract_status,
      contract_status_confidence = S.contract_status_confidence,
      inspection_status = S.inspection_status,
      inspection_status_confidence = S.inspection_status_confidence,
      key_facts = S.key_facts,
      timeline = S.timeline,
      sources = S.sources,
      payload_json = S.payload_json,
      loaded_at = S.loaded_at,
      loader_version = S.loader_version
    WHEN NOT MATCHED THEN INSERT ROW
    """
    job = client.query(sql)
    job.result()
    return job.num_dml_affected_rows or 0


def _replace_child_rows(client, target: str, staging: str) -> int:
    """For child tables: delete rows whose event_id appears in staging, then
    insert all staging rows. This is a 'replace per event' pattern -- safe
    for re-runs because re-loading the same event rewrites its child rows.
    Returns inserted-row count.
    """
    delete_sql = f"""
    DELETE FROM `{target}`
    WHERE event_id IN (SELECT DISTINCT event_id FROM `{staging}`)
    """
    client.query(delete_sql).result()
    insert_sql = f"INSERT INTO `{target}` SELECT * FROM `{staging}`"
    job = client.query(insert_sql)
    job.result()
    return job.num_dml_affected_rows or 0


# ─── BigQuery schemas (Python objects matching schema.sql) ─────────────────
def _build_schemas():
    """Return a dict of table_target -> List[SchemaField] for staging loads."""
    from google.cloud import bigquery as bq
    events_schema = [
        bq.SchemaField("event_id", "STRING", mode="REQUIRED"),
        bq.SchemaField("generated_at", "TIMESTAMP", mode="REQUIRED"),
        bq.SchemaField("schema_version", "STRING", mode="REQUIRED"),
        bq.SchemaField("response_kind", "STRING", mode="REQUIRED"),
        bq.SchemaField("folder_key", "STRING", mode="REQUIRED"),
        bq.SchemaField("folder_name", "STRING", mode="REQUIRED"),
        bq.SchemaField("folder_purpose", "STRING"),
        bq.SchemaField("checklist_name", "STRING"),
        bq.SchemaField("query", "STRING"),
        bq.SchemaField("confidence", "STRING"),
        bq.SchemaField("show_open_items", "BOOL"),
        bq.SchemaField("file_count_total", "INT64"),
        bq.SchemaField("file_count_in_dossier", "INT64"),
        bq.SchemaField("overview", "STRING"),
        bq.SchemaField("observations", "STRING", mode="REPEATED"),
        bq.SchemaField("property_address", "STRING"),
        bq.SchemaField("property_address_confidence", "STRING"),
        bq.SchemaField("property_address_source_file", "STRING"),
        bq.SchemaField("contract_status", "STRING"),
        bq.SchemaField("contract_status_confidence", "STRING"),
        bq.SchemaField("inspection_status", "STRING"),
        bq.SchemaField("inspection_status_confidence", "STRING"),
        bq.SchemaField("key_facts", "RECORD", mode="REPEATED", fields=[
            bq.SchemaField("label", "STRING"),
            bq.SchemaField("value", "STRING"),
            bq.SchemaField("confidence", "STRING"),
            bq.SchemaField("sources", "STRING", mode="REPEATED"),
        ]),
        bq.SchemaField("timeline", "RECORD", mode="REPEATED", fields=[
            bq.SchemaField("date", "STRING"),
            bq.SchemaField("event", "STRING"),
            bq.SchemaField("confidence", "STRING"),
            bq.SchemaField("sources", "STRING", mode="REPEATED"),
        ]),
        bq.SchemaField("sources", "RECORD", mode="REPEATED", fields=[
            bq.SchemaField("title", "STRING"),
            bq.SchemaField("uri", "STRING"),
            bq.SchemaField("subfolder", "STRING"),
        ]),
        bq.SchemaField("payload_json", "STRING", mode="REQUIRED"),
        bq.SchemaField("loaded_at", "TIMESTAMP", mode="REQUIRED"),
        bq.SchemaField("loader_version", "STRING"),
    ]
    fields_schema = [
        bq.SchemaField("event_id", "STRING", mode="REQUIRED"),
        bq.SchemaField("generated_at", "TIMESTAMP", mode="REQUIRED"),
        bq.SchemaField("folder_key", "STRING", mode="REQUIRED"),
        bq.SchemaField("field_name", "STRING", mode="REQUIRED"),
        bq.SchemaField("field_value", "STRING"),
        bq.SchemaField("confidence", "STRING"),
        bq.SchemaField("source_file", "STRING"),
    ]
    inventory_schema = [
        bq.SchemaField("event_id", "STRING", mode="REQUIRED"),
        bq.SchemaField("generated_at", "TIMESTAMP", mode="REQUIRED"),
        bq.SchemaField("folder_key", "STRING", mode="REQUIRED"),
        bq.SchemaField("doc_name", "STRING", mode="REQUIRED"),
        bq.SchemaField("doc_uri", "STRING"),
        bq.SchemaField("doc_type", "STRING"),
        bq.SchemaField("bucket", "STRING"),
        bq.SchemaField("is_marker", "BOOL", mode="REQUIRED"),
    ]
    open_items_schema = [
        bq.SchemaField("event_id", "STRING", mode="REQUIRED"),
        bq.SchemaField("generated_at", "TIMESTAMP", mode="REQUIRED"),
        bq.SchemaField("folder_key", "STRING", mode="REQUIRED"),
        bq.SchemaField("checklist_name", "STRING"),
        bq.SchemaField("label", "STRING", mode="REQUIRED"),
        bq.SchemaField("bucket", "STRING"),
        bq.SchemaField("status", "STRING", mode="REQUIRED"),
        bq.SchemaField("strict_count", "INT64"),
        bq.SchemaField("total_count", "INT64"),
    ]
    return {
        TABLE_EVENTS: events_schema,
        TABLE_FIELDS: fields_schema,
        TABLE_INVENTORY: inventory_schema,
        TABLE_OPEN_ITEMS: open_items_schema,
    }


# ─── CLI & main ────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load structured_summary JSONL into BigQuery (idempotent).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Parse and shape rows but do not write to BigQuery. Prints projected row counts.",
    )
    p.add_argument(
        "--since", metavar="TIMESTAMP",
        help="Load only records with generated_at >= TIMESTAMP (ISO-8601 UTC).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    print("=" * 70)
    print("BigQuery loader v1 -- folder_intelligence")
    print("=" * 70)
    print(f"Source:  {JSONL_PATH}")
    print(f"Target:  {PROJECT}.{DATASET}.<4 tables>  (region {LOCATION})")
    if args.dry_run:
        print("Mode:    DRY RUN -- no BigQuery writes")
    print()

    # Parse --since
    since_dt = None
    if args.since:
        since_dt = _parse_ts(args.since)
        if since_dt is None:
            print(f"ERROR: --since not parseable as ISO-8601: {args.since!r}", file=sys.stderr)
            return 2
        print(f"Filter: --since {since_dt.isoformat()}")

    # Load JSONL
    records, malformed, skipped_since = _load_jsonl(JSONL_PATH, since_dt)
    print(f"  Records read:           {len(records)}")
    print(f"  Malformed lines skipped: {malformed}")
    if since_dt is not None:
        print(f"  Records before --since: {skipped_since}")

    if not records:
        print("\nNothing to load.")
        return 0

    # Shape rows
    loaded_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_rows = []
    field_rows = []
    inventory_rows = []
    open_items_rows = []
    for rec in records:
        event_row = _shape_event_row(rec, loaded_at_iso)
        eid = event_row["event_id"]
        event_rows.append(event_row)
        field_rows.extend(_shape_structured_field_rows(rec, eid))
        inventory_rows.extend(_shape_inventory_rows(rec, eid))
        open_items_rows.extend(_shape_open_items_rows(rec, eid))

    print()
    print("Projected row counts:")
    print(f"  folder_intelligence_events:   {len(event_rows)}")
    print(f"  structured_fields_long:       {len(field_rows)}")
    print(f"  document_inventory_items:     {len(inventory_rows)}")
    print(f"  open_items_long:              {len(open_items_rows)}")

    # Check for duplicate event_ids in the staging set (would indicate a hash
    # collision or duplicate JSONL lines). Either is worth knowing.
    from collections import Counter
    eid_counts = Counter(r["event_id"] for r in event_rows)
    dupes = [(eid, n) for eid, n in eid_counts.items() if n > 1]
    if dupes:
        print(f"\n  WARN: {len(dupes)} event_id(s) appear multiple times in staging.")
        for eid, n in dupes[:5]:
            print(f"    {eid}  ({n} occurrences)")
        print("    Last write wins; consider --since to scope re-loads.")

    if args.dry_run:
        print("\nDry run complete. No writes performed.")
        return 0

    # Real load
    print()
    print("Connecting to BigQuery...")
    client = _init_bigquery_client()
    print(f"  Authenticated. Project: {client.project}")

    # Pre-flight: dataset, then tables. Better error message if either
    # is missing -- without this, the table check raises a less-helpful
    # "Not found: Table ..." that doesn't say the dataset is the issue.
    if not _dataset_exists(client):
        print(f"\nERROR: Dataset {PROJECT}.{DATASET} does not exist.", file=sys.stderr)
        print(f"\n       Create it first (one-time setup):", file=sys.stderr)
        print(f"       bq mk --dataset --location={LOCATION} \\", file=sys.stderr)
        print(f"         {PROJECT}:{DATASET}", file=sys.stderr)
        print(f"\n       Then create the tables:", file=sys.stderr)
        print(f"       Get-Content infra/bigquery/schema.sql -Raw | \\", file=sys.stderr)
        print(f"         bq query --use_legacy_sql=false --location={LOCATION} \\", file=sys.stderr)
        print(f"           --project_id={PROJECT}", file=sys.stderr)
        return 2

    missing = _tables_exist(client)
    if missing:
        print(f"\nERROR: Target tables missing in BigQuery:", file=sys.stderr)
        for t in missing:
            print(f"  {t}", file=sys.stderr)
        print(f"\n       Run the DDL:", file=sys.stderr)
        print(f"       Get-Content infra/bigquery/schema.sql -Raw | \\", file=sys.stderr)
        print(f"         bq query --use_legacy_sql=false --location={LOCATION} \\", file=sys.stderr)
        print(f"           --project_id={PROJECT}", file=sys.stderr)
        return 2

    schemas = _build_schemas()

    print()
    print("Loading...")

    # 1) Parent events
    staging = _load_to_staging(client, TABLE_EVENTS, event_rows, schemas[TABLE_EVENTS])
    affected = _merge_events(client, staging)
    print(f"  folder_intelligence_events:   MERGE affected {affected} row(s) "
          f"(staging: {len(event_rows)})")

    # 2) Children -- DELETE-by-event then INSERT
    for target, rows, label in [
        (TABLE_FIELDS,     field_rows,      "structured_fields_long"),
        (TABLE_INVENTORY,  inventory_rows,  "document_inventory_items"),
        (TABLE_OPEN_ITEMS, open_items_rows, "open_items_long"),
    ]:
        if not rows:
            print(f"  {label:<30s}: 0 rows to load, skipping")
            continue
        staging = _load_to_staging(client, target, rows, schemas[target])
        inserted = _replace_child_rows(client, target, staging)
        print(f"  {label:<30s}: INSERT {inserted} row(s) (staging: {len(rows)})")

    print()
    print("=" * 70)
    print("Load complete.")
    print("Next step: run smoke queries -- python scripts/bq_smoke_queries.py")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
