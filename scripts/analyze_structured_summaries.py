"""Read-only analysis of the structured_summary JSONL corpus.

Reads:
    data/structured_summaries/structured_summary_events.jsonl

Produces:
    Concise stdout report on row counts, structured_fields population,
    open_items status distributions, document_inventory shape, and a
    first-pass BigQuery schema recommendation grounded in what's actually
    in the persisted records.

Read-only -- never writes back to the JSONL, never touches the running
service. Safe to run any time. Rerun as the corpus grows to get
sharper recommendations.

Usage:
    # Whole file (default; cumulative history)
    python scripts/analyze_structured_summaries.py

    # Only records emitted after a specific timestamp
    python scripts/analyze_structured_summaries.py --since 2026-05-13T18:00:00Z

    # Only the most recent batch (auto-detected by timestamp clustering)
    python scripts/analyze_structured_summaries.py --latest-run

Why filter:
    The JSONL is append-only history. Parser and classifier improvements
    do not rewrite old rows -- a row written before a parser change keeps
    whatever structured_fields the older parser produced. So cumulative
    rates can drift below current-parser rates as the historical sample
    grows. Use --latest-run or --since when you want "what is the parser
    doing NOW" rather than "what has the parser ever done."

Exits 0 even if the file is empty -- this is an analysis tool, not a
test, so absence of data isn't a failure.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Configuration ──────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = REPO_ROOT / "data" / "structured_summaries" / "structured_summary_events.jsonl"

# Soft threshold for the BigQuery recommendation: fields populated at or
# above this rate are recommended as first-class scalar columns; below
# it, they're still scalars but flagged "low population, consider
# leaving in nested JSON if storage cost matters".
POPULATION_THRESHOLD_PCT = 40.0

# The 16 structured_fields keys, from the schema contract.
STRUCTURED_FIELDS_KEYS = [
    "property_address", "folder_name", "folder_purpose",
    "appraised_value", "appraisal_effective_date",
    "insurance_carrier", "claim_number",
    "estimate_total", "invoice_total", "inspection_date",
    "contract_status", "insurance_status", "estimate_status",
    "invoice_status", "inspection_status", "photos_status",
]

# Status fields are categorical -- when promoted to scalar columns they
# become STRING with a constrained enum. Worth flagging separately.
STATUS_FIELDS = {
    "folder_purpose",
    "contract_status", "insurance_status", "estimate_status",
    "invoice_status", "inspection_status", "photos_status",
}

# ── Loaders ────────────────────────────────────────────────────────────
def _load_records(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Load every well-formed JSON line. Returns (records, malformed_count).

    Malformed lines are skipped with a stderr warning so a single bad
    write doesn't abort the whole analysis.
    """
    if not path.exists():
        return [], 0
    records: List[Dict[str, Any]] = []
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
                else:
                    bad += 1
            except json.JSONDecodeError as e:
                bad += 1
                print(f"  WARN: line {lineno} malformed JSON: {e}", file=sys.stderr)
    return records, bad


