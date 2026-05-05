"""
One-shot diagnostic. Run this once. It tells us where the data actually is.

    python scripts/diagnose_index.py

No questions, no interactive prompts. It prints:
  - How many files are in GCS
  - How many docs Vertex has
  - Sample file names from each
  - Whether they match
"""
from __future__ import annotations
import os, sys
from pathlib import Path
from collections import Counter

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")
load_dotenv(REPO / "Phase3_Bootstrap" / "secrets" / ".env")

PROJECT       = os.getenv("GCP_PROJECT_ID", "")
LOCATION      = os.getenv("GCP_LOCATION", "global")
BUCKET        = os.getenv("GCS_BUCKET_NAME") or os.getenv("GCS_BUCKET_RAW", "")
DATA_STORE_ID = os.getenv("VERTEX_DATA_STORE_ID") or os.getenv("VERTEX_DATASTORE_ID", "")
SA_KEY        = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or str(
    REPO / "Phase3_Bootstrap" / "secrets" / "service-account.json")

print("=" * 70)
print(" DIAGNOSTIC — what is actually in your GCS and Vertex data store")
print("=" * 70)
print(f"  Project       : {PROJECT}")
print(f"  Bucket        : {BUCKET}")
print(f"  Data store    : {DATA_STORE_ID}")
print(f"  SA key        : {SA_KEY}")
print()

# ── 1. GCS — count files ────────────────────────────────────────────────────
print("─" * 70)
print(" 1) GCS BUCKET — what files have actually been synced")
print("─" * 70)

try:
    from google.cloud import storage
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        SA_KEY, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    client = storage.Client(project=PROJECT, credentials=creds)
    bucket = client.bucket(BUCKET)

    blobs = list(bucket.list_blobs(prefix="onedrive-mirror/"))
    print(f"  onedrive-mirror/ blobs : {len(blobs)}")

    # Also count anything else (DoorLoop sync may have used a different prefix)
    all_blobs = list(bucket.list_blobs())
    print(f"  total blobs in bucket  : {len(all_blobs)}")

    # Group by top-level prefix
    prefix_counts = Counter()
    for b in all_blobs:
        top = b.name.split("/")[0] if "/" in b.name else "(root)"
        prefix_counts[top] += 1
    print(f"  by top-level folder    :")
    for prefix, count in sorted(prefix_counts.items(), key=lambda x: -x[1]):
        print(f"      {prefix:<35s} {count:>6d}")

    # Sample 10 file names from onedrive-mirror
    print(f"\n  sample onedrive-mirror/ files (first 10):")
    for b in blobs[:10]:
        print(f"      {b.name}  ({b.size or 0:,} bytes)")
    if len(blobs) > 10:
        print(f"      ... and {len(blobs) - 10} more")

except Exception as e:
    print(f"  ERROR reading GCS: {e}")

# ── 2. Vertex — count documents ─────────────────────────────────────────────
print()
print("─" * 70)
print(" 2) VERTEX DATA STORE — how many docs are actually indexed")
print("─" * 70)

try:
    from google.cloud import discoveryengine_v1 as de
    from google.api_core.client_options import ClientOptions

    api_endpoint = (f"{LOCATION}-discoveryengine.googleapis.com"
                    if LOCATION != "global"
                    else "discoveryengine.googleapis.com")
    opts = ClientOptions(api_endpoint=api_endpoint)
    doc_client = de.DocumentServiceClient(credentials=creds, client_options=opts)

    parent = (f"projects/{PROJECT}/locations/{LOCATION}/"
              f"collections/default_collection/dataStores/{DATA_STORE_ID}/"
              f"branches/default_branch")

    docs = []
    # SDK compatibility: try keyword first, fall back to ListDocumentsRequest
    try:
        for d in doc_client.list_documents(parent=parent):
            docs.append(d)
            if len(docs) >= 2000:
                break
    except Exception as inner_e:
        # Some SDK versions need an explicit request object
        try:
            req = de.ListDocumentsRequest(parent=parent)
            for d in doc_client.list_documents(request=req):
                docs.append(d)
                if len(docs) >= 2000:
                    break
        except Exception as inner2:
            raise inner2

    print(f"  documents indexed      : {len(docs)}")

    # Sample 10 titles + struct_data
    print(f"\n  sample indexed docs (first 10):")
    for d in docs[:10]:
        try:
            sd = dict(d.struct_data) if d.struct_data else {}
        except Exception:
            sd = {}
        title = sd.get("title", "(no title)")
        src = sd.get("source_uri", sd.get("gcs_uri", "(no uri)"))
        print(f"      id={d.id[:50]:<50s} title={str(title)[:50]}")

    if not docs:
        print("\n  *** DATA STORE IS EMPTY ***")
        print("  This is the structural problem. The Vertex index has zero docs.")
        print("  The fix is to re-run a full OneDrive sync to repopulate it.")

except Exception as e:
    print(f"  ERROR reading Vertex documents: {e}")

# ── 3. Verdict ──────────────────────────────────────────────────────────────
print()
print("=" * 70)
print(" VERDICT")
print("=" * 70)
try:
    if not docs:
        print("  Vertex data store is EMPTY. Searches will return 0 results no")
        print("  matter what you ask. Re-run the OneDrive sync to re-populate.")
        print()
        print("  Next step:")
        print("    cd Phase5_oneDrive")
        print("    python onedrive_sync.py --rebuild-only")
        print()
        print("  (rebuild-only is fastest if files are still in GCS — it skips")
        print("  the OneDrive download and just rebuilds the Vertex index from")
        print("  what is already in the bucket.)")
    elif len(docs) < 50:
        print(f"  Only {len(docs)} docs indexed. That's well below normal for a")
        print("  full OneDrive. The last sync probably failed partway through.")
        print("  Re-run sync to repair:")
        print("    cd Phase5_oneDrive && python onedrive_sync.py --rebuild-only")
    else:
        print(f"  {len(docs)} docs indexed and {len(blobs)} files in GCS.")
        print("  Index looks populated. The issue is elsewhere — most likely the")
        print("  Vertex search-quota cooldown. Wait 60 min and retest.")
except NameError:
    print("  Could not complete diagnostic — see errors above.")
print()
