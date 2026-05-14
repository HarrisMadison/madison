"""BigQuery v1 smoke queries -- folder_intelligence dataset.

Validates that the loaded schema can answer the questions the design was
built for. Read-only. Safe to run any time. Useful after every loader run
as a sanity check.

Queries:
  Q1. Latest state per folder
  Q2. Folders by purpose (latest snapshot)
  Q3. Open items distribution (latest snapshot per folder)
  Q4. Extracted fields population (across latest events)
  Q5. Marker-file inventory count by folder

Usage:
  python scripts/bq_smoke_queries.py
  python scripts/bq_smoke_queries.py --query 1     # run only Q1
"""

import argparse
import sys
from typing import List, Optional

PROJECT = "madison-rag-60"
DATASET = "folder_intelligence"
LOCATION = "us-central1"

QUERIES = [
    ("Q1", "Latest state per folder",
     f"""
    WITH ranked AS (
      SELECT
        folder_key,
        folder_name,
        folder_purpose,
        property_address,
        contract_status,
        inspection_status,
        file_count_total,
        generated_at,
        response_kind,
        ROW_NUMBER() OVER (
          PARTITION BY folder_key
          ORDER BY generated_at DESC
        ) AS rn
      FROM `{PROJECT}.{DATASET}.folder_intelligence_events`
      WHERE response_kind = 'folder_summary'
    )
    SELECT
      folder_key,
      folder_purpose,
      property_address,
      contract_status,
      inspection_status,
      file_count_total,
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', generated_at) AS generated_at
    FROM ranked
    WHERE rn = 1
    ORDER BY folder_key
    """),

    ("Q2", "Folder count by purpose (latest snapshot)",
     f"""
    WITH ranked AS (
      SELECT
        folder_key,
        folder_purpose,
        ROW_NUMBER() OVER (
          PARTITION BY folder_key ORDER BY generated_at DESC
        ) AS rn
      FROM `{PROJECT}.{DATASET}.folder_intelligence_events`
      WHERE response_kind = 'folder_summary'
    )
    SELECT
      folder_purpose,
      COUNT(*) AS folder_count
    FROM ranked
    WHERE rn = 1
    GROUP BY folder_purpose
    ORDER BY folder_count DESC
    """),

    ("Q3", "Open items distribution (latest snapshot per folder)",
     f"""
    WITH latest_event AS (
      SELECT
        folder_key,
        MAX(generated_at) AS latest_at
      FROM `{PROJECT}.{DATASET}.folder_intelligence_events`
      WHERE response_kind IN ('folder_summary', 'open_items_only')
      GROUP BY folder_key
    ),
    latest_items AS (
      SELECT o.label, o.status
      FROM `{PROJECT}.{DATASET}.open_items_long` o
      JOIN latest_event l
        ON o.folder_key = l.folder_key
       AND o.generated_at = l.latest_at
    )
    SELECT
      label,
      status,
      COUNT(*) AS n
    FROM latest_items
    GROUP BY label, status
    ORDER BY label, status
    """),

    ("Q4", "Extracted field population (latest event per folder)",
     f"""
    WITH latest_event AS (
      SELECT
        folder_key,
        MAX(generated_at) AS latest_at
      FROM `{PROJECT}.{DATASET}.folder_intelligence_events`
      WHERE response_kind = 'folder_summary'
      GROUP BY folder_key
    ),
    folder_count AS (
      SELECT COUNT(*) AS total FROM latest_event
    )
    SELECT
      sf.field_name,
      COUNT(*) AS folders_with_value,
      (SELECT total FROM folder_count) AS total_folders,
      ROUND(100.0 * COUNT(*) / (SELECT total FROM folder_count), 1) AS pct
    FROM `{PROJECT}.{DATASET}.structured_fields_long` sf
    JOIN latest_event l
      ON sf.folder_key = l.folder_key
     AND sf.generated_at = l.latest_at
    GROUP BY sf.field_name
    ORDER BY folders_with_value DESC, sf.field_name
    """),

    ("Q5", "Marker-file inventory count by folder (latest event)",
     f"""
    WITH latest_event AS (
      SELECT
        folder_key,
        MAX(generated_at) AS latest_at
      FROM `{PROJECT}.{DATASET}.folder_intelligence_events`
      WHERE response_kind = 'folder_summary'
      GROUP BY folder_key
    )
    SELECT
      d.folder_key,
      COUNT(*) AS total_docs,
      COUNTIF(d.is_marker) AS marker_docs,
      COUNTIF(NOT d.is_marker) AS text_docs
    FROM `{PROJECT}.{DATASET}.document_inventory_items` d
    JOIN latest_event l
      ON d.folder_key = l.folder_key
     AND d.generated_at = l.latest_at
    GROUP BY d.folder_key
    HAVING marker_docs > 0 OR total_docs > 0
    ORDER BY marker_docs DESC, total_docs DESC
    """),
]


def _init_client():
    try:
        from google.cloud import bigquery
    except ImportError as e:
        print(f"ERROR: google-cloud-bigquery not installed: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        return bigquery.Client(project=PROJECT, location=LOCATION)
    except Exception as e:
        print(f"ERROR: BigQuery client init failed: {e}", file=sys.stderr)
        sys.exit(2)


def _run(client, qid: str, label: str, sql: str) -> None:
    print()
    print("=" * 70)
    print(f"{qid}  {label}")
    print("=" * 70)
    job = client.query(sql)
    rows = list(job.result())
    if not rows:
        print("  (no rows)")
        return
    # Pretty print as columns
    fields = list(rows[0].keys())
    widths = {f: max(len(f), max(len(str(r[f])) for r in rows)) for f in fields}
    header = "  " + "  ".join(f.ljust(widths[f]) for f in fields)
    print(header)
    print("  " + "  ".join("-" * widths[f] for f in fields))
    for r in rows:
        print("  " + "  ".join(str(r[f]).ljust(widths[f]) for f in fields))
    print(f"  ({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke queries against folder_intelligence v1.")
    p.add_argument("--query", type=str, metavar="N",
                   help="Run only query N (1-5). Default: all.")
    args = p.parse_args()

    selected: List[int]
    if args.query:
        try:
            selected = [int(args.query)]
        except ValueError:
            print(f"ERROR: --query takes an integer 1-5, got {args.query!r}", file=sys.stderr)
            return 2
    else:
        selected = list(range(1, len(QUERIES) + 1))

    print("=" * 70)
    print("folder_intelligence v1 smoke queries")
    print("=" * 70)
    print(f"Target: {PROJECT}.{DATASET} ({LOCATION})")

    client = _init_client()
    for i in selected:
        if i < 1 or i > len(QUERIES):
            print(f"  WARN: query {i} out of range, skipping", file=sys.stderr)
            continue
        qid, label, sql = QUERIES[i - 1]
        try:
            _run(client, qid, label, sql)
        except Exception as e:
            print(f"  ERROR running {qid}: {e}", file=sys.stderr)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
