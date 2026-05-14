#!/usr/bin/env python3
"""
Quick diagnostic: just count what's actually in the GCS bucket and what
the LocalFileIndex sees. No audit logic, no extraction, no opinions.

Run from repo root:
    python scripts/count_corpus.py
"""
from __future__ import annotations
import os, sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "Phase5_oneDrive"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

from google.cloud import storage

BUCKET = os.environ.get("GCS_BUCKET_NAME") or os.environ.get("GCS_BUCKET_RAW", "")
PROJECT = os.environ.get("GCP_PROJECT_ID", "")

print(f"Bucket:  {BUCKET}")
print(f"Project: {PROJECT}")
print()

client = storage.Client(project=PROJECT)
bucket = client.bucket(BUCKET)

# Count EVERY blob, no prefix filter, no extension filter.
print("Walking entire bucket (no prefix, no extension filter)...")
top_prefixes = Counter()
ext_counts = Counter()
total = 0
total_bytes = 0
for blob in bucket.list_blobs():
    total += 1
    total_bytes += blob.size or 0
    parts = blob.name.split("/", 1)
    top_prefixes[parts[0] + "/"] += 1
    ext = Path(blob.name).suffix.lower()
    ext_counts[ext or "(no ext)"] += 1
    if total % 5000 == 0:
        print(f"  {total:>7,} blobs walked so far...")

print()
print(f"TOTAL blobs in bucket:  {total:,}")
print(f"TOTAL size:             {total_bytes / 1e9:.2f} GB")
print()
print("Top-level prefixes:")
for prefix, n in top_prefixes.most_common(20):
    print(f"  {n:>7,}   {prefix}")
print()
print("Extensions (top 20):")
for ext, n in ext_counts.most_common(20):
    print(f"  {n:>7,}   {ext}")
print()

# Now show what LocalFileIndex finds
print("=" * 60)
print("Checking LocalFileIndex...")
from local_index import LocalFileIndex
idx = LocalFileIndex()
idx.load()
print(f"LocalFileIndex file count: {len(idx._files):,}")
print(f"LocalFileIndex property folder count: {len(idx._property_folders):,}")
print()
print("First 5 indexed files:")
for norm_name, real_name, uri in idx._files[:5]:
    print(f"  {real_name}")
    print(f"    {uri}")
print()
if idx._property_folders:
    print(f"First 10 property folders:")
    for pf in sorted(idx._property_folders)[:10]:
        print(f"  {pf}")
