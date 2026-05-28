"""One-shot diagnostic: figure out where the legacy `jsonData` records in the
live manifest came from.

We download the current manifest, group records by which envelope format
they use (structData vs jsonData vs neither), then for each group dump:
  - count
  - sample of 5 IDs
  - distribution of top-level path segments (so we can see if e.g. all
    jsonData records are in /Claims - Closed/ vs spread evenly)

This answers the question: did `onedrive_sync.py` write the jsonData records,
or are they coming from somewhere else?
"""
import _path  # noqa: F401
import json
import sys
from collections import Counter

from core import load_config, storage_client

cfg = load_config()
gcs = storage_client(cfg)
bucket = gcs.bucket(cfg.bucket)

src_blob = bucket.blob("manifests/import_manifest_latest.jsonl")
print(f"Reading gs://{cfg.bucket}/manifests/import_manifest_latest.jsonl")
src_blob.reload()
print(f"  Last modified: {src_blob.updated}")
print(f"  Size: {src_blob.size:,} bytes")
print()

raw = src_blob.download_as_text()
lines = [ln for ln in raw.splitlines() if ln.strip()]
print(f"Total records: {len(lines):,}")
print()

# Categorize
buckets = {
    "structData_only":      [],   # has structData, no jsonData
    "jsonData_only":        [],   # has jsonData, no structData
    "both":                 [],
    "neither":              [],
}

for i, line in enumerate(lines):
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    has_sd = bool(rec.get("structData"))
    has_jd = bool(rec.get("jsonData"))
    if has_sd and has_jd:
        buckets["both"].append((i, rec))
    elif has_sd:
        buckets["structData_only"].append((i, rec))
    elif has_jd:
        buckets["jsonData_only"].append((i, rec))
    else:
        buckets["neither"].append((i, rec))

for name, recs in buckets.items():
    print(f"=== {name}: {len(recs):,} records ===")
    if not recs:
        print()
        continue

    # Sample 5 IDs
    for i, rec in recs[:5]:
        # Determine what kind of doc this is
        doc_id = rec.get("id", "")[:80]
        content = rec.get("content") or {}
        content_keys = sorted(content.keys()) if isinstance(content, dict) else "non-dict"
        mime = content.get("mimeType", "") if isinstance(content, dict) else ""
        has_content_uri = bool(content.get("uri")) if isinstance(content, dict) else False
        has_rawbytes = bool(content.get("rawBytes")) if isinstance(content, dict) else False
        print(f"  [line {i+1}] id={doc_id}")
        print(f"    content keys: {content_keys}")
        print(f"    mimeType: {mime!r}")
        print(f"    has content.uri: {has_content_uri}")
        print(f"    has content.rawBytes: {has_rawbytes}")

        # Try to find a path
        sd = rec.get("structData")
        if isinstance(sd, str):
            try: sd = json.loads(sd)
            except: sd = {}
        if not isinstance(sd, dict): sd = {}
        jd = rec.get("jsonData")
        if isinstance(jd, str):
            try: jd = json.loads(jd)
            except: jd = {}
        if not isinstance(jd, dict): jd = {}

        print(f"    structData keys: {sorted(sd.keys())[:8] if sd else '(none)'}")
        print(f"    jsonData keys:   {sorted(jd.keys())[:8] if jd else '(none)'}")
        if sd.get("title"):
            print(f"    structData.title: {sd['title'][:60]!r}")
        if jd.get("title"):
            print(f"    jsonData.title:   {jd['title'][:60]!r}")
        if sd.get("source_uri"):
            print(f"    structData.source_uri: {sd['source_uri'][:80]!r}")
        if jd.get("source_uri"):
            print(f"    jsonData.source_uri:   {jd['source_uri'][:80]!r}")
        print()

    # Top-level path distribution (from doc_id)
    print(f"  Top-level path prefix distribution:")
    prefixes = Counter()
    for _, rec in recs:
        doc_id = rec.get("id", "")
        # IDs look like onedrive_mirror_1_Claims_In_Progress_... ; take first 4 tokens
        tokens = doc_id.split("_")[:6]
        prefix = "_".join(tokens)
        prefixes[prefix] += 1
    for prefix, count in prefixes.most_common(10):
        print(f"    {count:>5,}  {prefix}")
    print()
