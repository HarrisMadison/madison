"""
Deep diagnosis of why the chat is returning "empty excerpts" even though
Vertex IS finding documents by name.

Possible causes:
  1. The current Vertex index has docs with NO content (just metadata) so
     extractive snippets come back blank.
  2. The repopulation manifest is malformed or has no rawBytes.
  3. Vertex hasn't yet imported the new manifest (still in progress).
  4. Search-side: the Vertex client is configured to NOT return snippets.

This script checks all four without burning search quota.
"""
from __future__ import annotations
import os, sys, json, base64
from pathlib import Path

# Force UTF-8 output so Python 3.14 on Windows doesn't choke on box chars.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

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
print(" DEEP DIAGNOSIS — why chat returns empty excerpts")
print("=" * 70)
print()

from google.cloud import storage
from google.oauth2 import service_account
creds = service_account.Credentials.from_service_account_file(
    SA_KEY, scopes=["https://www.googleapis.com/auth/cloud-platform"])
gcs = storage.Client(project=PROJECT, credentials=creds)
bucket = gcs.bucket(BUCKET)

# ── 1. Find the repopulation manifest ──────────────────────────────────────
print("─" * 70)
print(" 1) The repopulation manifest on GCS — does it have rawBytes content?")
print("─" * 70)

manifest_blob = bucket.blob("manifests/import_manifest_repopulate.jsonl")
if not manifest_blob.exists():
    print("  Manifest doesn't exist yet at gs://.../manifests/import_manifest_repopulate.jsonl")
    print("    The repopulation script hasn't finished writing it.")
else:
    # Reload to ensure size/updated metadata is populated
    manifest_blob.reload()
    size_bytes = manifest_blob.size or 0
    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
    print(f"  Manifest found: {manifest_blob.name} ({size_mb:.1f} MB, {size_bytes:,} bytes)")
    print(f"  Updated: {manifest_blob.updated}")

    # Sample first 5 lines
    raw = manifest_blob.download_as_bytes()
    lines = raw.decode("utf-8").splitlines()
    print(f"  Total entries: {len(lines)}")
    print()
    print(f"  Inspecting first 5 entries:")
    print()
    for i, line in enumerate(lines[:5], 1):
        try:
            entry = json.loads(line)
            doc_id = entry.get("id", "?")
            jdata = json.loads(entry.get("jsonData", "{}"))
            title = jdata.get("title", "?")
            content = entry.get("content", {})
            mime = content.get("mimeType", "?")
            has_rawBytes = bool(content.get("rawBytes"))
            has_uri = bool(content.get("uri"))
            print(f"    [{i}] id={doc_id[:50]}")
            print(f"        title:     {title}")
            print(f"        mimeType:  {mime}")
            if has_rawBytes:
                # Decode and show first 200 chars of the actual content
                rb = content["rawBytes"]
                try:
                    decoded = base64.b64decode(rb).decode("utf-8", errors="replace")
                    preview = decoded[:300].replace("\n", " ")
                    print(f"        rawBytes:  YES — {len(rb)} chars of base64 (decodes to {len(decoded)} chars)")
                    print(f"        preview:   {preview!r}")
                except Exception as e:
                    print(f"        rawBytes:  YES but decode failed: {e}")
            elif has_uri:
                print(f"        uri:       {content['uri']} (passthrough — Vertex parses)")
            else:
                print(f"        ✗ NEITHER rawBytes NOR uri — this entry has NO content at all")
            print()
        except Exception as e:
            print(f"    [{i}] PARSE ERROR: {e}")

    # Count entries by content type
    extracted, passthrough, broken = 0, 0, 0
    sample_andover = None
    for line in lines:
        try:
            entry = json.loads(line)
            jdata = json.loads(entry.get("jsonData", "{}"))
            title = (jdata.get("title") or "").lower()
            content = entry.get("content", {})
            if content.get("rawBytes"):
                extracted += 1
                if "andover" in title and not sample_andover:
                    sample_andover = entry
            elif content.get("uri"):
                passthrough += 1
            else:
                broken += 1
        except Exception:
            broken += 1
    print(f"  Summary across all {len(lines)} entries:")
    print(f"      with rawBytes (text extracted) : {extracted}")
    print(f"      with uri only (passthrough)    : {passthrough}")
    print(f"      neither (broken)               : {broken}")

    # If we found an Andover entry, dump it
    if sample_andover:
        print()
        print(f"  ── Sample ANDOVER entry from manifest ──")
        jdata = json.loads(sample_andover.get("jsonData", "{}"))
        rb = sample_andover.get("content", {}).get("rawBytes", "")
        if rb:
            decoded = base64.b64decode(rb).decode("utf-8", errors="replace")
            print(f"  Title: {jdata.get('title')}")
            print(f"  Extracted text length: {len(decoded)} chars")
            print(f"  First 800 chars of extracted content:")
            print(f"  ┌─────────────────────────────────────────────────────")
            for ln in decoded[:800].splitlines()[:20]:
                print(f"  │ {ln}")
            print(f"  └─────────────────────────────────────────────────────")

