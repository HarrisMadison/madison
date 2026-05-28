"""
Direct Vertex repopulation from GCS — no OneDrive, no FULL reconciliation.

Walks gs://<bucket>/onedrive-mirror/, builds an import manifest with
INCREMENTAL reconciliation, and triggers a Vertex import. That's it.

This is what onedrive_sync.py --rebuild-only would do, MINUS:
  - the OneDrive listing (which takes forever)
  - reconciliationMode FULL (which wipes existing docs)
  - photo pointer doc generation (those need OneDrive metadata)

Run:
    python scripts/repopulate_vertex_from_gcs.py
    python scripts/repopulate_vertex_from_gcs.py --dry-run    # see what it would do
    python scripts/repopulate_vertex_from_gcs.py --limit 100  # test with first 100 docs

Time: about 15-25 min for 10,000 files (GCS download + extract + manifest write).
The Vertex import that fires at the end is server-side and takes another 30-60 min
to fully populate the index — you can close the script after it returns and
check progress in the Cloud Console.
"""
from __future__ import annotations
import os, sys, json, base64, io, argparse, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
# Load env file — try repo root first, then phase3 secrets
for env_path in [REPO / ".env", REPO / "Phase3_Bootstrap" / "secrets" / ".env"]:
    if env_path.exists():
        load_dotenv(env_path)
        print(f"  Loaded env: {env_path}")
        break

PROJECT       = os.getenv("GCP_PROJECT_ID", "")
LOCATION      = os.getenv("GCP_LOCATION", "global")
BUCKET        = os.getenv("GCS_BUCKET_NAME") or os.getenv("GCS_BUCKET_RAW", "")
DATA_STORE_ID = os.getenv("VERTEX_DATA_STORE_ID") or os.getenv("VERTEX_DATASTORE_ID", "")
SA_KEY        = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or str(
    REPO / "Phase3_Bootstrap" / "secrets" / "service-account.json")

SEARCHABLE_EXTS = ('.pdf', '.docx', '.doc', '.xlsx', '.xls', '.csv', '.txt', '.pptx')
MIME_MAP = {
    '.pdf':  'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.doc':  'application/msword',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls':  'application/vnd.ms-excel',
    '.csv':  'text/csv',
    '.txt':  'text/plain',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}
LARGE_PDF_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_CHARS = 200_000


def make_doc_id(blob_name: str) -> str:
    import re
    clean = re.sub(r'[^a-zA-Z0-9_]', '_', blob_name)
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean[:128]


