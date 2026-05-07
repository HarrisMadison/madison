"""
Phase 6 — OCR preprocessor + metadata enricher for onedrive_sync.py

Provides two functions called by _build_and_upload_manifest():

1. enrich_metadata(blob_name, struct) -> dict
   Extracts property name, document type, and date from the file path/name
   and adds them as structured fields so Vertex can filter on them.

2. needs_ocr(blob_name, blob_size) -> bool
   Heuristic: returns True for PDFs that are likely scanned (low size/page ratio
   or filename patterns that match known scan outputs).

3. ocr_pdf(gcs_uri, project_id) -> str | None
   Calls Google Document AI to extract text from a scanned PDF.
   Returns extracted text or None if Document AI is not configured.
   Falls back gracefully -- if Document AI is not enabled the sync still works,
   just without OCR text for scanned docs.
"""

from __future__ import annotations
import re
import os
import logging
from pathlib import Path

log = logging.getLogger("onedrive_sync.phase6")

# ── Document type classifier ──────────────────────────────────────────────────
# Maps filename keywords -> human-readable document type stored in metadata.
# Order matters -- first match wins.
_DOC_TYPE_RULES: list[tuple[str, str]] = [
    # Financial
    (r"p.?l|profit.?loss|statement",            "pl_statement"),
    (r"invoice|inv_",                            "invoice"),
    (r"closing.?package|closing.?docs",          "closing_package"),
    (r"closing.?statement|hud",                  "closing_statement"),
    (r"deposit|wire|payment",                    "payment_record"),
    (r"draw.?request",                           "draw_request"),
    # Legal / title
    (r"title.?report",                           "title_report"),
    (r"deed",                                    "deed"),
    (r"contract|executed",                       "contract"),
    (r"terms.?of.?sale",                         "terms_of_sale"),
    (r"agency.?disclosure",                      "disclosure"),
    (r"disclosure",                              "disclosure"),
    # Valuation
    (r"appraisal",                               "appraisal"),
    (r"assessment",                              "assessment"),
    # Permits / compliance
    (r"permit|webpermit",                        "permit"),
    (r"certificate.?of.?occupancy|coo",          "certificate_of_occupancy"),
    (r"certificate.?of.?compliance",             "certificate_of_compliance"),
    (r"inspection",                              "inspection_report"),
    (r"violation",                               "violation_report"),
    # Insurance / environmental
    (r"flood",                                   "flood_disclosure"),
    (r"insurance|policy",                        "insurance_policy"),
    (r"asbestos|mold",                           "environmental_report"),
    (r"goosehead|safechoice",                    "insurance_document"),
    # Loan / financing
    (r"loan.?approv|lender",                     "loan_document"),
    (r"orion",                                   "loan_document"),
    # Entity docs
    (r"ein|irs",                                 "tax_document"),
    (r"entity|llc|operating.?agreement",         "entity_document"),
    # Scope / SOW
    (r"\bsow\b|scope.?of.?work",                 "scope_of_work"),
    # Enrollment / producer
    (r"enrollment|producer",                     "insurance_document"),
    # Catch-all scan patterns
    (r"hpscan|atcco|atcks",                      "scanned_document"),
    (r"^\d{17,}",                                "scanned_document"),    # Doorloop auto-scans
]

# ── Property name extractor ───────────────────────────────────────────────────
# Extracts a meaningful grouping name from the GCS blob path.
#
# When ONEDRIVE_FOLDER_PATH is set (e.g. "Doorloop"), paths look like:
#   onedrive-mirror/Doorloop/<PROPERTY>/files/appraisal.pdf  -> "<PROPERTY>"
#
# When ONEDRIVE_FOLDER_PATH is empty (whole-drive sync), paths look like:
#   onedrive-mirror/<TOPLEVEL>/<sub>/file.pdf                -> "<TOPLEVEL>"
#   onedrive-mirror/<TOPLEVEL>/file.pdf                      -> "<TOPLEVEL>"
#
# We strip the configured scope prefix (if any) and use the next segment
# as the property/folder grouping name. Falls back to "" when the path
# is too shallow to extract anything meaningful.
def _extract_property(blob_name: str) -> str:
    parts = [p for p in blob_name.replace("\\", "/").split("/") if p]
    # Drop leading 'onedrive-mirror' if present
    if parts and parts[0] == "onedrive-mirror":
        parts = parts[1:]

    scope = os.environ.get("ONEDRIVE_FOLDER_PATH", "").strip("/").strip()
    if scope:
        scope_parts = [p for p in scope.split("/") if p]
        # If the path starts with the scope folder, strip it
        if parts[: len(scope_parts)] == scope_parts:
            parts = parts[len(scope_parts):]

    # First remaining segment is the grouping name; we need at least 1 more
    # segment (the file or a subfolder) for it to be a real grouping.
    if len(parts) >= 2:
        return parts[0]
    if len(parts) == 1:
        # Could be either a top-level file or a single-segment grouping;
        # treat as ungrouped.
        return ""
    return ""