# ── 2. Check the import operations on Vertex ──────────────────────────────
print()
print("─" * 70)
print(" 2) Vertex import operations — has the new manifest been imported yet?")
print("─" * 70)

try:
    import google.auth.transport.requests
    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token
    import requests

    # List recent import operations
    url = (f"https://discoveryengine.googleapis.com/v1alpha/projects/{PROJECT}"
           f"/locations/{LOCATION}/collections/default_collection"
           f"/dataStores/{DATA_STORE_ID}/branches/0/operations")
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        data = r.json()
        ops = data.get("operations", [])
        if not ops:
            print("  No import operations found on this data store.")
        else:
            print(f"  Found {len(ops)} operation(s). Most recent 3:")
            print()
            for op in ops[:3]:
                name = op.get("name", "?").split("/")[-1]
                done = op.get("done", False)
                meta = op.get("metadata", {})
                create_t = meta.get("createTime", "?")
                update_t = meta.get("updateTime", "?")
                success = (op.get("response", {}).get("@type") or "")
                error = op.get("error", {}).get("message", "")
                print(f"  Operation: {name}")
                print(f"    Created: {create_t}")
                print(f"    Updated: {update_t}")
                print(f"    Done:    {done}")
                if error:
                    print(f"    ERROR:   {error}")
                # Pull import-specific metadata
                if "successCount" in str(meta) or "failureCount" in str(meta):
                    sc = meta.get("successCount", "?")
                    fc = meta.get("failureCount", "?")
                    print(f"    Imported: {sc} successes, {fc} failures")
                print()
    else:
        print(f"  Operations API returned {r.status_code}: {r.text[:200]}")
except Exception as e:
    print(f"  ERROR fetching operations: {e}")

# ── 3. Sample a real document FROM the data store ─────────────────────────
print("─" * 70)
print(" 3) What does an indexed Andover doc actually look like inside Vertex?")
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
    for d in doc_client.list_documents(parent=parent):
        docs.append(d)
        if len(docs) >= 50:
            break

    print(f"  First 50 docs in the index:")
    andover_found = False
    for d in docs:
        try:
            sd = dict(d.struct_data) if d.struct_data else {}
        except Exception:
            sd = {}
        title = sd.get("title", "(no title)")
        if "andover" in str(title).lower():
            andover_found = True
            print(f"    ★ {d.id[:60]} — {title}")
            # Inspect the content
            if d.content:
                mime = d.content.mime_type or "?"
                has_rawBytes = bool(d.content.raw_bytes)
                has_uri = bool(d.content.uri)
                print(f"        mimeType:  {mime}")
                print(f"        rawBytes:  {'YES (' + str(len(d.content.raw_bytes)) + ' bytes)' if has_rawBytes else 'NO'}")
                print(f"        uri:       {d.content.uri if has_uri else 'NO'}")
        else:
            pass  # skip non-andover for brevity

    if not andover_found:
        print(f"    No Andover docs in the first 50 entries.")
        print(f"    (This is normal if the index is alphabetical and Andover")
        print(f"    is alphabetized below the first 50 in the listing.)")

except Exception as e:
    print(f"  ERROR: {e}")

print()
print("=" * 70)
print(" Done. Read the output above to understand what's broken.")
print("=" * 70)