# ── Timestamp filtering ────────────────────────────────────────────────
# The generated_at field is written by the structured_summary normalizer
# as ISO-8601 UTC with a 'Z' suffix, format "%Y-%m-%dT%H:%M:%SZ". Records
# without a parseable timestamp are treated as missing and excluded from
# any time-filtered view (better to under-count than to lie).

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse a JSONL generated_at value. Accepts both 'Z' and explicit
    offsets. Returns timezone-aware UTC datetime, or None on failure.
    """
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    # Most common shape from the writer.
    fmts = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S")
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Fallback: fromisoformat handles +00:00 etc.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


# Gap between consecutive timestamps that counts as a "run boundary."
# In practice, a full sample run is dominated by Gemini Flash latency.
# A 13-folder batch can take 15-45 minutes depending on Gemini queueing
# and any 429 retries. Intra-batch gaps of 10-15 minutes happen when a
# single call is slow. So we want a threshold that's:
#   - large enough to NOT split a single batch into pieces
#     (observed intra-batch gaps: up to ~12 min on the current corpus)
#   - small enough to detect genuine inter-batch boundaries
#     (observed inter-batch gaps: 19 min and up)
# 15 minutes sits in that gap. If runs ever start interleaving (parallel
# clients) this heuristic breaks; revisit then. For now this matches
# observed timing on a single-user dev workflow.
_LATEST_RUN_GAP_SECONDS = 15 * 60


def _detect_latest_run_cutoff(records: List[Dict]) -> Optional[datetime]:
    """Return the start-of-latest-run datetime by walking generated_at
    timestamps backwards from the most recent record and stopping at the
    first gap larger than _LATEST_RUN_GAP_SECONDS.

    Returns None if no records have a parseable timestamp.

    The walk is descending by time: the most recent record is always
    inside the latest run; we extend the run backward through every
    earlier record whose timestamp is within the gap threshold of the
    next-later record. The first oversize gap stops the walk.
    """
    timestamps = sorted(
        (ts for ts in (_parse_ts(r.get("generated_at")) for r in records)
         if ts is not None),
        reverse=True,
    )
    if not timestamps:
        return None
    cutoff = timestamps[0]
    for earlier in timestamps[1:]:
        gap = (cutoff - earlier).total_seconds()
        if gap > _LATEST_RUN_GAP_SECONDS:
            break
        cutoff = earlier
    return cutoff


def _apply_time_filter(records: List[Dict],
                         since: Optional[datetime]) -> Tuple[List[Dict], int]:
    """Keep records whose generated_at >= since. Records with no parseable
    timestamp are excluded when a filter is active (we can't tell whether
    they belong in the window or not -- safer to drop). Returns
    (kept_records, dropped_count).
    """
    if since is None:
        return records, 0
    kept: List[Dict] = []
    dropped = 0
    for rec in records:
        ts = _parse_ts(rec.get("generated_at"))
        if ts is None:
            dropped += 1
            continue
        if ts >= since:
            kept.append(rec)
        else:
            dropped += 1
    return kept, dropped


# ── Formatters ─────────────────────────────────────────────────────────
def _hr(title: str = "") -> None:
    """Print a section divider."""
    if title:
        print("\n" + "=" * 70)
        print(title)
        print("=" * 70)
    else:
        print("-" * 70)


def _print_counter(counter: Counter, total: Optional[int] = None,
                    indent: str = "  ") -> None:
    """Print a Counter as 'label: count (pct%)' lines, descending."""
    if total is None:
        total = sum(counter.values())
    if total == 0:
        print(f"{indent}(no values)")
        return
    width = max((len(str(k)) for k in counter), default=4)
    for label, n in counter.most_common():
        pct = (n / total) * 100.0
        print(f"{indent}{str(label):<{width}}  {n:>5}  ({pct:5.1f}%)")


# ── Section: row counts ────────────────────────────────────────────────
def _section_row_counts(records: List[Dict]) -> None:
    _hr("1. Row counts")
    print(f"\n  Total rows: {len(records)}")
    if not records:
        return

    print("\n  By schema_version:")
    ver_counts = Counter(r.get("schema_version", "(missing)") for r in records)
    _print_counter(ver_counts, total=len(records))

    print("\n  By response_kind:")
    kind_counts = Counter(r.get("response_kind", "(missing)") for r in records)
    _print_counter(kind_counts, total=len(records))

    print("\n  By folder_purpose:")
    purpose_counts = Counter(r.get("folder_purpose", "(missing)") for r in records)
    _print_counter(purpose_counts, total=len(records))

    print("\n  By folder_name (top 10):")
    folder_counts = Counter(r.get("folder_name", "(missing)") for r in records)
    for label, n in folder_counts.most_common(10):
        print(f"    {label!r}  {n}")
    if len(folder_counts) > 10:
        print(f"    ...and {len(folder_counts) - 10} more folders")
    print(f"\n  Distinct folders represented: {len(folder_counts)}")


# ── Section: structured_fields coverage ────────────────────────────────
def _section_structured_fields(records: List[Dict]) -> Dict[str, float]:
    """Returns {field_name: population_pct} for BigQuery recommendation use."""
    _hr("2. structured_fields coverage")

    # Only rows that actually have a structured_fields block are eligible.
    # v1.0 rows predate the derivation layer and have to be excluded from
    # population math -- otherwise their absence would look like a low
    # population rate when really the data just wasn't computed.
    eligible = [r for r in records if isinstance(r.get("structured_fields"), dict)]
    skipped = len(records) - len(eligible)
    print(f"\n  Eligible rows (have structured_fields): {len(eligible)}")
    if skipped:
        print(f"  Skipped rows (older schema, no structured_fields): {skipped}")

    if not eligible:
        print("\n  (no records to analyze)")
        return {}

    print(f"\n  {'field':<28} {'pop':>4} {'%':>6}  {'conf dist':<24}  src#")
    print(f"  {'-' * 28} {'-' * 4} {'-' * 6}  {'-' * 24}  {'-' * 4}")

    population_pcts: Dict[str, float] = {}
    for field in STRUCTURED_FIELDS_KEYS:
        populated = 0
        conf_counts: Counter = Counter()
        with_source = 0
        for rec in eligible:
            sf = rec["structured_fields"]
            entry = sf.get(field) or {}
            if entry.get("value") is not None:
                populated += 1
                conf = entry.get("confidence")
                conf_counts[str(conf)] += 1
                if entry.get("source_file"):
                    with_source += 1
        pct = (populated / len(eligible)) * 100.0 if eligible else 0.0
        population_pcts[field] = pct
        # Confidence distribution as a compact inline summary.
        if conf_counts:
            conf_str = ", ".join(f"{k}={v}" for k, v in conf_counts.most_common())
            conf_str = conf_str[:23]
        else:
            conf_str = "(none)"
        print(f"  {field:<28} {populated:>4} {pct:>5.1f}%  {conf_str:<24}  {with_source:>4}")

    # Highlight value-typed extraction for casual scanning.
    print("\n  Reading the table:")
    print("    pop  = number of eligible rows where value is not None")
    print("    %    = pop / eligible rows")
    print("    src# = how many of those had a source_file attached")

    return population_pcts


# ── Section: open_items distribution ───────────────────────────────────
def _section_open_items(records: List[Dict]) -> None:
    _hr("3. open_items distribution")

    # Per-label status counts (label -> status -> count)
    by_label: Dict[str, Counter] = defaultdict(Counter)
    overall: Counter = Counter()
    rows_with_oi = 0
    total_entries = 0

    for rec in records:
        oi_list = rec.get("open_items") or []
        if oi_list:
            rows_with_oi += 1
        for oi in oi_list:
            label = oi.get("label", "(missing)")
            status = oi.get("status", "(missing)")
            by_label[label][status] += 1
            overall[status] += 1
            total_entries += 1

    print(f"\n  Rows with open_items: {rows_with_oi} / {len(records)}")
    print(f"  Total open_items entries: {total_entries}")

    print("\n  Overall status distribution:")
    _print_counter(overall, total=total_entries)

    if by_label:
        print("\n  Per-label status counts:")
        label_width = max(len(l) for l in by_label) if by_label else 20
        for label in sorted(by_label):
            counts = by_label[label]
            parts = [f"{s}={n}" for s, n in counts.most_common()]
            print(f"    {label:<{label_width}}  {', '.join(parts)}")


# ── Section: document_inventory distribution ───────────────────────────
def _section_document_inventory(records: List[Dict]) -> None:
    _hr("4. document_inventory distribution")

    bucket_counts: Counter = Counter()    # bucket -> # of records that have it
    bucket_doc_total: Counter = Counter() # bucket -> total docs across records
    docs_per_record: List[int] = []
    inventories_seen = 0

    for rec in records:
        inv = rec.get("document_inventory")
        if not isinstance(inv, dict):
            continue
        inventories_seen += 1
        total_in_rec = 0
        for bucket, items in inv.items():
            if not isinstance(items, list):
                continue
            n = len(items)
            if n > 0:
                bucket_counts[bucket] += 1
                bucket_doc_total[bucket] += n
                total_in_rec += n
        docs_per_record.append(total_in_rec)

    print(f"\n  Rows with document_inventory: {inventories_seen} / {len(records)}")
    if docs_per_record:
        avg = sum(docs_per_record) / len(docs_per_record)
        mx  = max(docs_per_record)
        mn  = min(docs_per_record)
        print(f"  Docs per record: avg={avg:.1f}  min={mn}  max={mx}")

    if not bucket_counts:
        return

    print("\n  Buckets observed (sorted by # of records that contain them):")
    print(f"    {'bucket':<16} {'#records':>9}  {'total docs':>11}")
    for bucket, n_records in bucket_counts.most_common():
        n_docs = bucket_doc_total[bucket]
        print(f"    {bucket:<16} {n_records:>9}  {n_docs:>11}")


# ── Section: BigQuery schema recommendation ────────────────────────────
def _section_bigquery_recommendation(records: List[Dict],
                                       field_population_pcts: Dict[str, float]) -> None:
    _hr("5. BigQuery schema recommendation")

    n_records = len(records)
    n_with_fields = sum(1 for r in records if isinstance(r.get("structured_fields"), dict))

    print(f"\n  Based on {n_records} record(s), {n_with_fields} with structured_fields.")
    if n_with_fields < 10:
        print(f"  CAVEAT: small sample -- treat the recommendation as a starting")
        print(f"  point and rerun this script after the corpus grows past ~20-30")
        print(f"  records covering varied folders before committing to schema.")

    # --- Recommended scalar columns (always) ---
    print("\n  RECOMMENDED scalar columns (always present, schema-stable):")
    always_scalar = [
        ("schema_version",        "STRING",    "schema contract version"),
        ("response_kind",         "STRING",    "enum: folder_summary|open_items_only|open_items_unknown"),
        ("generated_at",          "TIMESTAMP", "ISO-8601 UTC, parse server-side"),
        ("query",                 "STRING",    "originating user query text"),
        ("folder_name",           "STRING",    "from structured_summary"),
        ("folder_purpose",        "STRING",    "enum: claim_restoration|property_appraisal|unknown"),
        ("checklist_name",        "STRING",    "enum: claim_default|property_default|unknown"),
        ("file_count_total",      "INT64",     "total files in folder"),
        ("file_count_in_dossier", "INT64",     "files used in dossier for this response"),
        ("overview",              "STRING",    "Gemini-generated paragraph; high variance"),
        ("show_open_items",       "BOOL",      "did chat render the checklist"),
        ("confidence",            "STRING",    "envelope-level confidence"),
    ]
    for col, typ, note in always_scalar:
        print(f"    {col:<24} {typ:<10}  -- {note}")

    # --- structured_fields: which to promote to scalar columns ---
    print("\n  structured_fields flat-column recommendation:")
    if not field_population_pcts:
        print("    (no structured_fields data observed yet -- rerun after more records)")
    else:
        promoted: List[str] = []
        low_pop: List[Tuple[str, float]] = []
        for field in STRUCTURED_FIELDS_KEYS:
            pct = field_population_pcts.get(field, 0.0)
            if pct >= POPULATION_THRESHOLD_PCT:
                promoted.append(field)
            else:
                low_pop.append((field, pct))

        print(f"    Threshold for promotion: {POPULATION_THRESHOLD_PCT:.0f}% population")
        print(f"\n    PROMOTE to flat scalar columns "
              f"({len(promoted)}/{len(STRUCTURED_FIELDS_KEYS)} fields >= threshold):")
        for f in promoted:
            pct = field_population_pcts[f]
            bq_type = "STRING"  # status enums + most values are strings
            # Date fields could be DATE, but since extraction preserves
            # raw strings from Gemini, leaving as STRING + casting in
            # queries is safer until we tighten the source format.
            if f.endswith("_date"):
                bq_type = "STRING"  # parse to DATE downstream
            note = "(enum)" if f in STATUS_FIELDS else ""
            print(f"      {f:<28} {bq_type:<8} -- {pct:5.1f}% populated  {note}")
        if low_pop:
            print(f"\n    KEEP NULLABLE but flagged "
                  f"({len(low_pop)} fields below threshold):")
            for f, pct in sorted(low_pop, key=lambda x: -x[1]):
                note = "(enum)" if f in STATUS_FIELDS else ""
                print(f"      {f:<28} STRING   -- {pct:5.1f}% populated  {note}")
            print("\n    Note: low-population fields often map to specific folder")
            print("    types (e.g. claim_number only appears on claim folders).")
            print("    Including them as nullable columns still beats parsing JSON.")
        # source_file and confidence per field
        print(f"\n    Per-field source_file/confidence:")
        print(f"    Each promoted field would ALSO get two companion STRING columns")
        print(f"    in BigQuery -- e.g. appraised_value_source_file and")
        print(f"    appraised_value_confidence. Cheap, preserves provenance.")

    # --- Repeated / nested fields ---
    print("\n  RECOMMENDED as REPEATED RECORD (BigQuery ARRAY<STRUCT>):")
    nested_fields = [
        ("key_facts",         "ARRAY<STRUCT<label STRING, value STRING, confidence STRING, sources ARRAY<STRING>>>"),
        ("timeline",          "ARRAY<STRUCT<date STRING, event STRING, confidence STRING, sources ARRAY<STRING>>>"),
        ("open_items",        "ARRAY<STRUCT<label STRING, bucket STRING, status STRING, strict_count INT64, total_count INT64, checklist_name STRING>>"),
        ("sources",           "ARRAY<STRUCT<title STRING, uri STRING, subfolder STRING>>"),
    ]
    for col, typ in nested_fields:
        print(f"    {col}")
        print(f"      {typ}")

    # --- document_inventory is a special case ---
    print("\n  document_inventory -- special case:")
    print("    Currently shape: dict<bucket, list<{name, uri, doc_type, bucket}>>.")
    print("    BigQuery doesn't have dynamic-key maps. Recommended representation:")
    print("    flatten to ARRAY<STRUCT<name STRING, uri STRING, doc_type STRING,")
    print("    bucket STRING>>. The 'bucket' key on each item is already present,")
    print("    so flattening loses no information.")

    # --- JSON-only / observations ---
    print("\n  KEEP as JSON (no fixed schema):")
    print("    observations  -- free-text list of Gemini-emitted notes")
    print("    structured_fields -- redundant if we promote (above), drop here")

    # --- Partitioning / clustering ---
    print("\n  Partitioning / clustering suggestion:")
    print("    PARTITION BY DATE(generated_at) -- daily partitions for cost control")
    print("    CLUSTER BY folder_purpose, response_kind, folder_name -- common filters")


# ── CLI ────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyze the structured_summary JSONL corpus (read-only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--since",
        metavar="TIMESTAMP",
        help=(
            "Only analyze records with generated_at >= TIMESTAMP. Accepts "
            "ISO-8601 UTC ('2026-05-13T18:00:00Z') or with offset "
            "('2026-05-13T18:00:00+00:00'). Records with no parseable "
            "timestamp are excluded."
        ),
    )
    grp.add_argument(
        "--latest-run",
        action="store_true",
        help=(
            "Auto-detect the most recent batch by clustering generated_at "
            "timestamps. The cluster boundary is a gap of more than "
            f"{_LATEST_RUN_GAP_SECONDS // 60} minutes between consecutive "
            "records. Useful for confirming current-parser behavior "
            "without historical noise."
        ),
    )
    return p.parse_args()


# ── Main ───────────────────────────────────────────────────────────────
def main() -> int:
    args = _parse_args()

    print("=" * 70)
    print("structured_summary corpus analysis (read-only)")
    print("=" * 70)
    print(f"Source: {JSONL_PATH}")

    if not JSONL_PATH.exists():
        print(f"\nNo persistence file found at {JSONL_PATH}")
        print("Run some folder summary queries to generate records, then rerun.")
        return 0

    all_records, bad_lines = _load_records(JSONL_PATH)
    if bad_lines:
        print(f"\nSkipped {bad_lines} malformed JSON line(s).")
    if not all_records:
        print("\nNo records to analyze yet. Run some folder summary queries first.")
        return 0

    # ── Resolve the filter ─────────────────────────────────────────────
    # Either --since X, --latest-run, or nothing. Mutually exclusive
    # via argparse, so at most one branch fires.
    since: Optional[datetime] = None
    filter_label = "none (full history)"
    if args.since:
        parsed = _parse_ts(args.since)
        if parsed is None:
            print(f"\nERROR: --since value not parseable as ISO-8601: {args.since!r}",
                  file=sys.stderr)
            print("       Try a value like '2026-05-13T18:00:00Z'.", file=sys.stderr)
            return 2
        since = parsed
        filter_label = f"--since {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    elif args.latest_run:
        detected = _detect_latest_run_cutoff(all_records)
        if detected is None:
            print("\nERROR: --latest-run requested but no records have a parseable",
                  file=sys.stderr)
            print("       generated_at timestamp.", file=sys.stderr)
            return 2
        since = detected
        filter_label = (
            f"--latest-run (cluster start "
            f"{since.strftime('%Y-%m-%dT%H:%M:%SZ')}, "
            f"gap threshold {_LATEST_RUN_GAP_SECONDS // 60} min)"
        )

    records, dropped = _apply_time_filter(all_records, since)

    # ── Filter banner ──────────────────────────────────────────────────
    print()
    print(f"  Filter:           {filter_label}")
    print(f"  Total rows in file: {len(all_records)}")
    print(f"  Rows in analysis:   {len(records)}")
    if dropped:
        print(f"  Rows excluded:      {dropped}")
        print(f"                      (older than filter or missing timestamp)")

    if not records:
        print("\nNo records match the filter. Try --since with an earlier timestamp,")
        print("or run without filters to see cumulative history.")
        return 0

    _section_row_counts(records)
    population_pcts = _section_structured_fields(records)
    _section_open_items(records)
    _section_document_inventory(records)
    _section_bigquery_recommendation(records, population_pcts)

    print("\n" + "=" * 70)
    print(f"Analysis complete. {len(records)} record(s) processed.")
    if filter_label != "none (full history)":
        print(f"Filter applied: {filter_label}")
        print(f"({len(all_records) - len(records)} historical row(s) excluded "
              f"from this view.)")
    print("Rerun any time as the corpus grows; recommendations sharpen with N.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