def _classify_doc_type(filename: str) -> str:
    name_lower = filename.lower()
    # Strip extension for matching
    stem = Path(filename).stem.lower()
    for pattern, doc_type in _DOC_TYPE_RULES:
        if re.search(pattern, stem, re.IGNORECASE):
            return doc_type
    return "document"


def _extract_date(filename: str) -> str:
    """Try to extract a date from Doorloop-style filenames like 20230614125527814.pdf"""
    m = re.match(r"(\d{4})(\d{2})(\d{2})", Path(filename).stem)
    if m:
        y, mo, d = m.groups()
        # Sanity check
        if 2015 <= int(y) <= 2030 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
    # Try date patterns in name like "JAN-JUL" etc
    month_map = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    for mon, num in month_map.items():
        if mon in filename.lower():
            return f"2023-{num}"   # approximate year
    return ""


def enrich_metadata(blob_name: str, base_struct: dict) -> dict:
    """
    Add structured metadata fields to a manifest document record.
    These fields are stored in jsonData and indexed by Vertex so Bob
    can filter by property, doc type, or date without full-text search.
    """
    filename = blob_name.split("/")[-1]
    property_name = _extract_property(blob_name)
    doc_type      = _classify_doc_type(filename)
    doc_date      = _extract_date(filename)

    enriched = dict(base_struct)
    enriched["property"]      = property_name
    enriched["document_type"] = doc_type
    if doc_date:
        enriched["doc_date"]  = doc_date

    # Improve title: use property + doc type if title is just a raw scan ID
    title = enriched.get("title", filename)
    if re.match(r"^\d{17,}", Path(title).stem):
        enriched["title"] = f"{property_name} — {doc_type.replace('_', ' ').title()}"

    log.debug(f"  Metadata: {property_name} | {doc_type} | {doc_date} <- {filename}")
    return enriched


# ── OCR heuristic ─────────────────────────────────────────────────────────────
# Doorloop auto-scan filenames are 17+ digit timestamps or start with ATCCO/ATCKS/HPSCAN.
_SCAN_PATTERNS = re.compile(
    r"(^\d{17,}|hpscan|atcco|atcks|bscan|scan_)", re.IGNORECASE
)

def needs_ocr(blob_name: str, blob_size: int) -> bool:
    """
    Heuristic: return True if this PDF is likely a scanned image.
    Scanned PDFs have no embedded text -- Vertex extracts nothing from them.
    Document AI OCR unlocks them.

    Criteria:
    - Filename matches known scan patterns (Doorloop, HP scanner, ATC scanner)
    - OR file is large but has a short filename (raw scan dumps)
    """
    filename = blob_name.split("/")[-1]
    stem     = Path(filename).stem

    if _SCAN_PATTERNS.search(stem):
        return True

    # Large file + pure numeric name = likely unprocessed scan
    if blob_size > 500_000 and re.match(r"^\d+$", stem):
        return True

    return False


# ── Document AI OCR ───────────────────────────────────────────────────────────
# Module-level circuit breaker. If OCR fails for a deterministic reason
# (permission denied, processor not found, API not enabled), every subsequent
# call will fail the same way. Flip this flag on the first such failure and
# skip all further OCR attempts for the remainder of the run -- saves hours
# of wasted retries.
_OCR_DISABLED = False
_OCR_DISABLED_REASON = ""


# OCR result cache: stored in the same GCS bucket under a separate prefix
# so each scanned PDF only gets sent through Document AI ONCE -- ever.
# Before paying Document AI to OCR a scan, we check if a cache entry exists
# AND is newer than the source PDF. If yes, read text from cache (free).
# If no, run OCR and write the result to cache.
OCR_CACHE_PREFIX = "ocr-cache/"

def _ocr_cache_path(gcs_uri: str) -> str:
    """Convert a source GCS URI into the corresponding cache blob path.
    gs://bucket/onedrive-mirror/foo/bar.pdf -> ocr-cache/onedrive-mirror/foo/bar.pdf.txt
    """
    # Strip 'gs://bucket/' prefix
    rest = gcs_uri.split("/", 3)[-1] if gcs_uri.startswith("gs://") else gcs_uri
    return OCR_CACHE_PREFIX + rest + ".txt"


def _read_ocr_cache(bucket, source_blob_name: str, source_updated, gcs_uri: str) -> str | None:
    """Return cached OCR text if cache exists AND is newer than source. Else None."""
    try:
        cache_blob = bucket.blob(_ocr_cache_path(gcs_uri))
        if not cache_blob.exists():
            return None
        cache_blob.reload()
        # If source PDF was modified after the cache was written, invalidate cache.
        if source_updated and cache_blob.updated and source_updated > cache_blob.updated:
            log.debug(f"  OCR cache stale for {source_blob_name} (source newer); will re-OCR")
            return None
        text = cache_blob.download_as_text()
        log.info(f"  OCR cache HIT: {len(text)} chars for {source_blob_name.split('/')[-1]} (no API call)")
        return text
    except Exception as e:
        log.debug(f"  OCR cache read failed for {source_blob_name}: {e}")
        return None


