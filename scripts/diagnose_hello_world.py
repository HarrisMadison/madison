"""
Hello-world delta-sync test diagnostic.

Probes ALL THREE layers for a test file:
  1. GCS bucket (was the file uploaded?)
  2. Vertex AI Search index (was it imported?)
  3. Local in-memory index (does the running Flask server know about it?)

Usage:
    python scripts/diagnose_hello_world.py "hello-world-claude-test.docx"
    python scripts/diagnose_hello_world.py "hello-world"   # fuzzy match

The third argument can be omitted for a default search of "hello world".
"""
from __future__ import annotations
import os, sys, json, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

PROJECT       = os.getenv("GCP_PROJECT_ID", "")
LOCATION      = os.getenv("GCP_LOCATION", "global")
BUCKET        = os.getenv("GCS_BUCKET_NAME") or os.getenv("GCS_BUCKET_RAW", "")
DATA_STORE_ID = os.getenv("VERTEX_DATA_STORE_ID") or os.getenv("VERTEX_DATASTORE_ID", "")
SA_KEY        = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or str(
    REPO / "Phase3_Bootstrap" / "secrets" / "service-account.json")

QUERY = sys.argv[1] if len(sys.argv) > 1 else "hello world"
QUERY_LOWER = QUERY.lower()

print("=" * 70)
print(f" DELTA-SYNC TEST DIAGNOSTIC — searching for: {QUERY!r}")
print("=" * 70)
print()

# ── 1. GCS ─────────────────────────────────────────────────────────────────
print("─" * 70)
print(" 1) GCS bucket — does the file exist in onedrive-mirror/?")
print("─" * 70)

found_in_gcs = []
try:
    from google.cloud import storage
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        SA_KEY, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    gcs = storage.Client(project=PROJECT, credentials=creds)
    bucket = gcs.bucket(BUCKET)

    for blob in bucket.list_blobs(prefix="onedrive-mirror/"):
        if QUERY_LOWER in blob.name.lower():
            found_in_gcs.append(blob)

    if found_in_gcs:
        print(f"  ✓ Found {len(found_in_gcs)} matching blob(s):")
        for b in found_in_gcs[:10]:
            print(f"      {b.name}  ({b.size:,} bytes, updated {b.updated})")
    else:
        print(f"  ✗ NO blobs in onedrive-mirror/ contain {QUERY_LOWER!r}.")
        print(f"    Either the OneDrive sync hasn't uploaded it yet, or the")
        print(f"    file isn't in your OneDrive sync source.")
except Exception as e:
    print(f"  GCS check failed: {e}")

# ── 2. Vertex ──────────────────────────────────────────────────────────────
print()
print("─" * 70)
print(" 2) Vertex data store — was it imported?")
print("─" * 70)

found_in_vertex = []
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

    # Walk the document list looking for our query string
    count = 0
    for d in doc_client.list_documents(parent=parent):
        count += 1
        try:
            sd = dict(d.struct_data) if d.struct_data else {}
        except Exception:
            sd = {}
        title = str(sd.get("title", "")).lower()
        if QUERY_LOWER in title:
            found_in_vertex.append({
                "id":     d.id,
                "title":  sd.get("title", ""),
                "uri":    sd.get("source_uri", sd.get("gcs_uri", "")),
            })
        if count > 15000:
            break

    print(f"  Walked {count} indexed documents")
    if found_in_vertex:
        print(f"  ✓ Found {len(found_in_vertex)} matching doc(s) in Vertex:")
        for d in found_in_vertex[:10]:
            print(f"      {d['title']}")
            print(f"        id={d['id'][:60]}")
            print(f"        uri={d['uri'][:80]}")
    else:
        print(f"  ✗ No Vertex documents contain {QUERY_LOWER!r} in title.")
        print(f"    Either the import hasn't completed yet (wait ~60s after sync),")
        print(f"    or the manifest didn't include this file.")
except Exception as e:
    print(f"  Vertex check failed: {e}")

# ── 3. Local index ─────────────────────────────────────────────────────────
print()
print("─" * 70)
print(" 3) Local in-memory index — would the running chat find it?")
print("─" * 70)
print(" (NOTE: this loads a FRESH local index, NOT the one in simple_web.")
print("  Even if this passes, the running Flask server may have a stale")
print("  index until you POST /api/admin/reload-index)")
print()

try:
    sys.path.insert(0, str(REPO / "scripts"))
    from local_index import LocalFileIndex
    idx = LocalFileIndex()
    idx.load()

    hits = idx.find(QUERY, top_n=10)
    if hits:
        print(f"  ✓ Local index would match {len(hits)} file(s):")
        for h in hits[:5]:
            marker = " ✓ STRONG" if h["score"] >= 100 else ""
            print(f"      score={h['score']:7.1f}  {h['name']}{marker}")
    else:
        print(f"  ✗ Local index has no matches for {QUERY!r}.")
except Exception as e:
    print(f"  Local index check failed: {e}")

# ── 4. Live Flask reload status ────────────────────────────────────────────
print()
print("─" * 70)
print(" 4) Running Flask server — is its in-memory index fresh?")
print("─" * 70)

try:
    req = urllib.request.Request("http://localhost:5000/api/admin/reload-index",
                                  method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        result = json.loads(body)
        if result.get("ok"):
            print(f"  ✓ Reload endpoint OK. Server now has {result['file_count']:,} files in index.")
            print(f"    (Reload took {result['elapsed_seconds']}s.)")
        else:
            print(f"  ✗ Reload returned error: {result.get('error')}")
except urllib.error.HTTPError as e:
    print(f"  HTTP error from Flask: {e.code} {e.reason}")
except urllib.error.URLError as e:
    print(f"  Could not reach Flask: {e.reason}")
    print(f"    (Is simple_web.py running?)")
except Exception as e:
    print(f"  Reload check failed: {type(e).__name__}: {e}")

# ── Verdict ────────────────────────────────────────────────────────────────
print()
print("=" * 70)
print(" VERDICT")
print("=" * 70)

g = bool(found_in_gcs)
v = bool(found_in_vertex)

if g and v:
    print("  All three layers see the file. The chat should be able to answer")
    print("  questions about it. Try asking Bob about it.")
elif g and not v:
    print("  GCS has the file but Vertex hasn't indexed it yet. Either:")
    print("    - Wait 30-60 more seconds for the Vertex import to finish, or")
    print("    - The import wasn't triggered (check the sync log for 'Vertex import")
    print("      triggered').")
elif not g and not v:
    print("  The file isn't in GCS yet. The OneDrive sync either hasn't run,")
    print("  didn't pick it up, or is still in progress.")
elif not g and v:
    print("  Strange: file appears in Vertex but not GCS. Check the bucket name")
    print("  and prefix in your env vars.")
print()
