#!/usr/bin/env python3
"""
audit_extraction_pipeline.py
─────────────────────────────
Walks every PDF in gs://<bucket>/onedrive-mirror/ and reports what the
production extraction pipeline (onedrive_sync._build_and_upload_manifest)
would do with each one.

Why this exists:
    The sync pipeline has 3 tiers (pdfplumber pre-extract -> Document AI OCR
    -> Vertex passthrough) plus an OCR cache. Today we have no aggregate
    visibility into which tier each doc lands in -- only scattered print()
    statements in onedrive_sync.py at sync time. This script gives a
    one-shot CSV of the whole corpus so we can answer:

      - How many PDFs ship with usable text (tier 1)?
      - How many trip the OCR pattern (tier 2)?
      - How many already have a populated ocr-cache entry?
      - How many would fall through to Vertex passthrough (tier 3)?
      - Are there suspicious cases: thin text layers (chars/MB very low)
        that tier 1 ACCEPTED but probably shouldn't have?

Cost / safety:
    - Read-only. Does NOT call Document AI. Does NOT modify GCS. Does NOT
      touch Vertex. Just downloads PDF bytes + checks ocr-cache existence.
    - Imports the actual production extraction function from onedrive_sync
      so the audit reflects exactly what production does.

Usage:
    cd C:\\Users\\Harris\\Desktop\\ClaudeWork\\dev\\MadisonAve
    python scripts/audit_extraction_pipeline.py

    # Smaller test run: only first N PDFs
    python scripts/audit_extraction_pipeline.py --limit 20

    # Resume from a previous run (skips files already in the CSV)
    python scripts/audit_extraction_pipeline.py --resume audit_extraction_<stamp>.csv

Outputs (written to repo root):
    audit_extraction_<YYYYMMDD-HHMM>.csv   one row per PDF
    audit_extraction_<YYYYMMDD-HHMM>.txt   summary + recommendations
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Make sibling project modules importable ────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "Phase5_oneDrive"))

# Load .env BEFORE importing google libs so GOOGLE_APPLICATION_CREDENTIALS
# is in place when the GCS client initializes.
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

from google.cloud import storage  # noqa: E402

# Import the EXACT production functions. We do NOT reimplement tier 1 or
# the OCR heuristic -- the audit must match production behavior or the
# numbers it produces are meaningless.
from onedrive_sync import _extract_text_from_bytes  # noqa: E402
from phase6_ocr_metadata import needs_ocr, _ocr_cache_path  # noqa: E402


# ── Config ──────────────────────────────────────────────────────────────────
BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME") or os.environ.get("GCS_BUCKET_RAW", "")
PREFIX = "onedrive-mirror/"
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")

# Threshold below which tier 1 output is "suspicious" -- a real text PDF has
# 2000+ chars per MB; thin-layer scans return <200. Anything in the gap is
# the hybrid-PDF case worth flagging.
SUSPICIOUS_CHARS_PER_MB = 200


# ── Per-doc audit record ────────────────────────────────────────────────────
def audit_one(blob, bucket) -> dict:
    """Run the production tier logic against a single blob. Returns a dict
    suitable for one CSV row. Never raises -- failures land in the 'error'
    field so the run continues."""
    rec = {
        "blob_name":        blob.name,
        "size_bytes":       blob.size or 0,
        "size_mb":          round((blob.size or 0) / 1_000_000, 2),
        "tier1_chars":      0,
        "tier1_chars_per_mb": 0,
        "tier1_succeeded":  False,
        "needs_ocr":        False,
        "ocr_cache_exists": False,
        "ocr_cache_chars":  0,
        "production_tier":  "",
        "flag":             "",
        "error":            "",
    }
    try:
        # --- Tier 1: pdfplumber pre-extraction (production code, verbatim) ---
        try:
            file_bytes = blob.download_as_bytes()
        except Exception as e:
            rec["error"] = f"download_failed: {type(e).__name__}: {e}"
            rec["production_tier"] = "tier3_passthrough"
            return rec

        try:
            tier1_text = _extract_text_from_bytes(file_bytes, ".pdf", blob.name)
        except Exception as e:
            tier1_text = ""
            rec["error"] = f"tier1_exception: {type(e).__name__}: {e}"
        rec["tier1_chars"] = len(tier1_text or "")
        rec["tier1_succeeded"] = bool(tier1_text)

        size_mb = max((blob.size or 0) / 1_000_000, 0.01)
        rec["tier1_chars_per_mb"] = int(rec["tier1_chars"] / size_mb)

        # --- Tier 2 gate: needs_ocr() heuristic (production code, verbatim) ---
        rec["needs_ocr"] = needs_ocr(blob.name, blob.size or 0)

        # --- OCR cache probe ---
        # The cache key is keyed off the FULL gs:// uri in production.
        # _ocr_cache_path takes any string starting with gs:// and produces
        # the cache blob path.
        gcs_uri = f"gs://{BUCKET_NAME}/{blob.name}"
        cache_blob_name = _ocr_cache_path(gcs_uri)
        cache_blob = bucket.blob(cache_blob_name)
        try:
            if cache_blob.exists():
                rec["ocr_cache_exists"] = True
                cache_blob.reload()
                rec["ocr_cache_chars"] = cache_blob.size or 0
        except Exception as e:
            # Don't let a cache probe failure kill the row -- record it.
            rec["error"] = (rec["error"] + " | " if rec["error"] else "") + f"cache_probe_failed: {e}"

        # --- Production tier classification ---
        # Replicate the exact decision tree in
        # _build_and_upload_manifest():
        #   if tier1 succeeded                -> tier1_extracted
        #   elif needs_ocr -> OCR runs:
        #       if cached                     -> tier2_ocr_cached
        #       else                          -> tier2_ocr_live (would API-call)
        #   else                              -> tier3_passthrough
        if rec["tier1_succeeded"]:
            rec["production_tier"] = "tier1_extracted"
        elif rec["needs_ocr"]:
            rec["production_tier"] = (
                "tier2_ocr_cached" if rec["ocr_cache_exists"] else "tier2_ocr_live"
            )
        else:
            rec["production_tier"] = "tier3_passthrough"

        # --- Flag suspicious / interesting cases ---
        # 1) Hybrid-PDF gap: tier 1 accepted but chars/MB suspiciously low.
        if rec["tier1_succeeded"] and rec["tier1_chars_per_mb"] < SUSPICIOUS_CHARS_PER_MB:
            rec["flag"] = "thin_text_layer_accepted"
        # 2) needs_ocr=True but tier 1 returned text -- we're skipping OCR
        #    because of tier 1's success. Worth a look.
        elif rec["needs_ocr"] and rec["tier1_succeeded"]:
            rec["flag"] = "scan_pattern_but_tier1_succeeded"
        # 3) OCR triggered but no cache exists yet -- next sync will pay $.
        elif rec["production_tier"] == "tier2_ocr_live":
            rec["flag"] = "ocr_pending_first_run"
        # 4) Falls through to passthrough -- Vertex's parser is the only
        #    chance. Could indicate non-standard scanner output.
        elif rec["production_tier"] == "tier3_passthrough":
            rec["flag"] = "passthrough_check_filename"

    except Exception as e:
        rec["error"] = (rec["error"] + " | " if rec["error"] else "") + f"row_exception: {type(e).__name__}: {e}"
    return rec


# ── Main walk ───────────────────────────────────────────────────────────────
def run(limit: int | None, resume_csv: Path | None) -> Path:
    if not BUCKET_NAME:
        sys.exit("GCS_BUCKET_NAME / GCS_BUCKET_RAW not set in environment")
    if not PROJECT_ID:
        sys.exit("GCP_PROJECT_ID not set in environment")

    print(f"Bucket:  {BUCKET_NAME}")
    print(f"Prefix:  {PREFIX}")
    print(f"Project: {PROJECT_ID}")
    print(f"Limit:   {limit or 'no limit'}")
    print()

    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(BUCKET_NAME)

    # Output paths
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_csv = REPO_ROOT / f"audit_extraction_{stamp}.csv"
    out_txt = REPO_ROOT / f"audit_extraction_{stamp}.txt"

    # Resume support: if asked to resume, skip blob_names already present
    # in the resume CSV. Useful for very large corpora where a run might
    # be interrupted.
    seen_names: set[str] = set()
    if resume_csv and resume_csv.exists():
        with resume_csv.open(newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                seen_names.add(row.get("blob_name", ""))
        print(f"Resuming: {len(seen_names)} files already audited in {resume_csv.name}")
        # Append to the same CSV in resume mode.
        out_csv = resume_csv
        write_header = False
    else:
        write_header = True

    # CSV setup
    fieldnames = [
        "blob_name", "size_bytes", "size_mb",
        "tier1_chars", "tier1_chars_per_mb", "tier1_succeeded",
        "needs_ocr", "ocr_cache_exists", "ocr_cache_chars",
        "production_tier", "flag", "error",
    ]
    csv_mode = "a" if (resume_csv and resume_csv.exists()) else "w"
    f_csv = out_csv.open(csv_mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(f_csv, fieldnames=fieldnames)
    if write_header:
        writer.writeheader()

    # Counters for summary
    counts = {
        "tier1_extracted":   0,
        "tier2_ocr_cached":  0,
        "tier2_ocr_live":    0,
        "tier3_passthrough": 0,
    }
    flag_counts: dict[str, int] = {}
    total = 0
    errors = 0
    start = time.time()

    print("Walking bucket...")
    pdf_count_total = 0
    try:
        for blob in bucket.list_blobs(prefix=PREFIX):
            if not blob.name.lower().endswith(".pdf"):
                continue
            pdf_count_total += 1
            if blob.name in seen_names:
                continue
            if limit and total >= limit:
                break

            rec = audit_one(blob, bucket)
            writer.writerow(rec)
            f_csv.flush()  # flush every row so a Ctrl+C still leaves usable data

            counts[rec["production_tier"]] = counts.get(rec["production_tier"], 0) + 1
            if rec["flag"]:
                flag_counts[rec["flag"]] = flag_counts.get(rec["flag"], 0) + 1
            if rec["error"]:
                errors += 1
            total += 1
            if total % 25 == 0:
                elapsed = time.time() - start
                rate = total / max(elapsed, 1)
                print(f"  {total:>5} done  ({rate:.1f}/s)  current: {Path(rec['blob_name']).name[:60]}")
    finally:
        f_csv.close()

    elapsed = time.time() - start
    print()
    print(f"Done. {total} PDFs audited in {elapsed:.0f}s "
          f"({(total / max(elapsed, 1)):.1f}/s).  Errors: {errors}.")
    print(f"CSV:  {out_csv}")

    # ── Summary report ─────────────────────────────────────────────────────
    summary_lines = []
    summary_lines.append(f"Audit run: {stamp}")
    summary_lines.append(f"Bucket:    gs://{BUCKET_NAME}/{PREFIX}")
    summary_lines.append(f"PDFs found in bucket:        {pdf_count_total}")
    summary_lines.append(f"PDFs audited this run:       {total}")
    summary_lines.append(f"Errors during audit:         {errors}")
    summary_lines.append(f"Elapsed:                     {elapsed:.0f}s")
    summary_lines.append("")
    summary_lines.append("── Production tier breakdown ──")
    for tier, n in sorted(counts.items()):
        pct = 100 * n / max(total, 1)
        summary_lines.append(f"  {tier:22s} {n:>5}  ({pct:5.1f}%)")
    summary_lines.append("")
    summary_lines.append("── Flags (cases worth a closer look) ──")
    if flag_counts:
        for flag, n in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
            pct = 100 * n / max(total, 1)
            summary_lines.append(f"  {flag:38s} {n:>5}  ({pct:5.1f}%)")
    else:
        summary_lines.append("  (none)")
    summary_lines.append("")
    summary_lines.append("── Recommendation hints ──")
    # Generate concrete next-step suggestions based on the numbers.
    hints = []
    pct_t1 = 100 * counts["tier1_extracted"] / max(total, 1)
    pct_t2_live = 100 * counts["tier2_ocr_live"] / max(total, 1)
    pct_t3 = 100 * counts["tier3_passthrough"] / max(total, 1)
    thin_n = flag_counts.get("thin_text_layer_accepted", 0)
    pct_thin = 100 * thin_n / max(total, 1)
    if pct_t1 > 70:
        hints.append("Tier 1 extraction is doing most of the work. Pipeline is healthy.")
    if pct_thin > 5:
        hints.append(
            f"{thin_n} files ({pct_thin:.1f}%) had tier 1 succeed but with "
            f"<{SUSPICIOUS_CHARS_PER_MB} chars/MB. This is the hybrid-PDF "
            f"gap -- consider switching the OCR trigger from filename "
            f"pattern to chars-per-MB threshold so these get OCR'd."
        )
    if pct_t2_live > 5:
        hints.append(
            f"{counts['tier2_ocr_live']} files would trigger a live "
            f"Document AI call on the next sync (no cache yet). Roughly "
            f"${counts['tier2_ocr_live'] * 0.0015 * 15:.2f}-"
            f"${counts['tier2_ocr_live'] * 0.0015 * 50:.2f} estimated, "
            f"depending on page counts. Run a `--rebuild-only` sync to "
            f"populate the cache once."
        )
    if pct_t3 > 10:
        hints.append(
            f"{counts['tier3_passthrough']} files ({pct_t3:.1f}%) fall "
            f"through to Vertex passthrough. Look at filenames in the "
            f"`passthrough_check_filename` flag rows -- if they are scans "
            f"with non-Doorloop naming, expand the scan pattern in "
            f"phase6_ocr_metadata._SCAN_PATTERNS."
        )
    if not hints:
        hints.append("Nothing obviously broken in the breakdown.")
    for h in hints:
        summary_lines.append(f"  - {h}")
    summary_lines.append("")
    summary_lines.append(f"Full per-file breakdown: {out_csv.name}")

    summary_text = "\n".join(summary_lines)
    out_txt.write_text(summary_text, encoding="utf-8")
    print()
    print(summary_text)
    print()
    print(f"Summary written to: {out_txt}")
    return out_csv


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N PDFs (useful for a quick test)")
    p.add_argument("--resume", metavar="CSV_PATH",
                   help="Resume from a previous run's CSV (skip files already audited)")
    args = p.parse_args()

    resume_csv = Path(args.resume) if args.resume else None
    run(limit=args.limit, resume_csv=resume_csv)


if __name__ == "__main__":
    main()