def _write_ocr_cache(bucket, gcs_uri: str, text: str) -> None:
    """Store OCR'd text alongside the bucket so we never re-pay for the same scan."""
    try:
        cache_blob = bucket.blob(_ocr_cache_path(gcs_uri))
        cache_blob.upload_from_string(text, content_type="text/plain; charset=utf-8")
        log.debug(f"  OCR cache WRITE: {len(text)} chars -> {cache_blob.name}")
    except Exception as e:
        log.warning(f"  Could not write OCR cache for {gcs_uri}: {e}")


def ocr_pdf_gcs(gcs_uri: str, project_id: str, location: str = "us",
                bucket=None, source_blob_name: str = "", source_updated=None) -> str | None:
    """
    Run Google Document AI OCR on a GCS-hosted PDF.
    Returns extracted text or None if Document AI is unavailable.

    Caches results in gs://<bucket>/ocr-cache/ so each scan is OCR'd ONCE
    forever (until the source PDF changes). Subsequent calls read from
    cache for free.

    Setup required (one-time):
      1. Enable Document AI API in GCP console
      2. Create an OCR processor:
         gcloud ai document-processors create --type=OCR_PROCESSOR --location=us
      3. Set DOCAI_PROCESSOR_ID in your .env
      4. Grant the service account 'roles/documentai.apiUser'

    Cost: ~$1.50 per 1,000 pages. A 400-doc library at ~15 pages avg = ~$9 total.
    With caching, repeat-syncs cost $0.
    """
    global _OCR_DISABLED, _OCR_DISABLED_REASON

    # Circuit breaker: skip silently if a previous call already determined
    # OCR is unavailable for the rest of this run.
    if _OCR_DISABLED:
        return None

    # ── Cache lookup BEFORE calling Document AI ───────────────────────────
    if bucket is not None:
        cached = _read_ocr_cache(bucket, source_blob_name, source_updated, gcs_uri)
        if cached is not None:
            return cached

    processor_id = os.environ.get("DOCAI_PROCESSOR_ID", "")
    if not processor_id:
        log.debug("  OCR skipped: DOCAI_PROCESSOR_ID not set in .env")
        return None

    try:
        from google.cloud import documentai_v1 as docai

        client = docai.DocumentProcessorServiceClient(
            client_options={"api_endpoint": f"{location}-documentai.googleapis.com"}
        )

        processor_name = (
            f"projects/{project_id}/locations/{location}"
            f"/processors/{processor_id}"
        )

        # Use batch processing for GCS URIs (more reliable for large PDFs)
        gcs_document = docai.GcsDocument(
            gcs_uri=gcs_uri,
            mime_type="application/pdf",
        )

        request = docai.ProcessRequest(
            name=processor_name,
            gcs_document=gcs_document,
        )

        result = client.process_document(request=request)
        text   = result.document.text
        log.info(f"  OCR: extracted {len(text)} chars from {gcs_uri.split('/')[-1]}")
        # Save to cache so future syncs don't re-pay for this scan
        if bucket is not None and text:
            _write_ocr_cache(bucket, gcs_uri, text)
        return text

    except ImportError:
        log.debug("  OCR skipped: google-cloud-documentai not installed")
        log.debug("  Install: pip install google-cloud-documentai")
        _OCR_DISABLED = True
        _OCR_DISABLED_REASON = "google-cloud-documentai library not installed"
        return None
    except Exception as e:
        err_str = str(e)
        # Detect deterministic failures and trip the circuit breaker so we
        # don't retry thousands of times for the same reason.
        deterministic_signals = (
            "403",                    # Permission denied
            "PERMISSION_DENIED",
            "IAM_PERMISSION_DENIED",
            "404",                    # Processor not found / API not enabled
            "NOT_FOUND",
            "has not been used",      # API not enabled in project
            "is not enabled",
        )
        if any(sig in err_str for sig in deterministic_signals):
            _OCR_DISABLED = True
            _OCR_DISABLED_REASON = err_str[:200]
            log.error(
                "OCR has been DISABLED for this run after a deterministic failure. "
                "All subsequent scanned PDFs will fall through to Vertex's parser."
            )
            log.error(f"  Reason: {err_str[:300]}")
            log.error(
                "  Likely fix: grant the service account 'roles/documentai.apiUser' "
                "and re-run --rebuild-only."
            )
        else:
            # Transient errors (network, rate limit) -- log and continue
            log.warning(f"  OCR failed for {gcs_uri}: {err_str[:200]}")
        return None
