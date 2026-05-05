"""
Investigate why "106 madison avenue pdf" returns empty content.
Checks: GCS, manifest, Vertex index, extraction, fetcher.
"""
import os, sys, json, base64, io
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

from google.cloud import storage
from google.oauth2 import service_account
creds = service_account.Credentials.from_service_account_file(
    SA_KEY, scopes=["https://www.googleapis.com/auth/cloud-platform"])
gcs = storage.Client(project=PROJECT, credentials=creds)
bucket = gcs.bucket(BUCKET)

SEARCH_TERM = "106 madison"

print(f"Hunting for '{SEARCH_TERM}' across the system...\n")

# ── 1. Find matching files in GCS ──────────────────────────────────────────
print("=" * 70)
print(" 1) GCS BUCKET — files matching the search term")
print("=" * 70)
matches = []
for b in bucket.list_blobs(prefix="onedrive-mirror/"):
    if SEARCH_TERM.lower() in b.name.lower():
        matches.append(b)
print(f"  Found {len(matches)} matching files:")
for b in matches[:20]:
    print(f"    {b.name}  ({b.size:,} bytes)")

# ── 2. Look for them in the import manifest ────────────────────────────────
print()
print("=" * 70)
print(" 2) IMPORT MANIFEST — how were these files indexed?")
print("=" * 70)
manifest_blob = bucket.blob("manifests/import_manifest_repopulate.jsonl")
manifest_lines = manifest_blob.download_as_bytes().decode("utf-8").splitlines()

manifest_matches = []
for line in manifest_lines:
    try:
        entry = json.loads(line)
        jdata = json.loads(entry.get("jsonData", "{}"))
        title = jdata.get("title", "").lower()
        if SEARCH_TERM.lower() in title:
            manifest_matches.append(entry)
    except Exception:
        continue

print(f"  Found {len(manifest_matches)} matching manifest entries.\n")
for entry in manifest_matches[:5]:
    jdata = json.loads(entry.get("jsonData", "{}"))
    content = entry.get("content", {})
    title = jdata.get("title", "?")
    mime = content.get("mimeType", "?")
    has_rawBytes = bool(content.get("rawBytes"))
    has_uri = bool(content.get("uri"))
    is_pointer = jdata.get("document_type") == "large_pdf_pointer"
    size_mb = jdata.get("size_mb", "?")

    print(f"  ── {title}")
    print(f"      mimeType:        {mime}")
    print(f"      has rawBytes:    {has_rawBytes}")
    print(f"      has uri:         {has_uri}")
    print(f"      is large pointer:{is_pointer} (size {size_mb} MB)" if is_pointer else "")
    if has_rawBytes:
        rb = content["rawBytes"]
        decoded = base64.b64decode(rb).decode("utf-8", errors="replace")
        print(f"      content length:  {len(decoded)} chars")
        if len(decoded) < 500:
            print(f"      content sample:  {decoded[:300]!r}")
        else:
            print(f"      content sample:  {decoded[:300]!r}...")
    print()

# ── 3. Try to extract the PDF directly using current logic ────────────────
if matches:
    print("=" * 70)
    print(" 3) DIRECT EXTRACTION TEST — pull from GCS and run pdfplumber")
    print("=" * 70)

    pdf_match = next((m for m in matches if m.name.lower().endswith(".pdf")), None)
    if pdf_match:
        print(f"  Trying file: {pdf_match.name}")
        print(f"  Size: {pdf_match.size:,} bytes ({pdf_match.size/1024/1024:.2f} MB)")

        try:
            raw = pdf_match.download_as_bytes()
            print(f"  Downloaded: {len(raw):,} bytes\n")

            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(raw)) as pdf:
                    page_count = len(pdf.pages)
                    print(f"  pdfplumber opened — {page_count} pages")

                    total_text = ""
                    pages_with_text = 0
                    pages_empty = 0
                    for i, page in enumerate(pdf.pages, 1):
                        try:
                            txt = page.extract_text() or ""
                            if txt.strip():
                                pages_with_text += 1
                                total_text += txt + "\n"
                            else:
                                pages_empty += 1
                        except Exception as pe:
                            print(f"    page {i} error: {pe}")

                    print(f"  Pages with text: {pages_with_text}")
                    print(f"  Pages empty:     {pages_empty}")
                    print(f"  Total text:      {len(total_text)} chars")
                    if total_text:
                        print(f"\n  First 500 chars:")
                        print(f"  {total_text[:500]!r}")
                    else:
                        print(f"\n  PDF EXTRACTED EMPTY — likely scanned (image-only). Needs OCR.")
            except Exception as ee:
                print(f"  pdfplumber failed: {ee}")

        except Exception as e:
            print(f"  Download failed: {e}")
    else:
        print("  No PDF in matched files (only non-PDF formats present)")

# ── 4. Try the actual fetcher used by chat ─────────────────────────────────
print()
print("=" * 70)
print(" 4) RUNTIME FETCHER TEST — does get_document_by_name work?")
print("=" * 70)
try:
    from vertex.document_fetch import get_document_by_name
    result = get_document_by_name("106 madison avenue")
    print(f"  ok:         {result.get('ok')}")
    print(f"  title:      {result.get('title')}")
    print(f"  uri:        {result.get('uri')}")
    print(f"  text len:   {len(result.get('text') or '')}")
    if result.get("error"):
        print(f"  error:      {result['error']}")
    if result.get("candidates"):
        print(f"  candidates: {result['candidates']}")
    text = result.get("text") or ""
    if text:
        print(f"\n  First 400 chars of text:")
        print(f"  {text[:400]!r}")
except Exception as e:
    print(f"  Fetcher errored: {type(e).__name__}: {e}")

print()
print("=" * 70)
print(" Verdict")
print("=" * 70)
print("""
- If GCS shows the file but the manifest entry has rawBytes empty:
    The original repopulate run failed extraction on this file. Re-extract.

- If pdfplumber extracts 0 chars: scanned PDF, needs OCR (Document AI).

- If the file is large (>8 MB): repopulate marked it as large_pdf_pointer
    with no extracted content. We need to extract those too.

- If runtime fetcher returns text but chat says empty: search-side problem.
""")