def extract_text(file_bytes: bytes, ext: str, name: str = "") -> str:
    """Pull plain text from a file. Returns '' on failure (Vertex will parse it)."""
    ext = ext.lower()
    try:
        if ext == ".pdf":
            try:
                import pdfplumber
                parts = []
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text() or ""
                        if t:
                            parts.append(t)
                        for table in page.extract_tables() or []:
                            for row in table:
                                cells = [c for c in (row or []) if c]
                                if cells:
                                    parts.append(" | ".join(str(c) for c in cells))
                return "\n".join(parts).strip()
            except Exception:
                return ""

        if ext == ".docx":
            try:
                from docx import Document
                doc = Document(io.BytesIO(file_bytes))
                parts = [p.text for p in doc.paragraphs if p.text]
                for table in doc.tables:
                    for row in table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            parts.append(" | ".join(cells))
                return "\n".join(parts).strip()
            except Exception:
                return ""

        if ext in (".xlsx", ".xls"):
            try:
                from openpyxl import load_workbook
                wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
                parts = []
                for sheet in wb.worksheets:
                    parts.append(f"[Sheet: {sheet.title}]")
                    for row in sheet.iter_rows(values_only=True):
                        cells = [str(c) for c in row if c is not None and str(c).strip()]
                        if cells:
                            parts.append(" | ".join(cells))
                return "\n".join(parts).strip()
            except Exception:
                return ""

        if ext in (".csv", ".txt"):
            try:
                return file_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                return ""

        if ext == ".pptx":
            try:
                from pptx import Presentation
                prs = Presentation(io.BytesIO(file_bytes))
                parts = []
                for i, slide in enumerate(prs.slides, 1):
                    parts.append(f"[Slide {i}]")
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text:
                            parts.append(shape.text)
                return "\n".join(parts).strip()
            except Exception:
                return ""
    except Exception:
        pass
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="build manifest but don't trigger import")
    parser.add_argument("--limit", type=int, default=0, help="process only first N files (testing)")
    parser.add_argument("--no-extract", action="store_true",
                        help="skip text extraction (faster, lets Vertex parse files itself)")
    args = parser.parse_args()

    print()
    print("=" * 70)
    print(" REPOPULATE VERTEX from existing GCS files")
    print("=" * 70)
    print(f"  Project       : {PROJECT}")
    print(f"  Bucket        : {BUCKET}")
    print(f"  Data store    : {DATA_STORE_ID}")
    print(f"  Mode          : {'dry-run' if args.dry_run else 'LIVE'}")
    print(f"  Extract text  : {'no (let Vertex parse)' if args.no_extract else 'yes (pdfplumber/docx/xlsx)'}")
    if args.limit:
        print(f"  Limit         : first {args.limit} files only")
    print()

    if not all([PROJECT, BUCKET, DATA_STORE_ID]):
        print("ERROR: PROJECT/BUCKET/DATA_STORE_ID missing from env. Aborting.")
        sys.exit(1)

    from google.cloud import storage
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        SA_KEY, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    gcs = storage.Client(project=PROJECT, credentials=creds)
    bucket = gcs.bucket(BUCKET)

    print("Walking GCS bucket onedrive-mirror/...")
    blobs = []
    for b in bucket.list_blobs(prefix="onedrive-mirror/"):
        name_lower = b.name.lower()
        ext = next((e for e in SEARCHABLE_EXTS if name_lower.endswith(e)), None)
        if ext:
            blobs.append((b, ext))
        if args.limit and len(blobs) >= args.limit:
            break
    print(f"  Found {len(blobs)} searchable files (PDF/DOCX/XLSX/etc).")
    print()

    print("Building manifest...")
    lines = []
    seen_ids = set()
    extracted_count = 0
    passthrough_count = 0
    large_count = 0
    error_count = 0
    t0 = time.time()

    for i, (blob, ext) in enumerate(blobs, 1):
        if i % 100 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (len(blobs) - i) / rate if rate else 0
            print(f"  [{i}/{len(blobs)}]  {extracted_count} extracted, "
                  f"{passthrough_count} passthrough, {large_count} large-pdf, "
                  f"{error_count} errors  |  ETA {eta/60:.1f} min")

        uri = f"gs://{BUCKET}/{blob.name}"
        title = blob.name.split("/")[-1]
        doc_id = make_doc_id(blob.name)
        if doc_id in seen_ids:
            doc_id = f"{doc_id[:120]}_{len(seen_ids)}"
        seen_ids.add(doc_id)

        struct = {
            "title":      title,
            "source_uri": uri,
            "gcs_uri":    uri,
        }

        # Large PDFs: pointer-only
        if ext == ".pdf" and blob.size and blob.size > LARGE_PDF_BYTES:
            size_mb = blob.size / (1024 * 1024)
            meta = dict(struct)
            meta["document_type"] = "large_pdf_pointer"
            meta["size_mb"] = round(size_mb, 1)
            meta["summary"] = f"{title} is a {size_mb:.1f} MB PDF. GCS path: {uri}"
            lines.append(json.dumps({"id": doc_id, "jsonData": json.dumps(meta)}))
            large_count += 1
            continue

        # Extract or pass through
        content_text = ""
        if not args.no_extract:
            try:
                file_bytes = blob.download_as_bytes()
                content_text = extract_text(file_bytes, ext, blob.name)
            except Exception as e:
                error_count += 1

        if content_text:
            if len(content_text) > MAX_EXTRACTED_CHARS:
                content_text = content_text[:MAX_EXTRACTED_CHARS]
            lines.append(json.dumps({
                "id":       doc_id,
                "jsonData": json.dumps(struct),
                "content":  {
                    "mimeType": "text/plain",
                    "rawBytes": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
                },
            }))
            extracted_count += 1
        else:
            lines.append(json.dumps({
                "id":       doc_id,
                "jsonData": json.dumps(struct),
                "content":  {"mimeType": MIME_MAP.get(ext, "application/pdf"), "uri": uri},
            }))
            passthrough_count += 1

    elapsed = time.time() - t0
    print()
    print("─" * 70)
    print(f" Manifest built in {elapsed/60:.1f} min")
    print(f"   {extracted_count} pre-extracted, {passthrough_count} passthrough, "
          f"{large_count} large-pdf, {error_count} errors")
    print(f"   total: {len(lines)} documents")
    print("─" * 70)

    manifest_path = "manifests/import_manifest_repopulate.jsonl"
    manifest_uri = f"gs://{BUCKET}/{manifest_path}"
    bucket.blob(manifest_path).upload_from_string("\n".join(lines))
    print(f"  Manifest uploaded -> {manifest_uri}")

    if args.dry_run:
        print()
        print("DRY RUN — not triggering Vertex import. Manifest is ready at:")
        print(f"  {manifest_uri}")
        return

    print()
    print("Triggering Vertex import (INCREMENTAL — does NOT wipe existing docs)...")

    import google.auth.transport.requests
    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token

    import requests
    url = (
        f"https://discoveryengine.googleapis.com/v1alpha/projects/{PROJECT}"
        f"/locations/{LOCATION}/collections/default_collection"
        f"/dataStores/{DATA_STORE_ID}/branches/0/documents:import"
    )
    body = {
        "gcsSource": {
            "inputUris": [manifest_uri],
            "dataSchema": "document",
        },
        # INCREMENTAL — adds/updates docs but does NOT delete existing ones.
        # If you want a full wipe-and-reload, change to "FULL" — but that is
        # what got us into this mess. INCREMENTAL is safer.
        "reconciliationMode": "INCREMENTAL",
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": PROJECT,
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=body)
    if r.status_code == 200:
        op_name = r.json().get("name", "")
        print()
        print("=" * 70)
        print(" IMPORT TRIGGERED SUCCESSFULLY")
        print("=" * 70)
        print(f"  Operation: {op_name}")
        print()
        print("  The import runs server-side at Google. It typically takes")
        print("  30-60 minutes to fully populate the index for ~10K files.")
        print()
        print("  Monitor progress at:")
        print(f"  https://console.cloud.google.com/gen-app-builder/data-stores"
              f"?project={PROJECT}")
        print()
        print("  Or re-run scripts/diagnose_index.py in 30 min — the doc")
        print("  count should be > 0.")
    else:
        print(f"\nImport request failed: {r.status_code}")
        print(r.text)
        sys.exit(1)


if __name__ == "__main__":
    main()
