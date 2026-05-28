"""Fast Black Knight backfill — patch BK fields into the existing manifest
without re-downloading or re-extracting any file content.

Why this exists:
  The full `onedrive_sync.py --rebuild-only` path re-downloads every blob
  from GCS, re-runs text extraction (pdfplumber / docx / xlsx), and re-runs
  the content classifier on each file. That's hours of work. But the ONLY
  thing we actually need to change in the manifest is the Black Knight
  fields (project_id, person_name, address, etc.) -- everything else
  (title, doc_type, content rawBytes, etc.) is fine.

  This script rewrites the manifest in place by:
    1. Streaming the existing manifest from GCS
    2. For each line, parsing the JSON record
    3. Re-running enrich_metadata() on the blob name (path-based extractor,
       no file content needed)
    4. Merging the refreshed fields into the existing structData
    5. Writing the updated record to a new manifest
    6. Uploading the new manifest back to the same path
    7. Triggering a Vertex AI Search FULL re-import

  Runtime: a few minutes for ~12k records (depending on bandwidth).

Usage:
  cd C:\\Users\\Harris\\Desktop\\ClaudeWork\\dev\\MadisonAve
  python scripts\\backfill_black_knight_fields.py            # dry-run preview
  python scripts\\backfill_black_knight_fields.py --apply    # actually rewrite
"""
import _path  # noqa: F401  -- adds repo root to sys.path

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Ensure Phase5_oneDrive is on sys.path so phase6_ocr_metadata can find
# black_knight_extractor regardless of how this script was launched.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PHASE5_DIR = _REPO_ROOT / "Phase5_oneDrive"
if str(_PHASE5_DIR) not in sys.path:
    sys.path.insert(0, str(_PHASE5_DIR))

from core import load_config, storage_client
from phase6_ocr_metadata import enrich_metadata, extract_project, _BK_IMPORT_ERROR


def _make_doc_id(blob_name: str) -> str:
    """Reproduce onedrive_sync.py's _make_doc_id so we can build a reverse
    lookup from doc-id back to blob name.

    Vertex requires document IDs match [a-zA-Z0-9_-]*. The sync sanitizes
    blob paths by replacing every other char with _, collapsing repeats,
    stripping leading/trailing underscores, and truncating to 128 chars.
    Lossy: '1. Claims In Progress' and '1__Claims__In__Progress' both
    sanitize to '1_Claims_In_Progress', so reversal would be ambiguous --
    but in practice each id maps to exactly one blob.
    """
    import re
    clean = re.sub(r'[^a-zA-Z0-9_]', '_', blob_name)
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean[:128]


def _blob_name_from_record(rec: dict) -> str:
    """Recover the GCS blob path from a manifest record.

    The manifest records embed the blob path in several places. We try
    in order:
      1. content.uri  (gs://bucket/path/to/file.pdf)
      2. structData.gcs_uri
      3. structData.source_uri (only if it's a gs:// URL)

    Returns the bare blob name (without the gs://bucket/ prefix) so it
    matches what extract_project() and _extract_property() expect.

    Defensive: tolerates structData being a stringified-JSON blob (legacy
    manifest records from before the structData-vs-jsonData fix) or even
    missing entirely.
    """
    content = rec.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    uri = content.get("uri", "") or ""

    sd = rec.get("structData")
    # Tolerate legacy records where structData was uploaded as a JSON
    # string instead of a real object. Parse if possible; otherwise
    # treat as empty.
    if isinstance(sd, str):
        try:
            sd = json.loads(sd)
        except Exception:
            sd = {}
    if not isinstance(sd, dict):
        sd = {}

    if not uri:
        uri = sd.get("gcs_uri", "") or ""
    if not uri:
        cand = sd.get("source_uri", "") or ""
        if isinstance(cand, str) and cand.startswith("gs://"):
            uri = cand
    if not uri.startswith("gs://"):
        return ""
    # Strip 'gs://<bucket>/' to leave just the object name
    parts = uri.split("/", 3)
    if len(parts) < 4:
        return ""
    return parts[3]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually rewrite the manifest and trigger Vertex import. "
             "Without this flag, the script previews changes only.",
    )
    parser.add_argument(
        "--manifest-name", default="manifests/import_manifest_latest.jsonl",
        help="Path to the live manifest in the bucket "
             "(default: manifests/import_manifest_latest.jsonl)",
    )
    parser.add_argument(
        "--sample-changes", type=int, default=5,
        help="How many BK-field changes to print as a sample (default 5).",
    )
    args = parser.parse_args()

    # Sanity check that extract_project actually imported.
    if extract_project is None:
        print(f"FATAL: black_knight_extractor failed to import.")
        print(f"  _BK_IMPORT_ERROR = {_BK_IMPORT_ERROR!r}")
        print("  Cannot proceed -- fix the import first.")
        sys.exit(1)

    cfg = load_config()
    gcs = storage_client(cfg)
    bucket = gcs.bucket(cfg.bucket)

    src_blob = bucket.blob(args.manifest_name)
    if not src_blob.exists():
        print(f"FATAL: manifest not found at gs://{cfg.bucket}/{args.manifest_name}")
        sys.exit(1)
    src_blob.reload()
    print(f"Source manifest: gs://{cfg.bucket}/{args.manifest_name}")
    print(f"  Size: {src_blob.size:,} bytes")
    print(f"  Last modified: {src_blob.updated}")
    print()

    print("Downloading manifest...")
    t0 = time.time()
    raw = src_blob.download_as_text()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    print(f"  {len(lines):,} records ({time.time() - t0:.1f}s download)")
    print()

    # Build a doc_id -> blob_name reverse lookup by walking the GCS bucket.
    # Needed because many records in the legacy `jsonData` format don't
    # carry the blob path in any readable field -- but their `id` field
    # was derived from the blob path via _make_doc_id, so we can reverse
    # it by enumerating known blobs and sanitizing each name.
    print("Building doc_id -> blob_name lookup from GCS...")
    t0 = time.time()
    doc_id_to_blob: dict[str, str] = {}
    blob_count = 0
    for blob in bucket.list_blobs(prefix="onedrive-mirror/"):
        blob_count += 1
        if blob_count % 2000 == 0:
            print(f"  {blob_count:,} blobs scanned...")
        doc_id = _make_doc_id(blob.name)
        if doc_id in doc_id_to_blob:
            # Same as onedrive_sync's collision handling -- duplicate IDs
            # get a numeric suffix. We can't perfectly reverse the suffix
            # rule here, so just keep the first.
            continue
        doc_id_to_blob[doc_id] = blob.name
    print(f"  {blob_count:,} blobs scanned, {len(doc_id_to_blob):,} unique IDs ({time.time() - t0:.1f}s)")
    print()

    bk_fields = {
        "project_id", "person_name", "address", "project_kind",
        "project_year", "project_status", "encircle_job_id",
    }

    # Process every record.
    output_lines: list[str] = []
    stats = {
        "total":           0,
        "no_blob_name":    0,
        "bk_added":        0,    # had no BK fields before, has some after
        "bk_changed":      0,    # had BK fields before, different after
        "bk_unchanged":    0,    # had BK fields before, same after (or both empty)
        "extractor_match": 0,    # extract_project returned a non-trivial rule
    }
    samples: list[tuple[str, dict, dict]] = []
    no_blob_samples: list[dict] = []   # for debugging the no-blob-path case

    print("Processing records...")
    t0 = time.time()
    for i, line in enumerate(lines):
        if i % 1000 == 0 and i > 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(lines) - i) / rate if rate > 0 else 0
            print(f"  {i:,}/{len(lines):,} ({rate:.0f}/s, ~{eta:.0f}s left)")

        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"  WARNING: line {i+1} not valid JSON ({e}); preserving as-is")
            output_lines.append(line)
            continue

        stats["total"] += 1
        blob_name = _blob_name_from_record(rec)
        # FALLBACK: legacy records (jsonData format) often don't carry the
        # blob path in any readable field. Use the doc_id -> blob_name
        # reverse lookup we built from the GCS bucket walk.
        if not blob_name:
            doc_id = rec.get("id", "") or ""
            blob_name = doc_id_to_blob.get(doc_id, "")
        if not blob_name:
            # Likely a photo_pointer doc -- no source GCS file, no BK enrichment.
            stats["no_blob_name"] += 1
            # Capture a few samples for debugging.
            if len(no_blob_samples) < 5:
                # Show the keys present at top level + structData keys
                sd_peek = rec.get("structData")
                if isinstance(sd_peek, str):
                    try:
                        sd_peek = json.loads(sd_peek)
                    except Exception:
                        sd_peek = {"__type__": "string"}
                if not isinstance(sd_peek, dict):
                    sd_peek = {}
                no_blob_samples.append({
                    "top_keys": sorted(rec.keys()),
                    "id": rec.get("id", "")[:60],
                    "sd_keys": sorted(sd_peek.keys())[:10],
                    "sd_title": str(sd_peek.get("title", ""))[:60],
                    "sd_source_uri": str(sd_peek.get("source_uri", ""))[:80],
                    "sd_gcs_uri": str(sd_peek.get("gcs_uri", ""))[:80],
                    "content_keys": sorted((rec.get("content") or {}).keys()) if isinstance(rec.get("content"), dict) else ["__not_dict__"],
                })
            output_lines.append(line)
            continue

        sd_before = rec.get("structData")
        # Same tolerance as _blob_name_from_record: structData may be a
        # stringified JSON blob in legacy records. Parse if possible.
        if isinstance(sd_before, str):
            try:
                sd_before = json.loads(sd_before)
            except Exception:
                sd_before = {}
        if not isinstance(sd_before, dict):
            sd_before = {}

        # MIGRATION: legacy records (~79% of this manifest as of 2026-05-27)
        # were written with `jsonData` (a stringified JSON object) instead
        # of `structData` (a real object). Vertex only indexes structData
        # for filtering. If we find a record with jsonData and no useful
        # structData, parse the jsonData and promote it to structData.
        # See [[BusinessBrain/10 Projects/Black Knight Filtering Works -
        # 2026-05-26]] for the original diagnosis.
        if not sd_before:
            json_data = rec.get("jsonData")
            if isinstance(json_data, str) and json_data:
                try:
                    parsed = json.loads(json_data)
                    if isinstance(parsed, dict):
                        sd_before = parsed
                except Exception:
                    pass
            elif isinstance(json_data, dict):
                # Rare case: jsonData was stored as a dict directly
                sd_before = json_data

        before_bk = {k: sd_before.get(k) for k in bk_fields if k in sd_before}

        # Re-run enrich_metadata on the blob name. Pass the existing
        # structData as base_struct so enrich_metadata's other side effects
        # (property, document_type, doc_date) remain consistent with what
        # the live builder would write. The key effect we care about is
        # that the BK block at the end of enrich_metadata now populates.
        try:
            sd_after = enrich_metadata(blob_name, dict(sd_before))
        except Exception as e:
            print(f"  WARNING: enrich_metadata failed on {blob_name!r}: {e}")
            output_lines.append(line)
            continue

        # Defensive: enrich_metadata may overwrite doc_type to "document"
        # via its filename-only classifier if our existing doc_type was
        # set by the content classifier (Layer 2). Preserve the existing
        # doc_type / document_type if they were already set.
        for preserve in ("doc_type", "document_type"):
            if preserve in sd_before and sd_before[preserve] and sd_before[preserve] != "document":
                sd_after[preserve] = sd_before[preserve]

        after_bk = {k: sd_after.get(k) for k in bk_fields if k in sd_after}

        if before_bk == after_bk:
            stats["bk_unchanged"] += 1
        elif not before_bk and after_bk:
            stats["bk_added"] += 1
            if after_bk.get("project_id") or after_bk.get("address"):
                stats["extractor_match"] += 1
            if len(samples) < args.sample_changes:
                samples.append((blob_name, before_bk, after_bk))
        else:
            stats["bk_changed"] += 1
            if len(samples) < args.sample_changes:
                samples.append((blob_name, before_bk, after_bk))

        rec["structData"] = sd_after
        output_lines.append(json.dumps(rec))

    elapsed = time.time() - t0
    print(f"  {len(lines):,} records processed in {elapsed:.1f}s")
    print()

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total records:             {stats['total']:>7,}")
    print(f"  Records with no blob path: {stats['no_blob_name']:>7,}  (photo pointers, etc.)")
    print(f"  BK fields ADDED:           {stats['bk_added']:>7,}  (had none, now has some)")
    print(f"  BK fields CHANGED:         {stats['bk_changed']:>7,}  (had some, now different)")
    print(f"  BK fields UNCHANGED:       {stats['bk_unchanged']:>7,}")
    print(f"  Extractor matches:         {stats['extractor_match']:>7,}  (rules 1-8 actually fired)")
    print()

    if samples:
        print("Sample changes (first {} of {}):".format(
            len(samples), stats["bk_added"] + stats["bk_changed"]
        ))
        for blob_name, before, after in samples:
            print(f"  {blob_name}")
            print(f"    before: {before}")
            print(f"    after:  {after}")
            print()

    if no_blob_samples:
        print(f"Sample 'no blob path' records (first {len(no_blob_samples)} of {stats['no_blob_name']}):")
        for s in no_blob_samples:
            print(f"  id: {s['id']}")
            print(f"    top-level keys: {s['top_keys']}")
            print(f"    content keys:   {s['content_keys']}")
            print(f"    structData keys: {s['sd_keys']}")
            print(f"    structData.title:      {s['sd_title']!r}")
            print(f"    structData.source_uri: {s['sd_source_uri']!r}")
            print(f"    structData.gcs_uri:    {s['sd_gcs_uri']!r}")
            print()

    if not args.apply:
        print("DRY RUN -- no changes uploaded.")
        print("Re-run with --apply to upload the patched manifest and trigger")
        print("a Vertex re-import.")
        return

    # Apply
    total_changes = stats["bk_added"] + stats["bk_changed"]
    if total_changes == 0:
        print("No changes to apply. Exiting without uploading or re-importing.")
        return

    new_body = "\n".join(output_lines)
    print(f"Uploading patched manifest ({len(new_body):,} bytes)...")
    src_blob.upload_from_string(new_body, content_type="application/json")
    print(f"  Uploaded to gs://{cfg.bucket}/{args.manifest_name}")
    print()

    # Trigger Vertex import via REST (same endpoint onedrive_sync.py uses).
    # We replicate the call here instead of importing onedrive_sync to keep
    # this script standalone.
    print("Triggering Vertex AI Search re-import (FULL reconciliation)...")
    import requests
    import google.auth
    import google.auth.transport.requests

    project_id = os.environ.get("GCP_PROJECT_ID", "") or cfg.project_id
    datastore  = os.environ.get("VERTEX_DATASTORE_ID", "") or cfg.data_store_id
    location   = os.environ.get("VERTEX_LOCATION", "global")

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())

    url = (
        f"https://discoveryengine.googleapis.com/v1alpha/projects/{project_id}"
        f"/locations/{location}/collections/default_collection"
        f"/dataStores/{datastore}/branches/0/documents:import"
    )
    body = {
        "gcsSource": {
            "inputUris": [f"gs://{cfg.bucket}/{args.manifest_name}"],
            "dataSchema": "document",
        },
        "reconciliationMode": "FULL",
    }
    r = requests.post(
        url,
        headers={
            "Authorization":          f"Bearer {creds.token}",
            "X-Goog-User-Project":    project_id,
            "Content-Type":           "application/json",
        },
        json=body,
    )
    if r.status_code == 200:
        op_name = r.json().get("name", "")
        print(f"  Import triggered. Operation: {op_name}")
        print()
        print("Vertex will process the import server-side (typically 5-15 minutes).")
        print("Once complete, BK fields will be filterable on every doc that matched")
        print("one of the 8 Black Knight extractor rules.")
    else:
        print(f"  Import FAILED: {r.status_code}")
        print(f"  Response: {r.text[:500]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
