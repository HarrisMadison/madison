"""
Phase 4 Job Intelligence — Optimized Architecture
- Vertex AI Search: RETRIEVAL ONLY (no LLM summarization, saves quota)
- Gemini: ALL synthesis and answering (cheap, fast, better)
- Smart caching: Don't re-search unnecessarily
"""
import os, re, uuid, time, json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from google.cloud import discoveryengine_v1 as discoveryengine
from google.api_core import exceptions as gapi_exceptions
from google.oauth2 import service_account
import google.auth
import google.generativeai as genai

# Shared full-text fetcher — imported from vertex/. Falls back gracefully if
# unavailable so this module still works in isolation.
try:
    import sys
    _REPO = Path(__file__).resolve().parent.parent
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    from vertex.document_fetch import get_document_by_name as _fetch_doc_by_name
    _FETCH_AVAILABLE = True
except Exception as _e:
    print(f"[Phase4-scripts] document_fetch unavailable: {_e}")
    _FETCH_AVAILABLE = False

# Local filename index — lets us answer name-based questions WITHOUT calling
# Vertex search. Eliminates 95% of search-quota usage.
try:
    from local_index import get_index as _get_local_index
    _LOCAL_INDEX_AVAILABLE = True
except Exception as _e:
    print(f"[Phase4-scripts] local_index unavailable: {_e}")
    _LOCAL_INDEX_AVAILABLE = False

# Document-type classifier rules — single source of truth lives in
# Phase5_oneDrive/phase6_ocr_metadata.py and is re-used here so ranking can
# infer doc_type from a filename at QUERY time. This matters because the
# stored manifest's document_type only updates after a re-ingest, but the
# ranker can use these rules immediately on existing files. Falls back to a
# noop classifier so the ranker still works if Phase 5 isn't on the path.
try:
    import sys as _sys
    _P5_DIR = Path(__file__).resolve().parent.parent / "Phase5_oneDrive"
    if str(_P5_DIR) not in _sys.path:
        _sys.path.insert(0, str(_P5_DIR))
    from phase6_ocr_metadata import _classify_doc_type as _classify_doc_type_at_query
    _DOC_TYPE_CLASSIFIER_AVAILABLE = True
except Exception as _e:
    print(f"[Phase4-scripts] phase6 classifier unavailable: {_e}")
    _DOC_TYPE_CLASSIFIER_AVAILABLE = False
    def _classify_doc_type_at_query(filename: str) -> str:
        return "document"

SERVING_CONFIG = os.getenv("VERTEX_SERVING_CONFIG", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
SESSION_TTL    = 3600
MAX_HISTORY    = 6
CONTEXT_CACHE_MINUTES = 15  # Reuse search results for this long

# === Phase 1: folder-aware retrieval =====================================
# Searchable file extensions for folder enumeration. Mirrors the allowlist
# in Phase5_oneDrive/onedrive_sync.py so we never go out of sync. Files
# outside this list (.js.download, .css, .axd, ToThumbnail web-export
# noise, .gif, etc.) are filtered out before Gemini ever sees the folder
# manifest. This is what makes Pampinella's 164 raw files collapse to the
# ~8 real legal documents the user actually wants.
_FOLDER_SEARCHABLE_EXTS = (
    ".pdf", ".docx", ".doc", ".xlsx", ".xls",
    ".csv", ".txt", ".pptx", ".ppt", ".md",
)

# Marker file extensions. Files whose only extension is in this set are
# NOT text-extractable by the current document_fetch pipeline, but their
# presence is meaningful evidence: it tells us the folder exists, has
# real content, and was visited by the company workflow that generated
# them (e.g. CompanyCam Final/<folder>/report.html is the canonical
# output of a CompanyCam photo report).
#
# Marker files are surfaced in folder enumeration (so folder_purpose can
# fire) but skipped during text extraction (no fetches attempted, no
# Gemini context bloat). The dossier records them as "marker file --
# text extraction not supported for .html" so downstream prompts and
# summaries are honest about what's known vs. unread.
#
# Generic rule: if we add another marker type later (e.g. .zip photo
# archives), append the extension here. No folder names live in code.
_FOLDER_MARKER_EXTS = (".html",)

# Combined enumeration set. Used wherever we count or list files for
# folder-aware features. The split between searchable and marker is
# applied later, inside _build_folder_dossier.
_FOLDER_ENUMERABLE_EXTS = _FOLDER_SEARCHABLE_EXTS + _FOLDER_MARKER_EXTS

# Per-mode budgets. Tuned so Mode 1+2 token usage matches or undercuts the
# pre-Phase-1 path (which sent 8 sources @ 3000 chars to Gemini = 24k chars).
# Mode 1 reads 5 docs at full snippet length -> ~15k chars to Gemini.
# Mode 2 reads 10 -> ~30k chars, comparable to today.
# Mode 3 reads ZERO -> cheaper than today by definition.
MODE_1_READ_LIMIT = 5    # specific factual ask: read top-N for the answer
MODE_2_READ_LIMIT = 10   # focused synthesis: read top-N for synthesis
MODE_3_LIST_LIMIT = 100  # unbounded enumeration: list up to N titles, no reading

# Query-intent -> document-type table. Used by Mode 1/2 ranking inside a
# detected folder so that asking for an "opinion of value" boosts every
# appraisal-class file in the folder, not just files whose names happen to
# share keywords with the query.
#
# This is the durable replacement for filename-keyword-only ranking. It is
# reusable across the whole portfolio: any "appraised value" query benefits
# every property's appraisal docs, not just 27 Manor's.
#
# Patterns are matched as a single regex against the lowercased query. The
# matched doc_type strings must agree with what _classify_doc_type returns
# in phase6_ocr_metadata.py.
_QUERY_INTENT_RULES: list[tuple[str, str]] = [
    # Valuation
    (r"opinion.?of.?value|appraised.?value|market.?value|appraisal|how.?much.?is.?(it|the.?house|the.?property)|what.?is.?(it|the.?house|the.?property).?worth|value.?of.?the.?(house|property|home)",
     "appraisal"),
    (r"comparable.?sales?|comp.?report|comps?\b",                  "appraisal"),
    # Closing / financial
    (r"closing.?statement|hud.?1|net.?proceeds|cash.?to.?close",   "closing_statement"),
    (r"closing.?package|closing.?docs?",                            "closing_package"),
    (r"profit.?(and|&).?loss|p.?(and|&).?l|p\s*&\s*l|\bpl\b",        "pl_statement"),
    # Permits / compliance
    (r"certificate.?of.?occupancy|\bc\.o\.",                       "certificate_of_occupancy"),
    (r"permit\b|building.?permit",                                  "permit"),
    (r"inspection|inspector",                                       "inspection_report"),
    (r"violation",                                                  "violation_report"),
    # Title / legal
    (r"title.?report|chain.?of.?title",                             "title_report"),
    (r"\bdeed\b",                                                   "deed"),
    (r"contract|executed.?contract|signed.?contract",               "contract"),
    # Insurance
    (r"policy|insurance",                                           "insurance_policy"),
    # Loan
    (r"draw.?request|construction.?draw",                           "draw_request"),
    (r"loan.?approv|lender|loan.?document",                         "loan_document"),
    # Invoices / billing
    (r"invoice|bill\b|receipt",                                     "invoice"),
]

# Boost magnitude for an intent match. 200 is high enough to overpower the
# keyword-overlap path (which maxes around 30 + 5*N for N distinctive words)
# without being so dominant that a perfect filename match gets buried. A file
# matching the query's intended doc type will out-rank a file with two
# coincidental keyword hits, which is exactly what we want.
_INTENT_BOOST = 200.0

# === Folder summary feature ==============================================
# Triggers a structured-summary response within MODE_2 when the user is
# clearly asking for an overview of a folder/claim/property rather than
# a synthesis across documents. Examples that should match:
#   "summarize 27 Manor Drive"
#   "what's going on with this claim"
#   "give me the status of the Pampinella file"
#   "tell me about 5 Croydon"
#   "what documents do we have for 15 Northridge"
# Phrasing-only check -- no Gemini, no LLM. Regex against the lowercased
# query. Order doesn't matter; first match wins.
_FOLDER_SUMMARY_PATTERNS: list[str] = [
    r"\bsummari[sz]e\b",
    r"\bsummary\b",
    r"\bstatus\b",
    r"\boverview\b",
    r"\bwhat'?s?\s+going\s+on\b",
    r"\bwhere\s+do\s+we\s+stand\b",
    r"\btell\s+me\s+about\b",
    r"\bwhat\s+documents?\s+(?:do\s+we\s+have|exist)\b",
    r"\bwhat\s+(?:do\s+we\s+have|exists?)\s+(?:for|on|about)\b",
    r"\bgive\s+me\s+(?:a\s+)?(?:summary|status|overview|rundown)\b",
    r"\brundown\b",
    r"\brecap\b",
    r"\bbreakdown\b",
    r"\bkey\s+facts?\b",
    # Open-items / completeness intent. When a folder is in scope and
    # the user asks "what's missing" / "open items" / "checklist", we
    # treat that as a folder-scoped structural question and route it
    # into the summary branch -- which then renders a focused
    # open-items-only response. Without this, follow-ups like
    # "whats missing" fall through to the generic Vertex/Gemini path
    # and get nonsense ("the document excerpts are empty...").
    r"\bopen\s+items?\b",
    r"\bmissing\b",
    r"\bwhat'?s\s+(?:not|missing|absent|left|outstanding)\b",
    r"\bwhat\s+do\s+(?:i|we)\s+(?:still\s+)?need\b",
    r"\bgap\b|\bgaps\b",
    r"\bchecklist\b",
    r"\bcomplete(?:ness)?\b",
    r"\banything\s+(?:missing|left|outstanding)\b",
    r"\bdo\s+(?:i|we)\s+have\s+(?:everything|all)\b",
]
_FOLDER_SUMMARY_RE = re.compile("|".join(_FOLDER_SUMMARY_PATTERNS), re.IGNORECASE)

# Doc-type buckets shown in the summary's Document Inventory section.
# Maps the raw doc_type values (from the sidecar / filename classifier /
# query-time classifier) to user-facing bucket labels. Values outside
# this map land in "other". The keys must agree with what
# phase6_ocr_metadata._classify_doc_type and metadata.content_classifier
# can return.
_DOC_TYPE_BUCKETS: dict[str, str] = {
    # Valuation
    "appraisal":                 "appraisal",
    "assessment":                "appraisal",
    # Estimates / scopes of work
    "estimate":                  "estimate",
    "scope_of_work":             "estimate",
    "sow":                       "estimate",
    # Invoices / billing
    "invoice":                   "invoice",
    "bill":                      "invoice",
    "receipt":                   "invoice",
    # Contracts
    "contract":                  "contract",
    "agreement":                 "contract",
    "signed_contract":           "contract",
    # Insurance / claims
    "insurance_policy":          "insurance",
    "claim":                     "insurance",
    "claim_documents":           "insurance",
    "adjustment":                "insurance",
    # Reports (inspections, environmental, soot, etc.)
    "inspection_report":         "report",
    "environmental_report":      "report",
    "soot_report":               "report",
    "report":                    "report",
    # Permits / compliance
    "permit":                    "permit",
    "certificate_of_occupancy":  "permit",
    "violation_report":          "permit",
    # Correspondence
    "correspondence":            "correspondence",
    "demand_letter":             "correspondence",
    "letter":                    "correspondence",
    "email":                     "correspondence",
    # Spreadsheets (catch-all for .xlsx with no better classification)
    "spreadsheet":               "spreadsheet",
    # Closing/financial -- group with appraisal because they're valuation-adjacent
    "closing_statement":         "appraisal",
    "closing_package":           "appraisal",
    "pl_statement":              "spreadsheet",
    # Deed / title -- group with contract
    "deed":                      "contract",
    "title_report":              "contract",
    # Loan -- group with contract
    "loan_document":             "contract",
    "draw_request":              "invoice",
}
# Display order for the inventory section. Buckets without files are skipped.
_DOC_TYPE_DISPLAY_ORDER: list[str] = [
    "appraisal",
    "insurance",
    "estimate",
    "invoice",
    "contract",
    "report",
    "permit",
    "correspondence",
    "spreadsheet",
    "photos",
    "other",
]

# Top-N files included in the summary dossier. Wider than MODE_1's 5 and
# MODE_2's 10 because summaries benefit from breadth -- we want to see
# every doc-type bucket represented if it exists.
#
# Staggered snippet budget: the top files get the largest budget (key
# facts live there), middle files get a medium budget (enough to know
# what they are), bottom files get just enough for inventory context.
# Total dossier text budget at full breadth: 5*3000 + 10*800 + 5*200
# = 24,000 chars, comfortably inside Gemini Flash's window with room
# for the prompt scaffolding.
#
# v1 used a flat 600 chars per file which lost key facts that lived
# past position 600 (e.g. the "Opinion of Value" line of an FNMA 1004
# typically sits on page 2 or 3, after the property identification
# header). The staggered budget fixes that without ballooning cost.
SUMMARY_DOSSIER_FILE_LIMIT = 20
SUMMARY_DOSSIER_TIER_SIZES = [
    (5,  3000),   # top 5 files: full snippet (matches MODE_1's read budget)
    (10,  800),   # next 10: medium budget
    (5,   200),   # last 5: title + tiny preview for inventory context
]

# Open-items checklist: which buckets we expect to see in a complete
# claim/property folder. Order is the display order in the Open Items
# section.
#
# Each entry is (label, bucket_key, strict_doc_types_or_None).
#   - label              : verbatim heading text shown to the user
#   - bucket_key         : the inventory bucket to consult
#   - strict_doc_types   : the raw doc_type values that constitute a
#                          "strict" match for THIS specific check.
#                          If None, every doc_type in the bucket is treated
#                          as strict (used when the bucket is unambiguous,
#                          e.g. "invoice" or "photos").
#
# Why strict_doc_types matter: several distinct raw doc_types share a
# display bucket. For example, the `contract` bucket contains real
# contracts AND deeds, title reports, loan documents -- all
# real-estate-adjacent records that aren't "signed agreements" in the
# colloquial sense. Reporting "Signed contract: found" when the only
# match is a deed overstates what we have. We resolve this with three
# output states per checklist item:
#     - "found in available folder inventory" -- at least one STRICT match
#     - "found, but type may need review"     -- bucket has files, but
#                                                none of them are strict
#     - "not found in available folder inventory" -- bucket is empty
#
# Phrasing is intentionally cautious -- never "missing", never absolute.
# Expanding this list later is the natural growth path for claim-type-
# specific checklists (e.g. fire claims requiring soot/smoke reports).
SUMMARY_OPEN_ITEMS_CHECKLIST: list[tuple[str, str, Optional[tuple[str, ...]]]] = [
    # Contract-type docs: strict matches are actual signed agreements.
    # Deeds/titles/loan docs land in the same bucket but don't count.
    ("Agreement / contract",                 "contract",
        ("contract", "agreement", "signed_contract")),
    # Insurance bucket includes both policies and claim docs -- both
    # are valid evidence of an insurance/claim relationship, so all
    # doc_types in the bucket count as strict (None signals this).
    ("Insurance / claim document",           "insurance",       None),
    # Estimate bucket: strict matches are estimates and scopes of work.
    ("Estimate / scope of work",             "estimate",        None),
    # Invoice bucket is unambiguous.
    ("Invoice",                              "invoice",         None),
    # Report bucket: any inspection/environmental report counts.
    ("Inspection / report",                  "report",          None),
    # Photos bucket: any photo doc counts (currently photo pointers).
    ("Photos / photo report",                "photos",          None),
]

# Property/appraisal/real-estate checklist. Used for folders classified
# as "property_appraisal" (appraisals + closing docs + deeds, no claim
# signals). Phrasing in the rendered view shifts from "missing/not found"
# to "available/not seen" -- a property folder shouldn't imply that
# absent items are gaps, only that they aren't in our index.
#
# Strict-doc-type discrimination matters more here than for claims:
# the appraisal bucket holds both appraisals AND closing
# statements/packages, but we want those to register as the "Deed /
# title / closing document" check, not as the appraisal check. So
# the appraisal entry's strict set excludes closing-flavored doc_types.
PROPERTY_FOLDER_CHECKLIST: list[tuple[str, str, Optional[tuple[str, ...]]]] = [
    # Appraisal / valuation -- only real appraisal doc_types, not
    # closing-statements/packages that happen to share the bucket.
    ("Appraisal / valuation",                "appraisal",
        ("appraisal",)),
    # Deed / title / closing document. The contract bucket holds deeds
    # and title reports; the appraisal bucket holds closing statements
    # and closing packages. Both can satisfy this check, but only the
    # strict doc_types qualify -- a signed renovation contract in the
    # contract bucket would NOT count toward "deed / title / closing".
    # The renderer looks at strict matches across BOTH buckets via the
    # multi-bucket signature shape ("bucket1,bucket2"). To keep the
    # existing single-bucket compute logic simple, we keep this entry
    # on the contract bucket and accept closing_* via a second entry.
    ("Deed / title",                         "contract",
        ("deed", "title_report")),
    ("Closing document",                     "appraisal",
        ("closing_statement", "closing_package")),
    # Inspection / report -- same as the claim checklist.
    ("Inspection / report",                  "report",          None),
    # Loan / financing -- often present on flip/acquisition packages.
    ("Loan / financing document",            "contract",
        ("loan_document",)),
    # Signed contract / agreement -- separate from the deed/title row
    # so users can see when a folder has BOTH a deed and a separate
    # signed contract (uncommon but informative).
    ("Contract / agreement",                 "contract",
        ("contract", "agreement", "signed_contract")),
]

# Open-items intent regex. When the user query matches any of these,
# the Open Items checklist is rendered regardless of folder purpose --
# the user explicitly asked about gaps/completeness, so the generic
# checklist is welcome even on folders where we wouldn't show it by
# default. Pure phrasing match, no Gemini.
_OPEN_ITEMS_INTENT_PATTERNS: list[str] = [
    r"\bopen\s+items?\b",
    r"\bmissing\b",
    r"\bwhat\s+(?:is|are|'?s)?\s*(?:not|missing|absent)\b",
    r"\bwhat\s+do\s+(?:i|we)\s+(?:still\s+)?need\b",
    r"\bgap\b|\bgaps\b",
    r"\bchecklist\b",
    r"\bcomplete(?:ness)?\b",
    r"\banything\s+(?:missing|left|outstanding)\b",
    r"\bdo\s+(?:i|we)\s+have\s+(?:everything|all)\b",
]
_OPEN_ITEMS_INTENT_RE = re.compile("|".join(_OPEN_ITEMS_INTENT_PATTERNS), re.IGNORECASE)

# Folder-purpose classification. Used to decide whether the generic
# Open Items checklist ("Agreement / contract: needs review", etc.) is
# rendered by default.
#
# The checklist is shaped for claim/restoration jobs -- it expects
# insurance docs, estimates, invoices, photos. On a property folder
# whose contents are appraisals and a deed, telling the user
# "Insurance / claim document: not found" is noise: nothing is
# actually missing because nothing was expected.
#
# Three purposes:
#   - "claim_restoration"  : claim-job-shaped contents; render Open Items
#   - "property_appraisal" : real-estate / valuation contents; suppress
#   - "unknown"            : neither pattern clearly fires; suppress
#                            unless the user explicitly asked
#
# Classification is deterministic from the inventory bucket counts.
# No Gemini call. The chosen purpose is stored on structured_summary
# so downstream consumers can also use it.
_CLAIM_RESTORATION_BUCKETS = {"insurance", "estimate", "photos"}
_PROPERTY_APPRAISAL_BUCKETS = {"appraisal", "contract"}

# Folder-name signals for claim/restoration jobs. These supplement (not
# replace) the bucket-content signals above. Many claim folders contain
# only inspection reports and contracts at the file level -- which
# matches the property profile -- but the folder NAME makes the intent
# obvious ("claim paid & closed", "toilet overflow", "mold & water").
#
# Word-boundary anchored to avoid:
#   - "loss" inside "profit & loss" (caught earlier by stronger rules)
#   - "lead" inside "lead time" or "sales lead"
#   - "fire" inside compound words
# Tokens with high ambiguity (water/fire alone could match address
# components in other corpora) are kept because in this corpus the
# property folders use street suffixes (Drive, Avenue, Street) and don't
# include damage-type vocabulary in their names.
#
# CAUTION on "appraisal": some claim folders literally have
# "- appraisal -" in the name (e.g. "Bolen, Barbara -appraisal -
# Jeremy Wolf"). Don't add appraisal to this list -- it would force
# property folders to claim_restoration.
_CLAIM_NAME_RE = re.compile(
    r"\b(?:"
    r"claim|restoration|puffback|smoke|mitigation|rebuild|"
    r"water|fire|mold|lead|loss|"
    r"toilet[\s_-]*overflow|"
    r"paid[\s_&]*(?:and|&)?[\s_&]*closed"
    r")\b",
    re.IGNORECASE,
)

# Buckets that, when combined with a claim-signal folder name, are
# strong-enough evidence to classify the folder as claim_restoration
# even if no insurance/estimate/photos files are tagged. A claim folder
# in the wild typically has at least one of: an inspection report
# (Encircle/IICRC/COC), a signed authorization (AOB / contract bucket),
# or a billing record. The 'other' bucket alone is NOT supporting --
# 'other' is the catch-all for unclassified files and matches almost
# every folder, claim or not.
_CLAIM_SUPPORTING_BUCKETS = {
    "report", "contract", "invoice",
    "insurance", "estimate", "photos",   # also covered by the strict rule
}

# Admin/template/reference folder name signal. When this fires on a
# folder name, _classify_folder_purpose short-circuits to "unknown"
# BEFORE the strict-claim / claim-by-name / property rules. Rationale:
# folders like "4. Bob Sheets - Spreadsheets" and "Merge Form Templates"
# are admin aggregators (operator's reference docs, blank forms,
# templates) that incidentally contain claim-flavored files (e.g. a
# generic "claim checklist.pdf") and were being misclassified as
# claim_restoration. The rule operates on folder name only, deliberately:
# inventory contents are unreliable for distinguishing real work from
# templates. See OPERATIONS.md §5.7 caveat (a) and §5.10 (this ship).
#
# Surface (deliberately narrow):
#   1. (templates?|forms?|boilerplate|reference)\s*$ -- folder name ENDS
#      with the admin keyword. Catches "Merge Form Templates", hypothetical
#      "Closing Forms", "Madison Boilerplate". Trailing anchor prevents
#      firing on "Reference Documents for Smith".
#   2. \bbob[\s_]+sheets?\b -- operator's admin compound. Compound match
#      prevents firing on a customer named "Bob" or street "Sheets".
#   3. \bmerge[\s_]+form\b -- template-aggregator compound.
#   4. ^\s*\d{4}\s+payroll\b -- anchored year+payroll, e.g. "2020 Payroll".
#      Bare "\bpayroll\b" was rejected; a customer at "Payroll Lane" or
#      a payroll-office claim would over-fire.
#
# Rejected from this surface:
#   - ^\s*\d+\.\s numeric prefix (false-positives "1. Bathroom Remodel -
#     Bolen", a real customer work-unit folder)
#   - standalone \bsheets?\b, \bmerge\b, \bpayroll\b (over-broad)
#   - broad \b(documents?|docs?|logs?)\b patterns
#
# Validated by scripts/probe_classifier_admin_folder.py: 7/7 pass
# criteria, exactly 2 expected flips (Bob Sheets, Merge Form Templates),
# no canonical claim/property folder regressions.
_ADMIN_NAME_RE = re.compile(
    r"(?:templates?|forms?|boilerplate|reference)\s*$"
    r"|\bbob[\s_]+sheets?\b"
    r"|\bmerge[\s_]+form\b"
    r"|^\s*\d{4}\s+payroll\b",
    re.IGNORECASE,
)

# ─── structured_summary schema contract ───────────────────────────────────
# The structured_summary object attached to every folder-aware response is
# the source of truth for future reporting/analytics/persistence. Treat its
# shape as a versioned contract: do not silently rename fields, do not drop
# fields, and bump _STRUCTURED_SUMMARY_SCHEMA_VERSION whenever a
# backwards-incompatible change ships. New optional fields can be added
# without a version bump as long as existing consumers stay valid.
#
# The actual normalization happens in JobIntelligence._normalize_structured_summary,
# which is the single entry point every emitter must call. These module-
# level constants define the allowed enum values so a consumer (or a
# linter) can verify a payload before persistence.
_STRUCTURED_SUMMARY_SCHEMA_VERSION = "1.1"

# Allowed `response_kind` values. Every response that emits a
# structured_summary picks one of these. "folder_summary" is the
# default for the Gemini-driven full-summary path; the two open_items_*
# kinds come from the deterministic short-circuit handlers.
_RESPONSE_KINDS = frozenset({
    "folder_summary",       # full Gemini-synthesized summary
    "open_items_only",      # focused checklist response (claim or property)
    "open_items_unknown",   # cautious fallback for unrecognized folders
})

# Allowed `folder_purpose` values. Mirrors _classify_folder_purpose output.
_FOLDER_PURPOSES = frozenset({"claim_restoration", "property_appraisal", "unknown"})

# Allowed `checklist_name` values. Mirrors _pick_checklist_for_purpose output.
_CHECKLIST_NAMES = frozenset({"claim_default", "property_default", "unknown"})

# Allowed open-items `status` values. snake_case, machine-readable,
# distinct from the user-facing display strings (which live in the
# renderer's style maps). Persisted as-is.
_OPEN_ITEM_STATUSES = frozenset({"found", "needs_review", "not_found"})

# Allowed `confidence` values on key_facts and timeline entries. None
# is permitted (caller didn't specify). Anything outside this set is
# coerced to None during normalization.
_FACT_CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})

# ─── structured_fields extraction (v1) ──────────────────────────────────
# Derives a flat, typed business-field object from the normalized
# structured_summary so future BigQuery rows can be queried as proper
# columns instead of nested JSON. v1 covers a narrow set of fields
# common to property/appraisal AND claim/restoration folders.
#
# Field shape: {value, confidence, source_file} for every field, always
# present. value=None when the field couldn't be extracted from the
# available evidence (e.g. insurance_carrier on a property folder).
# Stable shape -- BigQuery and any downstream consumer can rely on the
# same 16 keys appearing in every persisted row.

# Status fields are derived from open_items. Each maps a flat business
# field to a bucket: when open_items has one or more entries in that
# bucket, the field's status is the BEST status across those entries
# (found > needs_review > not_found). This handles the property
# checklist's three contract-bucket entries cleanly: if any one is
# found, contract_status=found.
_STATUS_FIELD_BUCKETS = {
    "contract_status":   "contract",
    "insurance_status":  "insurance",
    "estimate_status":   "estimate",
    "invoice_status":    "invoice",
    "inspection_status": "report",
    "photos_status":     "photos",
}

# Value fields are extracted by regex-matching key_fact labels against
# a list of synonym patterns. First match wins -- if Gemini emits
# multiple facts that match the same pattern (e.g. two appraisals from
# different years), we take the first one. Patterns are compiled
# lazily; \b word boundaries keep them targeted.
#
# When designing patterns, the goal is to be permissive about phrasing
# variations Gemini might produce, but strict enough to avoid pulling
# in adjacent concepts. For example, "appraised_value" must match
# "Opinion of value" and "Appraised value" but NOT "Sale price" or
# "Appraisal invoice total".
_KEY_FACT_FIELD_PATTERNS: "List[tuple[str, List[str]]]" = [
    # field_name, [regex patterns to match against lowercased label]
    ("property_address", [
        r"^property\s+address\b",
        r"^address\b",
        r"^subject\s+(?:property\s+)?address\b",
    ]),
    ("appraised_value", [
        # "Opinion of value", "Appraised value", "Appraised Value (2024)", etc.
        # Excludes "Sale price" and "Appraisal invoice total" by requiring
        # the word "value" and not allowing "invoice" / "sale" / "price"
        # to follow.
        r"^opinion\s+of\s+value\b",
        r"^appraised\s+value\b",
        r"^appraisal(?:\s+(?:value|amount))\b",
        r"^market\s+value\b",
    ]),
    ("appraisal_effective_date", [
        r"^appraisal\s+(?:effective\s+)?date\b",
        r"^effective\s+date\s+of\s+appraisal\b",
        r"^date\s+of\s+(?:appraisal|opinion)\b",
    ]),
    ("insurance_carrier", [
        r"^(?:insurance\s+)?(?:carrier|insurer)\b",
        r"^insurance\s+(?:company|provider)\b",
    ]),
    ("claim_number", [
        r"^claim\s+(?:number|no\.?|id)\b",
        r"^claim\s*#",
    ]),
    ("estimate_total", [
        r"^estimate\s+total\b",
        r"^total\s+estimate\b",
        r"^(?:scope|estimate)\s+amount\b",
        # Scope-prefixed estimate labels: Gemini frequently emits these
        # for restoration jobs that contain multiple sub-estimates
        # (asbestos abatement, mitigation, pack-out, etc.). The label
        # carries the scope prefix and ends with "estimate <metric>".
        # Examples in corpus:
        #   Asbestos Abatement Estimate Total
        #   Pack Out/Pack Back & Storage Estimate Total
        #   Emergency Mitigation Services Estimate Total
        #   Pack-back estimate total
        #   Pack-back Estimate Value (RCV)
        #   Asbestos Abatement Estimate (RCV)        <- paren form
        #   Emergency Mitigation Services Estimate (Replacement Cost Value)
        # The trailing metric word can be total/amount/value/RCV; it can
        # appear after a space OR inside an immediate parenthetical. The
        # prefix is anything ending in 'estimate'.
        # First match wins per field; source_file disambiguates which
        # specific estimate produced the value. If a folder has multiple
        # sub-estimates and a rolled-up total, consumers should join on
        # source_file to see which row this number came from.
        r"^.+\s+estimate\s+\(?(?:total|amount|value|rcv|replacement)\b",
        # "Total <scope> Estimate" form. Gemini sometimes leads with
        # "Total" and ends with "Estimate" plus an optional parenthetical:
        #   Total Repair Estimate (RCV)
        #   Total Mold Remediation Estimate (RCV)
        #   Total Asbestos Abatement Estimate (RCV)
        # The \b after 'estimate' allows a trailing paren or end-of-string.
        r"^total\s+.+\s+estimate\b",
    ]),
    ("invoice_total", [
        # Note: matches "Appraisal Invoice Total" -- consumers inspecting
        # source_file can disambiguate. For a claim folder this would be
        # work-done invoices; for a property folder it might be the
        # appraisal fee. v1 accepts both; v2 can split if needed.
        r"^(?:.+\s)?invoice\s+(?:total|amount)\b",
        r"^total\s+invoice\b",
    ]),
    ("inspection_date", [
        r"^inspection\s+date\b",
        r"^date\s+of\s+inspection\b",
    ]),
]
# Compile patterns once at module load.
_KEY_FACT_FIELD_RES: "List[tuple[str, List]]" = [
    (field, [re.compile(p, re.IGNORECASE) for p in pats])
    for field, pats in _KEY_FACT_FIELD_PATTERNS
]

# Complete list of structured_fields keys. Used by tests/validators to
# verify the contract; the derivation function below produces exactly
# this set every time, regardless of whether values were extractable.
_STRUCTURED_FIELDS_KEYS = frozenset({
    "property_address",
    "folder_name",
    "folder_purpose",
    "appraised_value",
    "appraisal_effective_date",
    "insurance_carrier",
    "claim_number",
    "estimate_total",
    "invoice_total",
    "inspection_date",
    "contract_status",
    "insurance_status",
    "estimate_status",
    "invoice_status",
    "inspection_status",
    "photos_status",
})

# Status precedence: when a bucket has multiple open_items entries
# (e.g. property checklist's three contract entries), pick the best.
_STATUS_RANK = {"found": 2, "needs_review": 1, "not_found": 0}

# ─── structured_summary persistence sink ──────────────────────────────────
# Append-only JSONL writer for normalized structured_summary objects.
# Every folder-aware response writes one event; factual Q&A (which
# emits structured_summary=None) writes nothing because the emit sites
# never call this for those paths.
#
# This is a stepping stone toward BigQuery. The records collected here
# inform the eventual table shape -- we deliberately do NOT transform,
# flatten, or add envelope fields. One row = one structured_summary
# object, exactly as the normalizer emitted it.
#
# Path is anchored to the repo root (derived from this file's location),
# not cwd, so the Flask server can be launched from anywhere.
_PERSIST_DIR = (Path(__file__).resolve().parent.parent
                / "data" / "structured_summaries")
_PERSIST_FILE = _PERSIST_DIR / "structured_summary_events.jsonl"

def _persist_structured_summary(obj: Dict) -> None:
    """Append one structured_summary object to the JSONL sink.

    Best-effort -- failures are logged and swallowed so a disk problem
    can never break the chat response. The caller invokes this AFTER
    _normalize_structured_summary so the object is already validated
    and schema-stable.

    Idempotent in the sense that calling it twice on the same object
    writes two identical rows -- that's a feature, not a bug, since
    rerunning the same query is a legitimate event worth logging.

    No locking. Single Flask process = single writer; if we ever go
    multi-process, switch to an OS file lock or a real queue.
    """
    if not isinstance(obj, dict) or not obj:
        return
    try:
        _PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        # default=str protects against any stray non-JSON-serializable
        # value (e.g. a datetime that slipped through). Better to write
        # a stringified version than to lose the record entirely.
        line = json.dumps(obj, default=str, ensure_ascii=False)
        with open(_PERSIST_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        # Compact log line so re-runs show up in the server console.
        # We log the response_kind + folder_name only, not the full
        # object -- the file IS the full record.
        print(
            f"[persist] wrote 1 event "
            f"kind={obj.get('response_kind')!r} "
            f"folder={obj.get('folder_name')!r} "
            f"-> {_PERSIST_FILE.name}"
        )
    except Exception as e:
        # Persistence is best-effort. Print the error and keep going --
        # the chat response has already been built and is on its way
        # back to the user; failing to record an analytics row must
        # not surface to them.
        print(f"[persist] ERROR writing structured_summary: {e}")

# ─── Vertex rate-limit guards ─────────────────────────────────────────────
# Google's `search_requests_regional` quota = 300/min, NOT adjustable.
# Windowed limiter: never exceed 240 calls in any rolling 60-second window
# (240 = 80% of cap, leaves headroom for bursts and other code paths).
VERTEX_MAX_PER_MINUTE = 200
_vertex_call_times: deque = deque()

def _throttle_vertex_call():
    """Block until making one more search call would not exceed the cap.
    Drops timestamps older than 60s, sleeps if we're at the ceiling."""
    now = time.time()
    cutoff = now - 60.0
    while _vertex_call_times and _vertex_call_times[0] < cutoff:
        _vertex_call_times.popleft()
    if len(_vertex_call_times) >= VERTEX_MAX_PER_MINUTE:
        # Wait until the oldest call ages out of the window
        sleep_for = (_vertex_call_times[0] + 60.0) - now + 0.05
        if sleep_for > 0:
            print(f"[throttle] At {len(_vertex_call_times)}/min cap — sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
    _vertex_call_times.append(time.time())


# Module-level result cache. Key: normalized query string. Value: (sources,
# num_results, timestamp). Lets repeated identical questions skip Vertex.
_GLOBAL_RESULT_CACHE: Dict[str, tuple] = {}
_GLOBAL_CACHE_TTL = 1800  # 30 minutes — same query rarely changes its answer

def _cache_key(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())

def _cache_get(query: str):
    key = _cache_key(query)
    hit = _GLOBAL_RESULT_CACHE.get(key)
    if not hit:
        return None
    sources, num_results, ts = hit
    if time.time() - ts > _GLOBAL_CACHE_TTL:
        _GLOBAL_RESULT_CACHE.pop(key, None)
        return None
    return sources, num_results

def _cache_put(query: str, sources, num_results):
    _GLOBAL_RESULT_CACHE[_cache_key(query)] = (sources, num_results, time.time())
    # Cap cache size to prevent unbounded growth
    if len(_GLOBAL_RESULT_CACHE) > 200:
        oldest = sorted(_GLOBAL_RESULT_CACHE.items(), key=lambda kv: kv[1][2])[:50]
        for k, _ in oldest:
            _GLOBAL_RESULT_CACHE.pop(k, None)

SYSTEM_PROMPT = """You are a document intelligence assistant.
You help the user find information in their indexed documents about jobs, properties, permits, loans, appraisals, and claims.
You will receive search results from the document index plus conversation history.

RULES:
1. Answer directly and conversationally - no fluff
2. Cite specific documents when making claims
3. If search results are empty or irrelevant, say so clearly
4. Highlight key numbers, dates, and names
5. Keep responses under 250 words unless asked for more detail
6. When unsure, acknowledge it - don't make up information"""

def _load_creds():
    for key in [
        Path(__file__).resolve().parent.parent / "service-account.json",
        Path(__file__).resolve().parent / "service-account.json",
    ]:
        if key.exists():
            return service_account.Credentials.from_service_account_file(
                str(key), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return creds

def _safe_struct_get(struct, key, default=""):
    if struct is None: return default
    try:
        if hasattr(struct,"get"): val = struct.get(key, default)
        else: val = struct[key] if key in struct else default
        return str(val or default)
    except: return default

NO_RESULT = ("no results could be found","try rephrasing","i could not find","summary could not be generated")

def _is_empty(text):
    if not text: return True
    return any(m in text.lower() for m in NO_RESULT)

@dataclass
class ChatMessage:
    role: str
    text: str
    timestamp: float = field(default_factory=time.time)

@dataclass
class ChatSession:
    session_id: str
    history: List[ChatMessage] = field(default_factory=list)
    job_context: Optional[str] = None
    last_active: float = field(default_factory=time.time)
    last_search_query: Optional[str] = None
    last_search_time: float = 0
    cached_sources: List[Dict] = field(default_factory=list)

@dataclass
class IntelligenceResponse:
    answer: str
    sources: List[Dict]
    search_results: int
    confidence: str
    job_context: Optional[str]
    suggested_followups: List[str]
    # Structured object companion to `answer`. Currently populated only by
    # the folder-summary path; None for every other response shape. Keys
    # when present:
    #   overview            (str)
    #   key_facts           (list[{label, value, sources}])
    #   timeline            (list[{date, event, sources}])
    #   observations        (list[str] -- evidence-based notes)
    #   document_inventory  ({bucket: [{name, uri, doc_type}]})
    #   open_items          (list[{label, bucket, status, strict_count, total_count}])
    #   folder_name         (str)
    #   file_count_total    (int)
    #   file_count_in_dossier (int)
    # The chat answer is a COMPACT view of this object. Reports / analytics
    # / future BigQuery extracts read from the structured object directly
    # so the user-facing prose stays terse without losing detail.
    structured_summary: Optional[Dict] = None

def _extract_job_context(text):
    """Extract a property address or job ID from free-form user text.

    Returns a normalized, title-cased label suitable for use as
    ``session.job_context`` -- e.g. "27 Manor Drive" -- or None if no
    address/job ID is found.

    Match rules (in priority order):
      1. "<1-5 digits> <Word> [<Word> ...]" -- the leading street number
         followed by 1-5 capitalized-or-lowercased words. We accept
         lowercase because most chat queries are typed lowercase
         ("give me a summary on 27 manor drive"); the prior regex
         required uppercase first letters and silently dropped these.
      2. JOB-#### or JOB_#### style identifiers (e.g. JOB-2024-17).

    Once matched, trailing STOPWORDS like "summary", "docs", "please"
    are stripped (so "27 manor drive please" -> "27 Manor Drive"),
    and remaining words are normalized with .title() so downstream
    code (folder detection, badge display) sees a consistent label
    regardless of the user's input casing.

    Returns None when no plausible address or job ID is in the text --
    callers fall back to whatever ``session.job_context`` already held.
    """
    STOPWORDS = {
        "tell", "show", "me", "about", "the", "of", "for", "on", "get",
        "find", "what", "is", "are", "give", "summary", "photos", "photo",
        "docs", "documents", "please", "do", "we", "have", "any",
    }
    # Street-number-led address. \b(\d{1,5}) anchors on the number;
    # the trailing group accepts 1-5 word tokens that can begin with
    # either case (this is the key change vs. the previous regex,
    # which used [A-Z] and dropped every lowercase property name).
    m = re.search(
        r"\b(\d{1,5})\s+([A-Za-z][a-zA-Z\.\'']+(?:\s+[A-Za-z][a-zA-Z\.\'']+){0,4})\b",
        text,
    )
    if m:
        num = m.group(1)
        words = m.group(2).split()
        # Strip trailing filler words ("summary", "please", etc.) so
        # "27 manor drive summary" normalizes to "27 Manor Drive".
        while words and words[-1].lower() in STOPWORDS:
            words.pop()
        if words:
            return f"{num} " + " ".join(w.title() for w in words)
    # JOB-####-## fallback identifier (case-insensitive). Used by some
    # legacy work-order references; harmless if never present.
    job_id = re.search(r"\bJOB[-_]\d{4}[-_]\d{2,4}\b", text, re.IGNORECASE)
    if job_id:
        return job_id.group(0).upper()
    return None

def _score(n):
    if n == 0: return "none"
    if n >= 5: return "high"
    if n >= 2: return "medium"
    return "low"

def _followups(query, ctx):
    q = query.lower()
    s = []
    if any(w in q for w in ["loan","draw","balance","payment"]): 
        s += ["Any other draws?","Current loan balance?"]
    elif any(w in q for w in ["permit","inspection","certificate"]): 
        s += ["When does the permit expire?","Are there any failed inspections?"]
    elif any(w in q for w in ["apprais","value","comparable"]): 
        s += ["Comparable sales used?","Site value?"]
    elif any(w in q for w in ["claim","insurance","adjuster"]): 
        s += ["Who is the insurer?","Approved scope amount?"]
    elif any(w in q for w in ["owner","lender","contact"]): 
        s += ["Permits for this owner?","Who is the lender?"]
    else: 
        s += ["Any permits on file?","What documents exist for this job?"]
    if ctx: s.append(f"More on {ctx}?")
    return s[:3]

class JobIntelligence:
    def __init__(self):
        self._creds = _load_creds()
        self._sessions = {}
        self._use_gemini = False
        self._tools = None

        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            try:
                genai.configure(api_key=api_key)
                # Only register tools if the shared fetcher is importable
                if _FETCH_AVAILABLE:
                    self._tools = [self._build_tools()]
                self._gemini = genai.GenerativeModel(
                    model_name=GEMINI_MODEL,
                    system_instruction=SYSTEM_PROMPT,
                    tools=self._tools,
                )
                self._use_gemini = True
                tool_state = "with tools" if self._tools else "no tools"
                print(f"[Phase4] Gemini synthesis ON ({GEMINI_MODEL}) {tool_state}")
            except Exception as e:
                print(f"[Phase4] Gemini init failed: {e}")
        else:
            print("[Phase4] No GEMINI_API_KEY — direct search results only")

    @staticmethod
    def _build_tools():
        """Declare get_document_by_name as a Gemini tool."""
        get_doc_tool = genai.protos.FunctionDeclaration(
            name="get_document_by_name",
            description=(
                "Fetches FULL text of ONE specific document by name (fuzzy match supported). "
                "Use this tool whenever: "
                "(a) the user mentions a specific document name, address, property, or filename "
                "(e.g. '106 madison avenue', 'Andover P&L', 'Northridge appraisal'), OR "
                "(b) the document excerpts in the prompt are empty / don't contain the requested info, OR "
                "(c) the user uses words like 'read', 'open', 'tell me about', 'show me' followed by anything that could be a document or address. "
                "The fuzzy matcher handles spaces vs hyphens vs underscores in filenames. "
                "HARD LIMIT: do NOT call this tool more than 2 times per user message. "
                "After 2 calls, answer from what you have."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "document_name": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description=(
                            "The document name, address, or filename hint from the user's question. "
                            "Pass the most-specific identifying phrase — e.g. for '106 madison avenue pdf' "
                            "pass '106 madison avenue', for 'tell me about the Andover invoice' pass 'Andover invoice'."
                        ),
                    ),
                },
                required=["document_name"],
            ),
        )
        return genai.protos.Tool(function_declarations=[get_doc_tool])

    def _dispatch_tool(self, name: str, args: dict) -> str:
        if name == "get_document_by_name":
            if not _FETCH_AVAILABLE:
                return ("get_document_by_name is currently unavailable. "
                        "Answer using the document excerpts already in the prompt.")
            doc_name = (args or {}).get("document_name", "").strip()
            result = _fetch_doc_by_name(doc_name)
            if not result.get("ok"):
                extras = ""
                if result.get("candidates"):
                    extras = f" Near-matches I do see: {', '.join(result['candidates'])}."
                return (
                    f"get_document_by_name: no exact match for {doc_name!r}.{extras} "
                    f"DO NOT tell the user 'no documents were retrieved' — the "
                    f"DOCUMENT EXCERPTS already in the prompt contain relevant "
                    f"material. Answer from those excerpts now and list the "
                    f"document names that appeared."
                )
            body = result.get("text") or "(empty document)"
            return f"get_document_by_name OK — file: {result['title']}\n\n{body}"
        return f"Unknown tool: {name}"

    def _run_tool_loop(self, prompt: str, max_rounds: int = 3) -> str:
        """Send prompt to Gemini and resolve tool calls until a final answer.

        HARD LIMIT: at most 2 total tool calls per user message. Without this,
        Gemini happily fires get_document_by_name dozens of times when it
        sees relevant filenames in the prompt, downloading each from GCS
        sequentially — which can take 5+ minutes per chat message."""
        chat = self._gemini.start_chat(enable_automatic_function_calling=False)
        msg: object = prompt
        total_tool_calls = 0
        TOTAL_TOOL_CALL_BUDGET = 2
        for round_n in range(max_rounds):
            try:
                resp = chat.send_message(msg)
            except Exception as e:
                return f"Gemini tool-loop error: {e}"
            try:
                parts = resp.candidates[0].content.parts
            except Exception:
                parts = []
            calls, texts = [], []
            for p in parts:
                fc = getattr(p, "function_call", None)
                if fc and getattr(fc, "name", ""):
                    calls.append(fc)
                else:
                    t = getattr(p, "text", "") or ""
                    if t:
                        texts.append(t)
            if not calls:
                return "".join(texts).strip() or (resp.text or "").strip()

            tool_responses = []
            for fc in calls:
                # Hard cap on total fetches per chat message.
                if total_tool_calls >= TOTAL_TOOL_CALL_BUDGET:
                    print(f"[diag] Tool-call budget exhausted ({total_tool_calls}/{TOTAL_TOOL_CALL_BUDGET}). "
                          f"Refusing further calls and forcing answer from existing context.")
                    tool_responses.append(
                        genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name=fc.name,
                                response={"result":
                                    "BUDGET EXHAUSTED. You have already used your "
                                    "document-fetch quota for this question. "
                                    "Answer the user NOW from the document excerpts "
                                    "already in the prompt and any tool results you "
                                    "have so far. Do NOT call this tool again."
                                },
                            )
                        )
                    )
                    continue
                total_tool_calls += 1
                try:
                    args = dict(fc.args) if fc.args else {}
                except Exception:
                    args = {}
                output = self._dispatch_tool(fc.name, args)
                tool_responses.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fc.name,
                            response={"result": output},
                        )
                    )
                )
            msg = tool_responses
        return "(Hit tool-call iteration limit without a final answer.)"

    def new_session(self):
        sid = str(uuid.uuid4())
        self._sessions[sid] = ChatSession(session_id=sid)
        return sid

    def get_session(self, sid):
        s = self._sessions.get(sid)
        if s and time.time() - s.last_active > SESSION_TTL:
            del self._sessions[sid]
            return None
        return s

    def _vertex_search(self, query: str) -> tuple[List[Dict], int]:
        """
        Vertex AI Search: RETRIEVAL ONLY
        - No LLM summarization (saves quota)
        - Returns raw document snippets
        - Fast and cheap
        """
        client = discoveryengine.SearchServiceClient(credentials=self._creds)

        # Ask Vertex for both snippets AND extractive segments. Some doc types
        # (short single-page invoices, scanned-text PDFs, .docx with tables)
        # only return content under one of these, not both. We then read
        # whichever fields actually have text.
        content_spec = discoveryengine.SearchRequest.ContentSearchSpec(
            snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                return_snippet=True,
                max_snippet_count=5,
            ),
            extractive_content_spec=discoveryengine.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
                max_extractive_segment_count=3,
                max_extractive_answer_count=3,
            ),
        )

        req = discoveryengine.SearchRequest(
            serving_config=SERVING_CONFIG,
            query=query,
            page_size=10,
            content_search_spec=content_spec,
        )

        print(f"[diag] Vertex search query={query!r} serving_config={SERVING_CONFIG!r}")

        # Cache check (module-level, cross-session) — repeated identical
        # questions skip the Vertex call entirely.
        cached = _cache_get(query)
        if cached is not None:
            print(f"[diag] Result cache HIT for {query!r}")
            return cached

        # Throttle: never exceed 240/min on the search API
        _throttle_vertex_call()

        # Single quick retry on 429. Used to be 3 retries with 19s backoff,
        # but that just hung the chat for a minute while making the quota
        # situation worse. Better to fail fast and let the user retry.
        results = None
        for attempt, backoff in enumerate([0, 3], start=1):
            if backoff:
                print(f"[diag] Backoff {backoff}s before retry {attempt}")
                time.sleep(backoff)
                _throttle_vertex_call()
            try:
                response = client.search(req)
                results = list(response)
                print(f"[diag] Vertex returned {len(results)} raw results (attempt {attempt})")
                break
            except gapi_exceptions.ResourceExhausted as qe:
                if attempt >= 2:
                    print(f"[diag] Vertex 429 after {attempt} attempts — giving up")
                    raise
                print(f"[diag] Vertex 429 on attempt {attempt} — will retry once")
            except Exception as _e:
                print(f"[diag] Vertex search EXCEPTION: {type(_e).__name__}: {_e}")
                raise

        if results is None:
            results = []

        sources = []
        seen = set()
        # Cap fallback fetches per query — each fetch is a GCS download +
        # text extraction round trip (1-15s per doc). Doing 10 = 2+ min wait.
        # Limit to first 3 docs that need fallback; rest get metadata-only.
        fallback_budget = 3
        for r in results:
            doc = r.document
            title = _safe_struct_get(doc.struct_data, "title", "")
            uri = (_safe_struct_get(doc.struct_data, "source_uri", "")
                   or _safe_struct_get(doc.struct_data, "gcs_uri", "")
                   or _safe_struct_get(doc.struct_data, "uri", ""))

            # Extract content text robustly from EVERY source Vertex provides.
            # For docs imported with content.rawBytes, snippets[] is usually
            # empty but extractive_segments[] / extractive_answers[] DO get
            # populated. We collect from all three.
            text_parts = []
            try:
                derived = doc.derived_struct_data
                if derived:
                    d = dict(derived) if not isinstance(derived, dict) else derived

                    for snip in (d.get("snippets") or []):
                        s = snip.get("snippet") if isinstance(snip, dict) else getattr(snip, "snippet", "")
                        if s:
                            text_parts.append(re.sub(r"<[^>]+>", "", str(s)))

                    for seg in (d.get("extractive_segments") or []):
                        s = seg.get("content") if isinstance(seg, dict) else getattr(seg, "content", "")
                        if s:
                            text_parts.append(str(s))

                    for ans in (d.get("extractive_answers") or []):
                        s = ans.get("content") if isinstance(ans, dict) else getattr(ans, "content", "")
                        if s:
                            text_parts.append(str(s))
            except Exception as e:
                print(f"[Vertex] snippet extract error: {e}")

            snippet_text = "\n".join(text_parts).strip()

            # FALLBACK FETCH — fills in content for top docs that Vertex
            # imported as rawBytes (those don't get auto-snippeted). Capped
            # at 3 per query (~10-30s total). Without this, the chat says
            # 'document excerpts are empty' for everything we imported.
            if (not snippet_text
                    and uri
                    and uri.startswith("gs://")
                    and _FETCH_AVAILABLE
                    and fallback_budget > 0):
                try:
                    fallback_budget -= 1
                    fetched = _fetch_doc_by_name(title or Path(uri).name)
                    if fetched.get("ok") and fetched.get("text"):
                        snippet_text = fetched["text"][:3000]
                        print(f"[diag] Fallback fetch hit for {title!r}: {len(snippet_text)} chars")
                except Exception as fe:
                    print(f"[diag] Fallback fetch failed for {title!r}: {fe}")

            if not title:
                title = _safe_struct_get(doc.derived_struct_data, "title", "")

            label = title or (Path(uri).name if uri else doc.id or "Document")

            if label and label not in seen:
                seen.add(label)
                sources.append({
                    "title": label,
                    "uri": uri,
                    "snippet": snippet_text[:3000],
                })

        # Cache the result for the next 5 min so repeated questions skip Vertex
        _cache_put(query, sources[:10], len(sources))

        return sources[:10], len(sources)

    # === Phase 1 helpers: folder-aware retrieval ========================

    def _enumerate_folder(self, folder_name: str) -> List[Dict]:
        """Return every file in the bucket whose path passes through `folder_name`,
        filtered to searchable extensions.

        Zero API cost. Walks the in-memory LocalFileIndex._files list and
        does normalized-segment matching identical to the Phase 0 endpoint.
        Files with non-searchable extensions (.js.download, .css, .axd, .gif,
        ToThumbnail noise, etc.) are excluded -- this is what stops the
        Pampinella case from drowning in 156 web-export junk files.

        Returns: list of {"name", "uri", "path", "subfolder"} dicts.
        """
        if not folder_name or not _LOCAL_INDEX_AVAILABLE:
            return []
        try:
            from local_index import _normalize
            idx = _get_local_index()
            folder_norm = _normalize(folder_name)
            if not folder_norm:
                return []

            out = []
            for entry in getattr(idx, "_files", []) or []:
                if len(entry) != 3:
                    continue
                _norm_name, real_name, gs_uri = entry
                # Extension filter -- accept both searchable (text-extractable)
                # and marker (presence-only) types. Junk like .css, .axd,
                # .js.download, etc. still excluded. The split between the
                # two categories is applied later in _build_folder_dossier.
                rl = real_name.lower()
                if not any(rl.endswith(ext) for ext in _FOLDER_ENUMERABLE_EXTS):
                    continue
                is_marker = any(rl.endswith(ext) for ext in _FOLDER_MARKER_EXTS)
                # Path extraction
                path = ""
                if gs_uri.startswith("gs://"):
                    rest = gs_uri[5:].split("/", 1)
                    if len(rest) == 2:
                        path = rest[1]
                if not path:
                    continue
                segments = path.split("/")
                # Match folder_norm against any folder segment (not the file).
                # Track which segment matched + collect the immediate subfolder
                # (the one right after the matched folder, if any) so we can
                # group output by subfolder for Mode 3 display.
                hit_idx = -1
                for i, seg in enumerate(segments[:-1]):
                    if _normalize(seg) == folder_norm:
                        hit_idx = i
                        break
                if hit_idx < 0:
                    continue
                # Subfolder = the segment immediately after the matched
                # folder, if it's not the file itself. Falls back to "" for
                # files directly inside the matched folder.
                subfolder = ""
                if hit_idx + 1 < len(segments) - 1:
                    subfolder = segments[hit_idx + 1]
                out.append({
                    "name":      real_name,
                    "uri":       gs_uri,
                    "path":      path,
                    "subfolder": subfolder,
                    "is_marker": is_marker,
                })
            return out
        except Exception as e:
            print(f"[Phase1] _enumerate_folder error: {e}")
            return []

    def _detect_query_intent(self, query: str) -> Optional[str]:
        """Match a query against _QUERY_INTENT_RULES and return the target
        document_type, or None if no rule fires.

        Pure regex — no Gemini call. Cheap enough to call on every Mode 1/2
        request. Returns the FIRST matching doc_type so order in the rules
        list matters (more specific patterns appear before more general ones).
        """
        if not query:
            return None
        q_lower = query.lower()
        for pattern, doc_type in _QUERY_INTENT_RULES:
            try:
                if re.search(pattern, q_lower):
                    return doc_type
            except re.error as e:
                # Bad regex shouldn't break ranking; log and continue.
                print(f"[Phase1] intent rule regex error {pattern!r}: {e}")
                continue
        return None

    def _rank_folder_files_by_relevance(self, folder_files: List[Dict],
                                         query: str) -> List[Dict]:
        """Sort folder files by query relevance for Mode 1 / Mode 2 reading.

        Two layered signals, in priority order:

        1. INTENT MATCH: if the query matches a rule in _QUERY_INTENT_RULES
           (e.g. "opinion of value" -> appraisal), every file whose inferred
           document_type matches the intent gets a large boost. This is what
           lets "opinion of value" surface FNMA-1004 forms whose filenames
           share zero keywords with the query.

        2. KEYWORD OVERLAP: distinct query words appearing in the filename
           or path add a smaller per-hit score, with filename hits weighted
           3x over path hits. This is the original signal and remains in
           place as a tiebreaker plus as the only signal when no intent
           rule fires.

        Files with zero score still appear at the end of the list (in
        original order) so Mode 1's top-5 has 5 picks even if nothing
        matches — the user explicitly named the folder, so every file in
        it is at least somewhat relevant.

        Pure local computation. No Gemini, no Vertex.
        """
        if not folder_files:
            return []

        # Intent detection runs unconditionally — even if query has no
        # distinctive words it can still classify (e.g. a one-word query
        # like "appraisal" has no 4+ char distinctive tokens but absolutely
        # has an intent).
        intent_doc_type = self._detect_query_intent(query)
        if intent_doc_type:
            print(f"[Phase1] query intent -> {intent_doc_type}")

        try:
            from local_index import _normalize, _strip_filler, _QUERY_STOP_WORDS
            norm_q = _normalize(query) or ""
            core_q = _strip_filler(norm_q) or norm_q
            q_words = set(core_q.split()) - _QUERY_STOP_WORDS
            # Distinctive = 4+ chars or contains digits. Prevents "of", "the",
            # etc. from boosting irrelevant files.
            q_distinctive = {
                w for w in q_words
                if len(w) >= 4 or any(c.isdigit() for c in w)
            }
        except Exception as e:
            print(f"[Phase1] rank: query normalization failed: {e}")
            q_distinctive = set()
            try:
                from local_index import _normalize
            except Exception:
                # Without _normalize we can't compute keyword overlap, but
                # we may still have an intent_doc_type. Fall through to a
                # simpler scoring path below.
                _normalize = lambda s: (s or "").lower()

        # If we have neither intent nor distinctive words, preserve original order.
        if not intent_doc_type and not q_distinctive:
            return list(folder_files)

        try:
            from local_index import _normalize
        except Exception:
            _normalize = lambda s: (s or "").lower()

        # Resolve the LocalFileIndex once so the inner _score function can
        # consult its persisted-classification sidecar without paying the
        # singleton-access cost per file. Falling back to None is safe --
        # _score handles missing index by skipping the Layer 4 lookup and
        # going straight to filename heuristics (Layer 3).
        try:
            _idx_for_score = _get_local_index() if _LOCAL_INDEX_AVAILABLE else None
        except Exception as e:
            print(f"[Phase1] index lookup for ranking unavailable: {e}")
            _idx_for_score = None

        def _score(rec):
            score = 0.0

            # ---- Intent boost (Layer 2/4) ----
            # Resolve the file's doc_type via the layered model:
            #   1. Layer 4: persisted classification from the doc_type
            #      sidecar JSON. Set by Phase5_oneDrive/onedrive_sync.py at
            #      ingestion time using metadata.content_classifier on the
            #      extracted text (or filename fallback if text was empty).
            #      This is the strongest signal -- content evidence beats
            #      filename evidence.
            #   2. Layer 3: filename pattern (FNMA/1004/URAR/etc) via
            #      _classify_doc_type_at_query. Used only when the sidecar
            #      has no entry for this URI -- e.g. a freshly-synced file
            #      that hasn't gone through the ingestion classifier yet,
            #      or a deployment where no sync has run since the
            #      classifier was added.
            if intent_doc_type:
                file_doc_type = ""
                uri = rec.get("uri", "")
                if _idx_for_score is not None and uri:
                    try:
                        file_doc_type = _idx_for_score.get_doc_type(uri)
                    except Exception:
                        file_doc_type = ""
                if not file_doc_type:
                    try:
                        file_doc_type = _classify_doc_type_at_query(rec.get("name", ""))
                    except Exception:
                        file_doc_type = ""
                if file_doc_type == intent_doc_type:
                    score += _INTENT_BOOST

            # ---- Keyword overlap (filename 3x, path 1x) ----
            if q_distinctive:
                name_norm = _normalize(rec.get("name", ""))
                path_norm = _normalize(rec.get("path", ""))
                name_words = set(name_norm.split())
                path_words = set(path_norm.split())
                name_hits = len(q_distinctive & name_words)
                path_hits = len(q_distinctive & path_words)
                score += (name_hits * 3) + path_hits

            return score

        scored = [(rec, _score(rec)) for rec in folder_files]
        # Stable sort: relevance desc, then original index asc.
        scored.sort(key=lambda pair: -pair[1])

        # Diagnostic: log the top-5 scoring decisions so the server console
        # makes it obvious whether the intent boost did its job.
        if intent_doc_type or q_distinctive:
            for rec, sc in scored[:5]:
                print(f"[Phase1 rank] score={sc:6.1f}  {rec.get('name', '')!r}")

        return [rec for rec, _s in scored]

    def _classify_query_mode(self, query: str, folder_name: str) -> tuple:
        """Classify a folder-scoped query into one of three modes.

        Returns (mode, target) where:
          mode   = "MODE_1" | "MODE_2" | "MODE_3"
          target = short noun phrase describing what the user wants
                   (used for prompt context; never user-visible)

        Strategy: ask Gemini Flash with a tight structured-output prompt.
        Cost is one fast model call (~50 tokens out, ~$0.0001). Falls
        back to a deterministic rule-based classifier if Gemini is
        unavailable so the system still works without an API key.

        MODE_1: specific factual lookup ("opinion of value of X",
                "closing date of Y", "who is the lender for Z").
                Read top-5 docs to find the value.
        MODE_2: focused synthesis ("status of X", "summary of Y",
                "where do we stand on Z"). Read top-10 docs.
        MODE_3: unbounded enumeration ("everything on X",
                "all docs for Y", "list of Z"). Read NOTHING; return
                a navigable manifest.

        Defaults to MODE_2 on any error -- safest middle ground.
        """
        if not self._use_gemini:
            return self._classify_query_mode_fallback(query)
        try:
            classifier_prompt = (
                f"Classify the user query into ONE of three modes.\n"
                f"Folder in scope: {folder_name}\n"
                f"User query: {query}\n\n"
                f"MODE_1 = user wants ONE specific fact, value, name, date, or amount.\n"
                f"  Examples: 'opinion of value', 'closing date', 'who is the lender', 'permit number'\n"
                f"MODE_2 = user wants synthesis or status of ONE matter.\n"
                f"  Examples: 'status of', 'summary of', 'where do we stand', 'tell me about'\n"
                f"MODE_3 = user wants an unbounded list / enumeration / inventory.\n"
                f"  Examples: 'everything on', 'all documents for', 'list of', 'what do we have'\n\n"
                f"Reply on one line in this exact format:\n"
                f"MODE_X|short noun phrase of what is wanted\n"
                f"Examples:\n"
                f"  MODE_1|opinion of value\n"
                f"  MODE_2|case status\n"
                f"  MODE_3|all documents\n\n"
                f"Now classify. Reply with ONE line, nothing else."
            )
            # Use a minimal-config call WITHOUT tools to avoid any tool-loop
            # overhead. Direct generate_content is fastest.
            classifier_model = genai.GenerativeModel(model_name=GEMINI_MODEL)
            resp = classifier_model.generate_content(classifier_prompt)
            txt = (resp.text or "").strip()
            # Parse "MODE_X|target" -- be lenient about whitespace.
            line = txt.split("\n")[0].strip()
            if "|" in line:
                mode_part, target_part = line.split("|", 1)
                mode = mode_part.strip().upper()
                target = target_part.strip()
            else:
                mode = line.strip().upper()
                target = ""
            if mode not in ("MODE_1", "MODE_2", "MODE_3"):
                print(f"[Phase1] classifier returned unknown mode {mode!r}; defaulting MODE_2")
                return ("MODE_2", "")
            print(f"[Phase1] classified {query!r} -> {mode} (target={target!r})")
            return (mode, target)
        except Exception as e:
            print(f"[Phase1] classifier error: {e}; falling back to rule-based")
            return self._classify_query_mode_fallback(query)

    def _classify_query_mode_fallback(self, query: str) -> tuple:
        """Deterministic backup classifier for when Gemini is unavailable.

        Last-resort only. The Gemini classifier is the long-term path
        because it understands phrasings we haven't seen. This fallback
        exists so the system stays functional without an API key.
        """
        ql = query.lower()
        # Mode 3 indicators: unbounded scope words.
        if any(w in ql for w in (
            "everything", "all docs", "all the docs", "all files",
            "all documents", "list all", "list of",
            "what do we have", "what do i have", "what are all",
            "every doc", "every file",
        )):
            return ("MODE_3", "")
        # Mode 1 indicators: pinpoint factual targets.
        if any(w in ql for w in (
            "price", "value", "amount", "cost", "date", "number",
            "who is", "what is the", "how much", "when did", "when was",
            "address", "phone", "email", "ein",
        )):
            return ("MODE_1", "")
        # Default: synthesis.
        return ("MODE_2", "")

    def _find_related_folders(self, folder_name: str) -> List[str]:
        """Return other indexed folders whose names share a distinctive token
        with ``folder_name``.

        Used only in MODE_3. A query like "everything on Pampinella" should
        enumerate every folder whose name relates to Pampinella -- not just
        the single highest-scoring folder that detect_property_in_query
        returned. Real OneDrive layouts have multiple folders per
        property/person/claim:
          Pampinella, Giacomo - Legal
          Pampinella 2120 6th has REBUILD
          Giacomo (Tenant Ezequias Pardim 516-939-5794) - 2120 6th St
        all relate to the same family/property. Detection picks one; this
        helper finds the rest.

        Token comparison uses the same distinctiveness rule as the ranker
        and filename-union helper: 4+ char or digit-containing words that
        are NOT in _FOLDER_CATEGORY_NOISE_WORDS. So Pampinella-Giacomo-Legal
        contributes tokens {pampinella, giacomo} (legal is noise) and a
        sibling folder is "related" iff it shares at least one of those.

        Returns folder NAMES (not paths). The MODE_3 caller will pass each
        to _enumerate_folder. Excludes the input folder_name itself --
        callers already enumerate it separately.

        Empty list when no folder context, no distinctive tokens, or index
        unavailable. Pure local computation; no GCS, no Vertex, no Gemini.
        """
        if not folder_name or not _LOCAL_INDEX_AVAILABLE:
            return []
        try:
            from local_index import _normalize, _FOLDER_CATEGORY_NOISE_WORDS
            idx = _get_local_index()
            seed_norm = _normalize(folder_name)
            seed_words = seed_norm.split() if seed_norm else []
            seed_distinctive = {
                w for w in seed_words
                if (w.isdigit() or any(c.isdigit() for c in w) or len(w) >= 4)
                and w not in _FOLDER_CATEGORY_NOISE_WORDS
            }
            if not seed_distinctive:
                return []

            known_folders = getattr(idx, "_property_folders", set()) or set()
            related: List[str] = []
            for other in known_folders:
                if not other or other == folder_name:
                    continue
                other_norm = _normalize(other)
                if not other_norm:
                    continue
                other_words = set(other_norm.split())
                if seed_distinctive & other_words:
                    related.append(other)
            return related
        except Exception as e:
            print(f"[Phase1] _find_related_folders error: {e}")
            return []

    def _collect_filename_matches(self, query: str, folder_name: str = "",
                                    top_n: int = 50) -> List[Dict]:
        """Return files whose FILENAME matches the folder's distinctive tokens.

        Used only in MODE_3 (broad enumeration). The user's "everything on X"
        mental model is broader than my folder-scoped enumeration: they want
        related-by-folder PDFs AND related-by-name PDFs, because not every
        "about X" file lives inside the X folder (e.g. "Re Pampinella claim"
        in Outlook Files/, invoices for X in Vendors/).

        Search subject: when ``folder_name`` is given, we extract its
        distinctive tokens and use those as the filename-search query --
        NOT the user's raw query string. This is the durable design:
        folder detection already resolved the meaningful subject of the
        question, and the union of "related by folder" with "related by
        name" only makes sense if both signals are about the same subject.
        Using the raw query risks false positives on scope-modifier words
        like "everything", "all", "show" that happen to appear in other
        files' names. Falls back to the raw query if no folder is given
        or if the folder has no distinctive non-noise tokens.

        Distinctive = words that are digit-containing or 4+ chars. Noise =
        words in _FOLDER_CATEGORY_NOISE_WORDS (the same list that folder
        detection trusts as "category words that aren't real anchors").
        For "Pampinella, Giacomo - Legal" that yields ``pampinella giacomo``
        (drops "legal" as a category noise word).

        Returns records in the SAME SHAPE as _enumerate_folder so the two
        lists can be unioned and handed to _build_mode_3_response with no
        schema branching: {name, uri, path, subfolder}.

        For filename matches the `subfolder` is the file's IMMEDIATE parent
        folder name -- not the detected query folder. That gives MODE_3's
        subfolder grouping an honest display ("in: Outlook Files",
        "in: Vendors") instead of pretending these files came from the
        scoped folder.

        Extension allowlist is applied (same constant the folder enumerator
        uses) so web-export junk never enters the union, even if a junk
        filename happens to contain the query word.

        Pure local computation. No GCS, no Vertex, no Gemini.
        """
        if not _LOCAL_INDEX_AVAILABLE:
            return []

        # Compute the actual search string. Prefer folder-derived distinctive
        # tokens; fall back to the raw query.
        search_subject = ""
        if folder_name:
            try:
                from local_index import _normalize, _FOLDER_CATEGORY_NOISE_WORDS
                folder_norm = _normalize(folder_name)
                folder_words = folder_norm.split() if folder_norm else []
                # Same distinctive-token rule the ranker uses elsewhere.
                distinctive = [
                    w for w in folder_words
                    if (w.isdigit() or any(c.isdigit() for c in w) or len(w) >= 4)
                    and w not in _FOLDER_CATEGORY_NOISE_WORDS
                ]
                if distinctive:
                    search_subject = " ".join(distinctive)
            except Exception as e:
                print(f"[Phase1] _collect_filename_matches: folder token extraction failed: {e}")
                search_subject = ""

        if not search_subject:
            # No folder context or folder had no distinctive tokens. Fall
            # back to the raw query. This preserves usefulness when the
            # union helper is called without a resolved folder, and is the
            # only path where scope-modifier words could leak through. The
            # MODE_3 caller always passes folder_name so in practice this
            # branch is exercised only by defensive fallbacks.
            search_subject = query or ""
        if not search_subject.strip():
            return []

        try:
            idx = _get_local_index()
            hits = idx.find(search_subject, top_n=top_n)
        except Exception as e:
            print(f"[Phase1] _collect_filename_matches: find() failed: {e}")
            return []

        out: List[Dict] = []
        for h in hits:
            name = h.get("name", "")
            uri  = h.get("uri", "")
            if not name or not uri:
                continue
            # Extension allowlist -- mirrors _enumerate_folder so junk
            # (.css, .js.download, .gif, .axd) never leaks into MODE_3.
            # Marker files (.html) are included here too since this code
            # path also surfaces folder contents; downstream display knows
            # which entries lack body text.
            nl = name.lower()
            if not any(nl.endswith(ext) for ext in _FOLDER_ENUMERABLE_EXTS):
                continue
            is_marker = any(nl.endswith(ext) for ext in _FOLDER_MARKER_EXTS)
            # Path extraction (same logic as _enumerate_folder).
            path = ""
            if uri.startswith("gs://"):
                rest = uri[5:].split("/", 1)
                if len(rest) == 2:
                    path = rest[1]
            if not path:
                continue
            segments = path.split("/")
            # Subfolder display value: immediate parent of the file. If the
            # file is at bucket root somehow, falls back to empty (which
            # _build_mode_3_response will render as "(top level)").
            parent = segments[-2] if len(segments) >= 2 else ""
            out.append({
                "name":      name,
                "uri":       uri,
                "path":      path,
                "subfolder": parent,
                "is_marker": is_marker,
            })
        return out

    def _build_mode_3_response(self, query: str, folder_name: str,
                                folder_files: List[Dict],
                                session: "ChatSession") -> "IntelligenceResponse":
        """Build the unbounded-enumeration response.

        Reads ZERO documents. Returns a structured manifest:
          - Short Gemini-written intro (1-2 sentences) that previews the list
          - All files as `sources` so the front-end renders clickable chips
          - Subfolder grouping in the answer text when the folder has
            substructure
          - Confidence='high' since we know exactly what's in the folder

        Cost: ONE Gemini call (~30 token input + ~50 token output) to write
        the intro. No document reads. Strictly cheaper than the pre-Phase-1
        path which would have read 5-8 documents on the same query.

        Capped at MODE_3_LIST_LIMIT (100) visible files. If the folder has
        more, we mention the count and offer a follow-up to expand.
        """
        total = len(folder_files)
        # Group by subfolder so the user can see the folder's structure.
        by_sub: Dict[str, List[Dict]] = {}
        for f in folder_files:
            sub = f.get("subfolder", "") or ""
            by_sub.setdefault(sub, []).append(f)

        truncated = total > MODE_3_LIST_LIMIT
        visible = folder_files[:MODE_3_LIST_LIMIT]

        # Build a short structured summary for the Gemini intro prompt.
        # We pass file titles + subfolder grouping, NOT contents.
        sub_summary_lines = []
        for sub, files in sorted(by_sub.items()):
            label = sub if sub else "(top level)"
            sub_summary_lines.append(f"  {label}: {len(files)} files")
            # Show a few example titles per subfolder so Gemini can speak
            # about the folder's character without reading anything.
            for f in files[:3]:
                sub_summary_lines.append(f"    - {f['name']}")
            if len(files) > 3:
                sub_summary_lines.append(f"    ... and {len(files) - 3} more")
        sub_summary = "\n".join(sub_summary_lines)

        intro = ""
        if self._use_gemini:
            try:
                intro_prompt = (
                    f"The user asked: {query}\n"
                    f"They are referring to a folder called {folder_name!r}, "
                    f"which contains {total} document(s) (after filtering out "
                    f"non-document files like images, scripts, and stylesheets).\n\n"
                    f"Folder contents grouped by subfolder:\n{sub_summary}\n\n"
                    f"Write a SHORT (1-2 sentence) friendly preview of what they will see in the file list below. "
                    f"Mention the total count and any subfolder structure. Do NOT list individual files "
                    f"(the file list is shown to the user separately). Do NOT speculate about contents. "
                    f"End with a brief offer to look into a specific document."
                )
                intro_model = genai.GenerativeModel(model_name=GEMINI_MODEL)
                resp = intro_model.generate_content(intro_prompt)
                intro = (resp.text or "").strip()
            except Exception as e:
                print(f"[Phase1 mode3] intro generation failed: {e}")
                intro = ""

        # Fallback intro if Gemini failed or is disabled.
        if not intro:
            sub_count = len([s for s in by_sub if s])
            sub_phrase = (
                f" organized across {sub_count} subfolder(s)" if sub_count > 1 else ""
            )
            intro = (
                f"Found {total} document(s) in {folder_name!r}{sub_phrase}. "
                f"Click any file below to open it, or ask me about a specific one."
            )

        if truncated:
            intro += (
                f"\n\n*Showing {MODE_3_LIST_LIMIT} of {total} -- ask 'show more' "
                f"to see the rest.*"
            )

        # Build sources list. The front-end already renders these as
        # clickable chips that link to /api/download?uri=...
        sources_out = []
        for f in visible:
            sources_out.append({
                "title":     f["name"],
                "uri":       f["uri"],
                "subfolder": f.get("subfolder", ""),
            })

        # Update session
        session.history.append(ChatMessage(role="user", text=query))
        session.history.append(ChatMessage(role="model", text=intro))
        session.last_active = time.time()
        # Cache so a second "everything on X" within 30 min is instant.
        session.last_search_query = query
        session.last_search_time = time.time()
        session.cached_sources = [
            {"title": s["title"], "uri": s["uri"], "snippet": ""}
            for s in sources_out
        ]

        return IntelligenceResponse(
            answer=intro,
            sources=sources_out,
            search_results=total,
            confidence="high",
            job_context=session.job_context,
            suggested_followups=(
                ["Show more documents", "Read the most recent one",
                 "What is the status?"]
                if truncated else
                ["Read the most recent one", "What is the status?",
                 "Summarize this folder"]
            ),
        )

    def _is_folder_summary_query(self, query: str) -> bool:
        """Return True iff the query is asking for a folder/claim summary.

        Pure regex over lowercased query. Matches summary/status/overview
        phrasing. False positives are tolerable here (would just produce
        a structured summary on a query that maybe didn't ask for one,
        which is still useful). False negatives mean MODE_2 falls through
        to the existing free-form synthesis, which is the current
        behavior -- also acceptable.

        No Gemini, no LLM. Sub-millisecond.
        """
        if not query:
            return False
        try:
            return bool(_FOLDER_SUMMARY_RE.search(query))
        except Exception:
            return False

    def _bucket_for_doc_type(self, doc_type: str, filename: str = "") -> str:
        """Map a raw doc_type to one of the display buckets in _DOC_TYPE_BUCKETS.

        Falls back to extension-based bucketing for files with no/unknown
        doc_type so .xlsx still lands in 'spreadsheet' rather than 'other'.
        Photo extensions land in 'photos' even though the live system
        doesn't index them (the bucket exists so future photo-pointer
        docs slot in cleanly).
        """
        dt = (doc_type or "").strip().lower()
        if dt in _DOC_TYPE_BUCKETS:
            return _DOC_TYPE_BUCKETS[dt]
        # Extension fallbacks
        nl = (filename or "").lower()
        if any(nl.endswith(e) for e in (".xlsx", ".xls", ".csv")):
            return "spreadsheet"
        if any(nl.endswith(e) for e in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp", ".gif")):
            return "photos"
        return "other"

    def _build_folder_dossier(self, query: str, folder_name: str,
                                ranked_files: List[Dict]) -> Dict:
        """Pre-process the top-N folder files into a structured dossier.

        Output shape:
          {
            "folder_name": str,
            "file_count_total": int,         # full folder size, not just top-N
            "file_count_in_dossier": int,    # how many files actually included
            "files": [                       # one entry per file in the dossier
              {
                "name": str, "uri": str, "path": str, "subfolder": str,
                "doc_type": str,             # raw doc_type from layered lookup
                "bucket": str,               # display bucket (appraisal, invoice, ...)
                "snippet": str,              # up to SUMMARY_DOSSIER_SNIPPET_CHARS
                "snippet_chars": int,        # actual length
                "has_text": bool,            # snippet non-empty after extraction
              }, ...
            ],
            "inventory": {bucket: [{name, uri, doc_type}], ...},  # grouped
          }

        Pulls doc_type via the layered lookup (sidecar -> filename heuristic).
        Fetches snippet via the same _fetch_doc_by_name path MODE_1/MODE_2
        already uses, so we leverage existing extraction. Limits each
        snippet to keep total token cost bounded.

        Pure local + GCS reads -- no Gemini.
        """
        # Resolve the local index once so we can read sidecar doc_types
        # without paying per-file singleton-access cost.
        idx_for_dossier = None
        if _LOCAL_INDEX_AVAILABLE:
            try:
                idx_for_dossier = _get_local_index()
            except Exception as e:
                print(f"[Phase1 summary] index unavailable for dossier: {e}")

        dossier_files: List[Dict] = []
        dossier_slice = ranked_files[:SUMMARY_DOSSIER_FILE_LIMIT]

        # Build the per-file char budget from the tier configuration.
        # tier_budgets[i] = char budget for the i-th file in the dossier.
        # Files beyond the configured total fall through to 0 (excluded).
        tier_budgets: List[int] = []
        for count, budget in SUMMARY_DOSSIER_TIER_SIZES:
            tier_budgets.extend([budget] * count)
        # Truncate or pad to match the slice size.
        if len(tier_budgets) < len(dossier_slice):
            # Defensive: if the tier config is shorter than the slice,
            # use the smallest configured budget for the overflow.
            min_budget = SUMMARY_DOSSIER_TIER_SIZES[-1][1] if SUMMARY_DOSSIER_TIER_SIZES else 200
            tier_budgets.extend([min_budget] * (len(dossier_slice) - len(tier_budgets)))

        # Diagnostic counters for the dossier-build summary log line.
        d_files_with_text = 0
        d_total_chars_kept = 0
        d_total_chars_available = 0

        for i, f in enumerate(dossier_slice):
            name = f.get("name", "")
            uri = f.get("uri", "")
            if not name or not uri:
                continue

            # Layered doc_type lookup: sidecar (Layer 4) -> filename (Layer 3).
            doc_type = ""
            if idx_for_dossier is not None:
                try:
                    doc_type = idx_for_dossier.get_doc_type(uri) or ""
                except Exception:
                    doc_type = ""
            if not doc_type:
                try:
                    doc_type = _classify_doc_type_at_query(name) or ""
                except Exception:
                    doc_type = ""

            bucket = self._bucket_for_doc_type(doc_type, name)
            char_budget = tier_budgets[i] if i < len(tier_budgets) else 0
            is_marker = bool(f.get("is_marker"))

            # Fetch snippet (re-uses the document_fetch pipeline that already
            # handles extracted text, manifests, OCR cache, etc.).
            # Marker files (.html etc.) skip the fetch entirely -- the
            # pipeline can't extract their text yet, and attempting the
            # fetch would waste a call and produce misleading errors.
            # They still appear in the inventory with has_text=False so
            # downstream code can see them and report honestly.
            snippet = ""
            full_text_len = 0
            has_text = False
            fetch_error = ""
            if is_marker:
                fetch_error = (
                    "marker file -- text extraction not currently supported "
                    "for this file type"
                )
            elif _FETCH_AVAILABLE and char_budget > 0:
                try:
                    fetched = _fetch_doc_by_name(name)
                    if fetched.get("ok") and fetched.get("text"):
                        full_text = fetched["text"]
                        full_text_len = len(full_text)
                        snippet = full_text[:char_budget]
                        has_text = bool(snippet.strip())
                        d_total_chars_available += full_text_len
                        d_total_chars_kept += len(snippet)
                        if has_text:
                            d_files_with_text += 1
                    elif not fetched.get("ok"):
                        fetch_error = fetched.get("error", "unknown") or "unknown"
                except Exception as fe:
                    # Fetcher errors are non-fatal -- the file still
                    # appears in the inventory, just without body text.
                    fetch_error = str(fe)[:100]
                    print(f"[Phase1 summary] fetch failed for {name!r}: {fe}")

            # Per-file diagnostic so we can see if a file silently came back
            # empty vs. yielded text that we then truncated. Critical for
            # debugging "why didn't the appraised value show up".
            if not has_text:
                reason = fetch_error or (
                    "empty extraction (likely scan with no OCR yet)"
                    if char_budget > 0 else "snippet budget=0 (file outside tier window)"
                )
                print(f"[Phase1 summary] {name!r}: no usable text -- {reason}")
            else:
                print(
                    f"[Phase1 summary] {name!r}: {full_text_len} chars extracted, "
                    f"kept first {len(snippet)} for dossier (tier {char_budget})"
                )

            dossier_files.append({
                "name":          name,
                "uri":           uri,
                "path":          f.get("path", ""),
                "subfolder":     f.get("subfolder", ""),
                "doc_type":      doc_type or "document",
                "bucket":        bucket,
                "snippet":       snippet,
                "snippet_chars": len(snippet),
                "full_text_len": full_text_len,
                "has_text":      has_text,
                "char_budget":   char_budget,
                "is_marker":     is_marker,
            })

        print(
            f"[Phase1 summary] dossier stats: {d_files_with_text}/{len(dossier_files)} files with text, "
            f"{d_total_chars_kept:,} chars in dossier (of {d_total_chars_available:,} available)"
        )

        # Group inventory by bucket for the prompt's structured inventory
        # section. Preserve dossier_files order within each bucket.
        # is_marker is propagated so downstream classifiers can see that
        # the folder has evidence even when the bucket is just 'other'.
        inventory: Dict[str, List[Dict]] = {}
        for f in dossier_files:
            inventory.setdefault(f["bucket"], []).append({
                "name":      f["name"],
                "uri":       f["uri"],
                "doc_type":  f["doc_type"],
                "is_marker": f.get("is_marker", False),
            })

        return {
            "folder_name":           folder_name,
            "file_count_total":      len(ranked_files),
            "file_count_in_dossier": len(dossier_files),
            "files":                 dossier_files,
            "inventory":             inventory,
        }

    def _compute_open_items_structured(self, inventory: Dict[str, List[Dict]],
                                         checklist: Optional[List[tuple]] = None,
                                         checklist_name: str = "claim_default") -> List[Dict]:
        """Compute the Open Items checklist as STRUCTURED DATA, not Markdown.

        Source of truth for the open-items feature. The output is a list
        of dicts (one per entry in the chosen checklist) carrying all
        evidence we have about that checklist item -- the rendering
        layer (Markdown, BigQuery, future JSON export) then derives its
        view from this list without re-doing the classification logic.

        Three states, deterministic from the inventory alone:
          - "found"        : at least one STRICT match in the bucket
          - "needs_review" : bucket has files but none are strict
                             (e.g. a deed in the contract bucket)
          - "not_found"    : bucket is empty

        Pure function of the inventory dict -- no LLM call, no
        interpretation, no fact-checking against the prose summary.
        Identical across re-runs of the same query, which is exactly
        what we want for analytics/reporting.

        Args:
          inventory: bucket -> list-of-items map from the dossier path.
          checklist: which checklist to apply. Defaults to
                     SUMMARY_OPEN_ITEMS_CHECKLIST (the claim/restoration
                     operational checklist), preserving the prior
                     single-checklist behavior for callers that don't
                     specify one.
          checklist_name: short label identifying which checklist was
                          used. Stamped on every output dict so reporting
                          can group/filter by checklist variant.

        Returns:
          [
            {
              "label":          str,   # user-facing heading text
              "bucket":         str,   # which inventory bucket this maps to
              "status":         str,   # "found" | "needs_review" | "not_found"
              "strict_count":   int,   # how many strict-match files in bucket
              "total_count":    int,   # how many files total in bucket
              "checklist_name": str,   # which checklist this entry came from
            },
            ...
          ]
        """
        if checklist is None:
            checklist = SUMMARY_OPEN_ITEMS_CHECKLIST
        out: List[Dict] = []
        for label, bucket_key, strict_doc_types in checklist:
            items = inventory.get(bucket_key, []) or []
            total_count = len(items)
            if total_count == 0:
                strict_count = 0
                status = "not_found"
            else:
                if strict_doc_types is None:
                    # Whole bucket counts as strict (unambiguous case).
                    strict_count = total_count
                else:
                    strict_set = set(strict_doc_types)
                    strict_count = sum(
                        1 for it in items
                        if (it.get("doc_type", "") or "").lower() in strict_set
                    )
                status = "found" if strict_count > 0 else "needs_review"
            out.append({
                "label":          label,
                "bucket":         bucket_key,
                "status":         status,
                "strict_count":   strict_count,
                "total_count":    total_count,
                "checklist_name": checklist_name,
            })
        return out

    @staticmethod
    def _pick_checklist_for_purpose(folder_purpose: str
                                     ) -> tuple[Optional[List[tuple]], str]:
        """Select the right checklist for a folder purpose.

        Returns (checklist, checklist_name). Returns (None, "unknown") for
        unknown folder purposes -- callers should NOT render a checklist
        in that case and should emit a cautious message instead.

        Mapping:
          - claim_restoration  -> SUMMARY_OPEN_ITEMS_CHECKLIST ("claim_default")
          - property_appraisal -> PROPERTY_FOLDER_CHECKLIST   ("property_default")
          - unknown            -> (None, "unknown")
        """
        if folder_purpose == "claim_restoration":
            return (SUMMARY_OPEN_ITEMS_CHECKLIST, "claim_default")
        if folder_purpose == "property_appraisal":
            return (PROPERTY_FOLDER_CHECKLIST, "property_default")
        return (None, "unknown")

    @staticmethod
    def _derive_structured_fields(normalized: Dict) -> Dict:
        """Derive a flat, typed business-field object from structured_summary.

        Pure function. Takes the already-normalized structured_summary
        (so key_facts/timeline/open_items are guaranteed to be in canonical
        shape) and produces a 16-key flat dict that future BigQuery rows
        can use as typed columns.

        Each field is a {value, confidence, source_file} triple. value is
        None when the field couldn't be extracted; consumers can branch on
        that without needing to check field presence. confidence and
        source_file are also None in that case.

        Value fields (e.g. appraised_value) are extracted from key_facts
        by matching the label against curated synonym patterns; first
        match wins. Status fields (e.g. contract_status) are derived
        from open_items by bucket, picking the best status when the
        bucket has multiple entries.

        Deterministic, no Gemini, idempotent. Safe to call repeatedly
        on the same normalized object.
        """
        # Empty-field template -- every key is always present.
        def _empty():
            return {"value": None, "confidence": None, "source_file": None}

        out = {field: _empty() for field in _STRUCTURED_FIELDS_KEYS}

        # ---- Identity fields (deterministic from structured_summary) --
        # These come straight from the normalized object, no extraction.
        # Confidence is "high" because they're not LLM-derived.
        fn = normalized.get("folder_name") or None
        if fn:
            out["folder_name"] = {
                "value":       fn,
                "confidence":  "high",
                "source_file": None,
            }
        fp = normalized.get("folder_purpose") or None
        if fp:
            out["folder_purpose"] = {
                "value":       fp,
                "confidence":  "high",
                "source_file": None,
            }

        # ---- Value fields from key_facts -------------------------------
        # Walk key_facts once, try each pattern set, first match wins per
        # field. Stops when all value-pattern fields are filled (small
        # optimization).
        key_facts = normalized.get("key_facts") or []
        filled = set()
        for kf in key_facts:
            label = (kf.get("label") or "").strip().lower()
            if not label:
                continue
            for field, regexes in _KEY_FACT_FIELD_RES:
                if field in filled:
                    continue
                if any(rx.search(label) for rx in regexes):
                    sources = kf.get("sources") or []
                    src = sources[0] if sources else None
                    out[field] = {
                        "value":       kf.get("value") or None,
                        "confidence":  kf.get("confidence"),
                        "source_file": src,
                    }
                    filled.add(field)
                    break
            if len(filled) == len(_KEY_FACT_FIELD_RES):
                break

        # ---- Date fields: fall back to timeline if key_facts missed ---
        # The timeline is often a better source for dates than key_facts
        # because Gemini puts dated events there preferentially. Map by
        # phrase-matching the event description.
        #
        # For appraisal_effective_date specifically, prefer events that
        # describe the value opinion itself ("opinion of value",
        # "appraised at", "appraisal completed") over events that
        # incidentally mention the word "appraisal" but are actually
        # about something else (e.g. "Invoice date for appraisal").
        # Two-pass: try strict matches first, fall back to permissive.
        if out["appraisal_effective_date"]["value"] is None:
            timeline = normalized.get("timeline") or []
            STRICT_APPRAISAL_RE = re.compile(
                r"\bopinion\s+of\s+value\b|\bappraisal\s+(?:completed|issued|effective)\b"
                r"|\bappraised\b",
                re.IGNORECASE,
            )
            PERMISSIVE_APPRAISAL_RE = re.compile(
                r"\bappraisal\b|\bopinion\s+of\s+value\b", re.IGNORECASE
            )
            matched = None
            # Pass 1: strict
            for tl in timeline:
                event = tl.get("event") or ""
                if STRICT_APPRAISAL_RE.search(event) and tl.get("date"):
                    matched = tl
                    break
            # Pass 2: permissive, only if strict found nothing
            if matched is None:
                for tl in timeline:
                    event = tl.get("event") or ""
                    if PERMISSIVE_APPRAISAL_RE.search(event) and tl.get("date"):
                        matched = tl
                        break
            if matched is not None:
                sources = matched.get("sources") or []
                src = sources[0] if sources else None
                out["appraisal_effective_date"] = {
                    "value":       matched.get("date"),
                    "confidence":  matched.get("confidence"),
                    "source_file": src,
                }
        if out["inspection_date"]["value"] is None:
            for tl in normalized.get("timeline") or []:
                event = (tl.get("event") or "").lower()
                if "inspection" in event and tl.get("date"):
                    sources = tl.get("sources") or []
                    src = sources[0] if sources else None
                    out["inspection_date"] = {
                        "value":       tl.get("date"),
                        "confidence":  tl.get("confidence"),
                        "source_file": src,
                    }
                    break

        # ---- Status fields from open_items by bucket ------------------
        # Aggregate by bucket -- the property checklist puts three
        # entries (Deed/title, Loan/financing, Contract/agreement) all
        # in the "contract" bucket; we want contract_status to reflect
        # the BEST of the three (found > needs_review > not_found). If a
        # bucket has no entries at all, the status field stays None
        # rather than "not_found" -- the absence of an open_items entry
        # for a bucket means the checklist didn't include it, not that
        # the documents were absent.
        open_items = normalized.get("open_items") or []
        # Group statuses by bucket.
        by_bucket: Dict[str, List[str]] = {}
        for oi in open_items:
            b = oi.get("bucket")
            s = oi.get("status")
            if not b or not s:
                continue
            by_bucket.setdefault(b, []).append(s)
        for field, bucket in _STATUS_FIELD_BUCKETS.items():
            statuses = by_bucket.get(bucket)
            if not statuses:
                # Bucket not on this folder's checklist -> leave field at
                # value=None. The consumer can distinguish "not
                # measured" from "not_found" this way.
                continue
            best = max(statuses, key=lambda s: _STATUS_RANK.get(s, -1))
            out[field] = {
                "value":       best,
                "confidence":  "high",   # deterministic, not Gemini-derived
                "source_file": None,     # not from a single file
            }

        return out

    @staticmethod
    def _normalize_structured_summary(loose: Dict,
                                       response_kind: str,
                                       query: str,
                                       sources: Optional[List[Dict]] = None,
                                       confidence: Optional[str] = None) -> Dict:
        """Coerce a loose structured-summary dict into the canonical schema.

        Single source of truth for the structured_summary shape contract.
        Every emitter (full Gemini summary, open-items-only short-circuit,
        unknown-folder cautious response) calls this with the loose dict
        it built plus the four context fields (response_kind, query,
        sources, confidence) that aren't part of the loose dict today.

        Guarantees on the returned object:
          * All schema-required top-level keys are present, never missing.
            Defaults are filled in for absent or wrong-typed values.
          * key_facts is a list of {label, value, confidence, sources}
            dicts. confidence is one of {"high", "medium", "low", None}.
            sources is always a list of strings.
          * timeline is a list of {date, event, confidence, sources}
            dicts. date is YYYY-MM-DD-shaped string or None (the original
            string is preserved when non-empty; no parsing). Same
            confidence/sources rules as key_facts.
          * document_inventory[bucket][*] always has {name, uri, doc_type,
            bucket}. bucket is promoted from the dict key onto each item
            so consumers can flatten without losing the grouping.
          * open_items items pass through (already canonical); status
            values outside the allowed enum are coerced to "not_found".
          * sources is always a list of {title, uri, subfolder} dicts.
          * response_kind, folder_purpose, checklist_name, confidence are
            coerced against their respective enum sets; out-of-range
            values fall back to a safe default rather than raise.
          * schema_version and generated_at are stamped here.

        Pure function -- no I/O, no side effects, no Gemini. Safe to
        call from any emitter. Idempotent: normalizing an already-
        normalized object returns an equivalent object.
        """
        # ---- Helpers --------------------------------------------------
        def _as_str(v, default=""):
            if isinstance(v, str):
                return v
            if v is None:
                return default
            return str(v)

        def _as_int(v, default=0):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        def _as_bool(v, default=False):
            if isinstance(v, bool):
                return v
            return default

        def _as_str_list(v):
            if not isinstance(v, list):
                return []
            out = []
            for x in v:
                if isinstance(x, str) and x.strip():
                    out.append(x.strip())
                elif isinstance(x, (int, float)) and not isinstance(x, bool):
                    out.append(str(x))
            return out

        def _norm_confidence(v):
            if isinstance(v, str) and v.lower() in _FACT_CONFIDENCE_VALUES:
                return v.lower()
            return None

        def _norm_fact(item):
            """Coerce one key_fact dict to {label, value, confidence, sources}.

            Extra fields Gemini emitted (e.g. its own per-fact metadata)
            are dropped here -- the contract is fixed. If we ever want
            to preserve extras, change this to spread + override.
            """
            if not isinstance(item, dict):
                return None
            label = _as_str(item.get("label")).strip()
            value = _as_str(item.get("value")).strip()
            if not label and not value:
                return None
            return {
                "label":      label,
                "value":      value,
                "confidence": _norm_confidence(item.get("confidence")),
                "sources":    _as_str_list(item.get("sources")),
            }

        def _norm_timeline_entry(item):
            """Coerce one timeline dict to {date, event, confidence, sources}.

            `date` is preserved as a string (or None) without parsing --
            we don't attempt to validate YYYY-MM-DD here because Gemini's
            output is best-effort and downstream consumers can re-parse.
            """
            if not isinstance(item, dict):
                return None
            event = _as_str(item.get("event")).strip()
            date_raw = item.get("date")
            date = _as_str(date_raw).strip() or None
            if not event and not date:
                return None
            return {
                "date":       date,
                "event":      event,
                "confidence": _norm_confidence(item.get("confidence")),
                "sources":    _as_str_list(item.get("sources")),
            }

        def _norm_inventory(inv):
            """Promote bucket name onto each inventory item.

            Output:  {bucket: [{name, uri, doc_type, bucket, is_marker}, ...], ...}
            Skips malformed items (non-dict) and empty buckets. The
            bucket key is duplicated on each item so a flat scan
            (sum(inv.values(), [])) yields self-describing rows.

            is_marker is preserved verbatim when present and coerced to a
            strict bool. Items that lack the key are normalized to False
            (default-safe -- the absence of the marker flag means "this
            is a normal text-extractable file", which is the right
            assumption for any inventory item written by code paths that
            don't yet propagate the flag).
            """
            out: Dict[str, List[Dict]] = {}
            if not isinstance(inv, dict):
                return out
            for bucket, items in inv.items():
                if not isinstance(items, list):
                    continue
                cleaned: List[Dict] = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    cleaned.append({
                        "name":      _as_str(it.get("name")),
                        "uri":       _as_str(it.get("uri")),
                        "doc_type":  _as_str(it.get("doc_type")),
                        "bucket":    _as_str(bucket),
                        "is_marker": bool(it.get("is_marker", False)),
                    })
                if cleaned:
                    out[bucket] = cleaned
            return out

        def _norm_open_item(item):
            """Validate an open_items entry. Coerces unknown status to not_found."""
            if not isinstance(item, dict):
                return None
            status = _as_str(item.get("status")).lower()
            if status not in _OPEN_ITEM_STATUSES:
                status = "not_found"
            return {
                "label":          _as_str(item.get("label")),
                "bucket":         _as_str(item.get("bucket")),
                "status":         status,
                "strict_count":   _as_int(item.get("strict_count")),
                "total_count":    _as_int(item.get("total_count")),
                "checklist_name": _as_str(item.get("checklist_name"), "unknown"),
            }

        def _norm_source(item):
            if not isinstance(item, dict):
                return None
            return {
                "title":     _as_str(item.get("title")),
                "uri":       _as_str(item.get("uri")),
                "subfolder": _as_str(item.get("subfolder")),
            }

        # ---- Coerce enums ---------------------------------------------
        kind = response_kind if response_kind in _RESPONSE_KINDS else "folder_summary"
        purpose = loose.get("folder_purpose")
        if purpose not in _FOLDER_PURPOSES:
            purpose = "unknown"
        checklist_name = loose.get("checklist_name")
        if checklist_name not in _CHECKLIST_NAMES:
            checklist_name = "unknown"
        # Confidence on the response itself is free-form-ish ("high",
        # "medium", "low", "none"); preserve common values and lower-case
        # them, but accept anything string-ish so we don't lose info.
        conf_str = _as_str(confidence).lower() if confidence is not None else ""
        if not conf_str:
            conf_str = None

        # ---- Lists ----------------------------------------------------
        key_facts = [
            f for f in (_norm_fact(x) for x in (loose.get("key_facts") or []))
            if f is not None
        ]
        timeline = [
            t for t in (_norm_timeline_entry(x) for x in (loose.get("timeline") or []))
            if t is not None
        ]
        observations = _as_str_list(loose.get("observations"))
        open_items = [
            o for o in (_norm_open_item(x) for x in (loose.get("open_items") or []))
            if o is not None
        ]
        sources_out = [
            s for s in (_norm_source(x) for x in (sources or []))
            if s is not None
        ]

        # ---- Assemble canonical object --------------------------------
        # Field order chosen for readability when serialized -- provenance
        # first, identity next, narrative middle, structured tail.
        canonical = {
            # Provenance
            "schema_version":        _STRUCTURED_SUMMARY_SCHEMA_VERSION,
            "response_kind":         kind,
            "generated_at":          datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query":                 _as_str(query),

            # Folder identity
            "folder_name":           _as_str(loose.get("folder_name")),
            "folder_purpose":        purpose,
            "checklist_name":        checklist_name,
            "file_count_total":      _as_int(loose.get("file_count_total")),
            "file_count_in_dossier": _as_int(loose.get("file_count_in_dossier")),

            # Narrative (may be empty -- response_kind tells consumers when)
            "overview":              _as_str(loose.get("overview")),
            "key_facts":             key_facts,
            "timeline":              timeline,
            "observations":          observations,

            # Structured
            "document_inventory":    _norm_inventory(loose.get("document_inventory")),
            "open_items":            open_items,
            "show_open_items":       _as_bool(loose.get("show_open_items")),

            # Self-contained sources mirror (also on the envelope, repeated
            # here so the structured object stands alone for persistence)
            "sources":               sources_out,

            # Response-level confidence mirrored from the envelope
            "confidence":            conf_str,
        }
        # Derive flat business fields from the canonical object. This is
        # the v1 "useful columns" layer that future BigQuery rows will
        # query against. Stable 16-key shape, always present, every key
        # carries {value, confidence, source_file} -- value=None when the
        # field couldn't be extracted from the available evidence.
        canonical["structured_fields"] = JobIntelligence._derive_structured_fields(canonical)
        return canonical

    @staticmethod
    def _classify_folder_purpose(inventory: Dict[str, List[Dict]],
                                  folder_name: str = "") -> str:
        """Classify a folder's purpose from its bucket distribution + name.

        Returns one of:
          - "claim_restoration"  : claim/restoration job folder. Fires when
                                   any of these are true:
                                     (a) content has insurance/estimate/
                                         photos buckets (strong evidence)
                                     (b) folder name contains claim/
                                         restoration vocabulary AND content
                                         has a supporting bucket (report,
                                         contract, or invoice) OR a marker
                                         file (HTML CompanyCam export etc.)
          - "property_appraisal" : real-estate / valuation folder. Has
                                   appraisal OR contract bucket AND the
                                   folder name has no claim signal.
          - "unknown"            : neither pattern clearly fires. Treated
                                   as conservative -- suppress Open Items
                                   unless the user explicitly asked.

        Pure function. Deterministic; no Gemini.

        Priority order matters. We check claim-by-content first because
        that's the strongest signal (a folder with actual insurance docs
        is definitely a claim folder regardless of name). Then we check
        claim-by-name-plus-supporting-content -- this is the v2 addition
        that catches claim folders whose files are mostly inspection
        reports and AOBs (which look property-shaped at the file level).
        Only then do we check the property pattern, and the property
        check is GATED by the absence of a claim-name signal so that a
        folder named "claim paid & closed" with a deed in it doesn't
        get pulled back to property_appraisal.

        2026-05-13 update: added folder-name signals because the bucket-
        only logic was returning 'unknown' on real claim folders whose
        files are dominated by inspection_report + contract + other (no
        insurance bucket). See corpus audit; ~67 claim_restoration folders
        existed in the inventory but zero records were classified as such.
        """
        # Admin/template/reference short-circuit. Folder names matching
        # _ADMIN_NAME_RE are aggregators (Bob's reference sheets, blank
        # forms, payroll archives) -- they should not be treated as
        # work-unit folders regardless of incidental file content. Short-
        # circuits to "unknown" rather than any concrete purpose, per
        # §5.8's lesson that flipping from one wrong purpose to another
        # breaks neighbors. See OPERATIONS.md §5.10.
        if folder_name and _ADMIN_NAME_RE.search(folder_name):
            return "unknown"

        present_buckets = {b for b, items in inventory.items() if items}
        has_strict_claim = bool(present_buckets & _CLAIM_RESTORATION_BUCKETS)
        has_property     = bool(present_buckets & _PROPERTY_APPRAISAL_BUCKETS)
        name_is_claim = bool(folder_name and _CLAIM_NAME_RE.search(folder_name))
        has_supporting   = bool(present_buckets & _CLAIM_SUPPORTING_BUCKETS)

        # Marker files (.html etc.) are independent evidence that the
        # folder exists and has content. Any marker file anywhere in the
        # inventory counts toward the claim-by-name path because the
        # alternative (treating marker-only folders as empty) wrongly
        # demotes valid claim folders whose only content is CompanyCam
        # HTML exports. Strict claim and property paths are unchanged --
        # markers don't fire claim_restoration on their own without a
        # claim-name signal.
        has_marker = any(
            item.get("is_marker")
            for items in inventory.values()
            for item in (items or [])
        )

        # Strongest signal: real claim-bucket content.
        if has_strict_claim:
            return "claim_restoration"

        # Folder-name claim signal + at least one supporting bucket or
        # marker file. Without that requirement we'd over-fire on any
        # folder whose name happens to mention water/fire/lead but has
        # no actual claim documents.
        if name_is_claim and (has_supporting or has_marker):
            return "claim_restoration"

        # Property pattern. Only fires if the folder name does NOT signal
        # a claim -- a claim folder with a deed in it should not be
        # pulled back to property_appraisal on the strength of the deed.
        if has_property and not name_is_claim:
            return "property_appraisal"

        return "unknown"

    @staticmethod
    def _should_render_open_items(folder_purpose: str, query: str) -> bool:
        """Decide whether to render the Open Items checklist in chat.

        Render rules:
          - User intent override always wins: if the query mentions
            "missing", "open items", "what's not", "checklist", etc.,
            render Open Items regardless of folder purpose.
          - Otherwise render only for "claim_restoration" folders, where
            the checklist's expectations actually match reality.
          - Suppress for "property_appraisal" and "unknown" by default.

        Note that suppressing the render does NOT remove the data --
        structured_summary["open_items"] is still populated for
        reporting/BigQuery. Only the chat-Markdown view is gated.
        """
        if query and _OPEN_ITEMS_INTENT_RE.search(query):
            return True
        return folder_purpose == "claim_restoration"

    @staticmethod
    def _render_open_items_markdown(open_items: List[Dict], style: str = "claim") -> str:
        """Render the structured open_items list as a compact Markdown block.

        Display-only -- the underlying truth lives in the structured list
        returned by _compute_open_items_structured.

        Style controls section heading and status phrasing so the same
        renderer works for both claim folders (operational checklist,
        "missing/not found" implies a gap to address) and property
        folders (informational inventory, "not seen" implies absence-of-
        evidence rather than absence-of-thing):

            style="claim"     -> heading "## Open Items"
                                 found        -> "found"
                                 needs_review -> "needs review"
                                 not_found    -> "not found"

            style="property"  -> heading "## Documents Present"
                                 found        -> "available"
                                 needs_review -> "review needed"
                                 not_found    -> "not seen in indexed inventory"

        Unknown style falls back to "claim" (the long-standing default).
        Cautious phrasing intentional in both styles: never "missing",
        never absolute.
        """
        if style == "property":
            heading = "## Documents Present"
            display = {
                "found":        "available",
                "needs_review": "review needed",
                "not_found":    "not seen in indexed inventory",
            }
            footer = (
                "_Reflects only documents currently indexed in the system; "
                "items marked 'not seen' may still exist in OneDrive._"
            )
        else:
            heading = "## Open Items"
            display = {
                "found":        "found",
                "needs_review": "needs review",
                "not_found":    "not found",
            }
            footer = "_Based only on the indexed folder inventory._"

        lines = [heading]
        for item in open_items:
            label = item.get("label", "")
            status = display.get(item.get("status", ""), item.get("status", ""))
            lines.append(f"- **{label}**: {status}")
        lines.append("")
        lines.append(footer)
        return "\n".join(lines)

    def _build_open_items_section(self, inventory: Dict[str, List[Dict]]) -> str:
        """Backward-compat wrapper: compute -> render Markdown.

        Retained so any external caller (and existing log statements) that
        used to invoke this directly keeps working. New code should use
        _compute_open_items_structured for data and
        _render_open_items_markdown for display.
        """
        return self._render_open_items_markdown(
            self._compute_open_items_structured(inventory)
        )

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        """Peel ```json ... ``` (or plain ``` ... ```) fences from a Gemini reply.

        Gemini Flash often returns JSON wrapped in a fenced code block even
        when explicitly told not to. Strip the wrapper so json.loads sees
        clean text. Idempotent on already-clean input.
        """
        if not text:
            return ""
        s = text.strip()
        # ```json\n ... \n```  or  ``` ... ```
        m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", s, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return s

    @staticmethod
    def _render_compact_summary_markdown(structured: Dict) -> str:
        """Render the structured_summary object as a compact chat Markdown view.

        The chat answer is intentionally NOT a reporting view. It shows:
          - one short paragraph (Summary)
          - up to 5-8 key facts as a clean bulleted list, each
            annotated with up to 2 source filenames in italics. Facts
            with no source are tagged '(unsourced - review)' so the
            operator can see when grounding is missing.
          - the deterministic Open Items checklist, BUT only when the
            folder's purpose makes that checklist meaningful. A property/
            appraisal folder doesn't need "Insurance / claim document:
            not found" -- nothing was missing because nothing was
            expected. The gating decision lives upstream in
            _build_folder_summary_response, which sets
            structured["show_open_items"]. This renderer just respects
            the flag.

        Everything else -- full inventory, full timeline, observations,
        per-fact sources/confidence, AND the full open_items list even
        when not rendered -- stays inside `structured_summary` for
        downstream consumers (reporting, BigQuery exports, future admin
        views). Suppressing the render does NOT remove the data.
        """
        # ---- Summary paragraph -----------------------------------------
        overview = (structured.get("overview") or "").strip()
        if not overview:
            folder = structured.get("folder_name", "") or "this folder"
            total = structured.get("file_count_total", 0)
            overview = (
                f"Folder **{folder}** has {total} indexed document(s). "
                f"No narrative summary was generated."
            )
        lines = ["## Summary", overview, ""]

        # ---- Key Facts (top 5-8, with inline source filenames) --------
        # Hard cap at 8 so the chat answer stays scannable. Reporting
        # consumers can read the full list from structured.key_facts.
        #
        # Each rendered fact is annotated with up to SOURCE_CAP source
        # filenames pulled from kf['sources'] (existing schema field,
        # populated by Gemini during normalization in
        # _normalize_structured_summary). Facts with no source are
        # tagged '(unsourced - review)' so the operator can see when
        # grounding is missing -- this is Search/Response Relevance v1
        # Step 1 (visible source grounding).
        #
        # The sources list is already a list of basenames per the
        # JSONL corpus (verified 2026-05-19); no URI/path stripping
        # is needed here.
        KEY_FACT_CAP = 8
        SOURCE_CAP = 2
        key_facts = structured.get("key_facts") or []
        if key_facts:
            lines.append("## Key Facts")
            for kf in key_facts[:KEY_FACT_CAP]:
                # Tolerate slightly off shapes: missing label, value, etc.
                label = (kf.get("label") or "").strip()
                value = (kf.get("value") or "").strip()
                sources = kf.get("sources") or []
                # Build the source annotation. Tolerate non-string
                # entries by coercing to str and stripping; drop blanks.
                shown_sources = [
                    str(s).strip() for s in sources[:SOURCE_CAP]
                    if s and str(s).strip()
                ]
                if shown_sources:
                    src_text = ", ".join(shown_sources)
                    annotation = f" *(sources: {src_text})*"
                else:
                    annotation = " *(unsourced - review)*"
                if label and value:
                    lines.append(f"- **{label}:** {value}{annotation}")
                elif value:
                    lines.append(f"- {value}{annotation}")
                elif label:
                    lines.append(f"- {label}{annotation}")
            lines.append("")

        # ---- Open Items (deterministic checklist) ----------------------
        # Gated by structured["show_open_items"]. False on property/appraisal
        # folders by default; True on claim/restoration folders; True
        # anywhere when the user's query explicitly asked about gaps,
        # missing items, completeness, or open items. The full open_items
        # list is still present in structured even when not rendered, so
        # BigQuery / future admin UI can read it.
        #
        # Rendering style follows folder_purpose: property folders get
        # the "Documents Present" framing (available/not seen), claims
        # get the operational "Open Items" framing (found/needs review/
        # not found). Without this, a property folder whose Open Items
        # render was triggered by user intent would still show the
        # claim-style heading.
        if structured.get("show_open_items"):
            open_items = structured.get("open_items") or []
            if open_items:
                render_style = (
                    "property"
                    if structured.get("folder_purpose") == "property_appraisal"
                    else "claim"
                )
                lines.append(JobIntelligence._render_open_items_markdown(
                    open_items, style=render_style
                ))

        return "\n".join(lines).rstrip() + "\n"

    def _build_open_items_only_response(self, query: str, folder_name: str,
                                         folder_files: List[Dict],
                                         session: "ChatSession") -> "IntelligenceResponse":
        """Focused 'what's missing / open items' response for a folder.

        Built when the user's query explicitly asks about completeness
        (missing items, open items, checklist, gaps) and a folder is in
        scope. This path:

          - Does NOT call Gemini. The checklist is deterministic from
            the inventory alone; we already have everything we need.
          - Does NOT call _build_folder_dossier. Building the dossier
            fetches snippets for every file in the folder, which is
            expensive and provides zero value for the open-items
            checklist (the checklist only needs bucket membership, not
            extracted text).
          - Does NOT re-render the prose overview / key facts. The user
            asked a focused question; the focused answer is the right
            answer. (If they want the broader picture, they ask for
            "summary".)
          - DOES populate the canonical structured_summary object with
            the same fields a full summary would, so reporting/BigQuery
            and any future frontend can read a consistent shape. Just
            the chat-Markdown view is different.

        Chat render:
          - One-line preamble naming the folder, doc count, and a brief
            note about what "not found" means.
          - The Open Items checklist rendered from the deterministic
            structured list.

        Cheap. Instant. Always on-topic.

        Args:
          folder_files: list of {name, uri, path, subfolder, doc_type, ...}
                        dicts from _enumerate_folder. Snippets not required.
        """
        total = len(folder_files)

        # ---- Build lightweight inventory --------------------------------
        # We only need bucket membership for the checklist. No snippet
        # fetching, no Gemini, no doc_type re-classification beyond what
        # _enumerate_folder already gave us. If doc_type is missing on a
        # file, fall back to the filename heuristic the dossier path uses.
        idx_for_open_items = None
        if _LOCAL_INDEX_AVAILABLE:
            try:
                idx_for_open_items = _get_local_index()
            except Exception:
                idx_for_open_items = None

        inventory: Dict[str, List[Dict]] = {}
        for f in folder_files:
            name = f.get("name", "")
            uri = f.get("uri", "")
            doc_type = (f.get("doc_type") or "").strip()
            # Layered fallback to match dossier path's behavior.
            if not doc_type and idx_for_open_items is not None:
                try:
                    doc_type = idx_for_open_items.get_doc_type(uri) or ""
                except Exception:
                    doc_type = ""
            if not doc_type:
                try:
                    doc_type = _classify_doc_type_at_query(name) or ""
                except Exception:
                    doc_type = ""
            bucket = self._bucket_for_doc_type(doc_type, name)
            # is_marker comes from _enumerate_folder (set by extension
            # against _FOLDER_MARKER_EXTS). Propagate it through so the
            # open_items_only/open_items_unknown inventory matches the
            # dossier path's shape. Default False keeps normal text-
            # extractable files un-flagged. The downstream _norm_inventory
            # step is what writes the flag into the persisted JSONL, so
            # setting it here is necessary but not sufficient -- it has
            # to also survive normalization.
            inventory.setdefault(bucket, []).append({
                "name":      name,
                "uri":       uri,
                "doc_type":  doc_type,
                "is_marker": bool(f.get("is_marker", False)),
            })

        # ---- Classify folder purpose first ------------------------------
        # Purpose drives which checklist we apply, the rendering style
        # ("Open Items" vs "Documents Present"), and the preamble. For
        # unknown folders we don't apply any checklist -- we don't yet
        # know what "should" be there, so saying anything is missing
        # would be misleading.
        folder_purpose = self._classify_folder_purpose(inventory, folder_name)
        checklist, checklist_name = self._pick_checklist_for_purpose(folder_purpose)

        # ---- Unknown folder: cautious early return ----------------------
        # No checklist applied. Tell the user what we can see (bucket
        # distribution as a one-liner) and explicitly state that we can't
        # determine requirements for this folder type yet. Preserves the
        # structured_summary shape so reporting consumers still get a
        # consistent envelope.
        if checklist is None:
            present_buckets = sorted(b for b, items in inventory.items() if items)
            bucket_summary = ", ".join(present_buckets) if present_buckets else "none"
            answer = (
                f"I can see **{folder_name}** has {total} indexed document(s) "
                f"across these categories: {bucket_summary}. I cannot determine "
                f"what documents are required for this folder type yet -- the "
                f"folder doesn't match a known claim/restoration or property/"
                f"appraisal pattern. If you can tell me what kind of folder "
                f"this is, I can give you a more specific checklist.\n"
            )
            structured: Dict = {
                "folder_name":           folder_name,
                "file_count_total":      total,
                "file_count_in_dossier": total,
                "overview":              "",
                "key_facts":             [],
                "timeline":              [],
                "observations":          [],
                "document_inventory":    inventory,
                "open_items":            [],  # no checklist applied
                "folder_purpose":        folder_purpose,
                "show_open_items":       False,
                "response_kind":         "open_items_unknown",
                "checklist_name":        checklist_name,
            }
            sources_out = [
                {
                    "title":     f.get("name", ""),
                    "uri":       f.get("uri", ""),
                    "subfolder": f.get("subfolder", ""),
                }
                for f in folder_files[:12]
            ]
            session.history.append(ChatMessage(role="user", text=query))
            session.history.append(ChatMessage(role="model", text=answer))
            session.last_active = time.time()
            session.last_search_query = query
            session.last_search_time = time.time()
            session.cached_sources = [
                {"title": s["title"], "uri": s["uri"], "snippet": ""}
                for s in sources_out
            ]
            print(
                f"[Phase1 open-items-only] folder={folder_name!r} "
                f"docs={total} purpose=unknown response=cautious"
            )
            # Normalize to canonical schema before returning.
            structured = self._normalize_structured_summary(
                structured,
                response_kind="open_items_unknown",
                query=query,
                sources=sources_out,
                confidence="medium",
            )
            # Persist for future reporting/analytics (best-effort).
            _persist_structured_summary(structured)
            return IntelligenceResponse(
                answer=answer,
                sources=sources_out,
                search_results=total,
                confidence="medium",
                job_context=session.job_context,
                suggested_followups=[
                    "Give me a summary",
                    "What documents do we have",
                ],
                structured_summary=structured,
            )

        # ---- Compute checklist using the purpose-appropriate template ---
        open_items_structured = self._compute_open_items_structured(
            inventory, checklist=checklist, checklist_name=checklist_name
        )
        # User explicitly asked, so always show.
        show_open_items = True

        structured: Dict = {
            "folder_name":           folder_name,
            "file_count_total":      total,
            "file_count_in_dossier": total,  # no dossier; we used the full inventory
            # The narrative fields stay present but empty -- a uniform
            # shape lets downstream consumers branch on response_kind
            # rather than on field presence.
            "overview":              "",
            "key_facts":             [],
            "timeline":              [],
            "observations":          [],
            "document_inventory":    inventory,
            "open_items":            open_items_structured,
            "folder_purpose":        folder_purpose,
            "show_open_items":       show_open_items,
            # Marker so future consumers can distinguish this from a
            # full summary without sniffing the answer text.
            "response_kind":         "open_items_only",
            "checklist_name":        checklist_name,
        }

        # ---- Render purpose-aware chat Markdown -------------------------
        if folder_purpose == "property_appraisal":
            preamble = (
                f"Here's the indexed document inventory for **{folder_name}** "
                f"({total} indexed document(s)). This is a property/appraisal-"
                f"style folder, so the list below shows what document types "
                f"are present vs. not seen -- it's informational, not a list "
                f"of required items."
            )
            render_style = "property"
        else:
            # claim_restoration
            preamble = (
                f"Here's what the checklist looks like for **{folder_name}** "
                f"({total} indexed document(s)). \"Not found\" means the "
                f"document type isn't in the indexed inventory -- it may "
                f"still exist in OneDrive without being indexed yet."
            )
            render_style = "claim"

        answer = (
            preamble
            + "\n\n"
            + self._render_open_items_markdown(open_items_structured, style=render_style)
            + "\n"
        )

        # ---- Source chips ----------------------------------------------
        # Show the indexed files as chips; cap at a reasonable count so a
        # huge folder doesn't drown the chat in chips.
        chip_cap = 12
        sources_out = [
            {
                "title":     f.get("name", ""),
                "uri":       f.get("uri", ""),
                "subfolder": f.get("subfolder", ""),
            }
            for f in folder_files[:chip_cap]
        ]

        # ---- Session bookkeeping ---------------------------------------
        session.history.append(ChatMessage(role="user", text=query))
        session.history.append(ChatMessage(role="model", text=answer))
        session.last_active = time.time()
        session.last_search_query = query
        session.last_search_time = time.time()
        session.cached_sources = [
            {"title": s["title"], "uri": s["uri"], "snippet": ""}
            for s in sources_out
        ]

        # Compact diagnostic so server log shows the route taken.
        found_n = sum(1 for it in open_items_structured if it["status"] == "found")
        nr_n    = sum(1 for it in open_items_structured if it["status"] == "needs_review")
        nf_n    = sum(1 for it in open_items_structured if it["status"] == "not_found")
        print(
            f"[Phase1 open-items-only] folder={folder_name!r} "
            f"docs={total} purpose={folder_purpose} checklist={checklist_name} "
            f"found={found_n} needs_review={nr_n} not_found={nf_n}"
        )

        # Normalize to canonical schema before returning.
        structured = self._normalize_structured_summary(
            structured,
            response_kind="open_items_only",
            query=query,
            sources=sources_out,
            confidence="high",
        )
        # Persist for future reporting/analytics (best-effort).
        _persist_structured_summary(structured)

        return IntelligenceResponse(
            answer=answer,
            sources=sources_out,
            search_results=total,
            confidence="high",
            job_context=session.job_context,
            suggested_followups=[
                "Give me a summary",
                "What's the appraised value?",
                "Show me the contract",
            ],
            structured_summary=structured,
        )

    def _build_folder_summary_response(self, query: str, folder_name: str,
                                        ranked_files: List[Dict],
                                        session: "ChatSession") -> "IntelligenceResponse":
        """Structured summary of a detected folder/claim/property.

        Flow:
          1. Build dossier from top-N ranked files (doc_type + snippet per file).
          2. Ask Gemini ONCE for a JSON object (overview, key_facts,
             timeline, observations).
          3. Strip ``` fences, parse JSON. On parse failure, fall back to
             a deterministic structure built from the dossier alone.
          4. Combine parsed JSON with Python-computed document_inventory
             and open_items into the canonical structured_summary object.
          5. Render a COMPACT Markdown view for the chat answer
             (Summary + Key Facts top 5-8 + Open Items).
          6. Return IntelligenceResponse with:
               - answer            = compact Markdown
               - structured_summary = full structured object (source of truth)
               - sources           = dossier files as chips (unchanged)

        ONE Gemini call. No re-search. No re-rank. No Markdown parsing.
        The structured object is the source of truth; the chat answer is
        a view of it. Downstream consumers (reporting, analytics,
        BigQuery exports) read from `structured_summary` directly so the
        chat stays terse without losing detail.
        """
        dossier = self._build_folder_dossier(query, folder_name, ranked_files)
        files = dossier["files"]
        inventory = dossier["inventory"]
        total = dossier["file_count_total"]
        in_dossier = dossier["file_count_in_dossier"]

        # ---- Build the Gemini prompt for STRUCTURED JSON ---------------
        # We provide the dossier text plus an explicit JSON schema example,
        # and instruct Gemini to return ONLY JSON. Inventory and open items
        # are NOT requested from Gemini -- those are computed in Python
        # below from the dossier itself so they stay deterministic.
        dossier_lines: List[str] = []
        for i, f in enumerate(files, 1):
            # Marker files surface honestly in the prompt so Gemini doesn't
            # speculate. The note explicitly says the file exists but its
            # text isn't readable, which guides the model toward a brief
            # acknowledgment rather than a fabricated key_facts list.
            if f.get('is_marker'):
                text_note = (
                    "Extracted text: (marker file -- HTML or similar; "
                    "text extraction not currently supported. Treat as "
                    "evidence the folder exists but do NOT invent details.)"
                )
                snippet_render = "(no extracted text available)"
            elif f['has_text']:
                text_note = (
                    f"Extracted text (first {f['snippet_chars']} of "
                    f"{f['full_text_len']} chars):"
                )
                snippet_render = f['snippet']
            else:
                text_note = "Extracted text: (none available)"
                snippet_render = "(no extracted text available)"
            dossier_lines.append(
                f"[FILE {i}]\n"
                f"Filename: {f['name']}\n"
                f"Bucket: {f['bucket']}\n"
                f"Doc type (raw): {f['doc_type']}\n"
                f"Subfolder: {f['subfolder'] or '(top level)'}\n"
                f"{text_note}\n"
                f"{snippet_render}"
            )
        dossier_block = "\n\n".join(dossier_lines) if dossier_lines else "(no files)"

        # The schema we want back. Repeated in the prompt as a concrete
        # JSON example so Gemini Flash imitates the shape reliably.
        schema_example = (
            "{\n"
            '  "overview": "Short paragraph (2-4 sentences) describing what '
            'this folder appears to be about, grounded in what the documents '
            'actually show.",\n'
            '  "key_facts": [\n'
            "    {\n"
            '      "label": "Property address",\n'
            '      "value": "27 Manor Dr, Shirley, NY 11967",\n'
            '      "confidence": "high",\n'
            '      "sources": ["filename.pdf"]\n'
            "    }\n"
            "  ],\n"
            '  "timeline": [\n'
            "    {\n"
            '      "date": "2024-06-07",\n'
            '      "event": "Opinion of value issued",\n'
            '      "confidence": "high",\n'
            '      "sources": ["filename.pdf"]\n'
            "    }\n"
            "  ],\n"
            '  "observations": [\n'
            '    "Evidence-based issue or noteworthy discrepancy."\n'
            "  ]\n"
            "}"
        )

        # ---- Call Gemini (one call, JSON output) -----------------------
        parsed: Optional[Dict] = None
        if self._use_gemini:
            prompt = (
                f"You are extracting a STRUCTURED BUSINESS SUMMARY of a folder/claim/property "
                f"based on the documents below. The user asked: {query!r}\n\n"
                f"Folder: {folder_name}\n"
                f"Total files in folder: {total}\n"
                f"Files included in this dossier (top {in_dossier} by relevance): {in_dossier}\n\n"
                f"--- DOSSIER (extracted text + metadata per file) ---\n{dossier_block}\n\n"
                f"--- OUTPUT FORMAT ---\n"
                f"Reply with a SINGLE JSON object matching this exact schema. "
                f"No prose, no Markdown, no ```json fences. JSON ONLY.\n\n"
                f"{schema_example}\n\n"
                f"--- FIELD RULES ---\n"
                f"- overview: ONE short paragraph, 2-4 sentences. Property/customer/claim context "
                f"if extractable. Grounded in the dossier only.\n"
                f"- key_facts: concrete facts you can extract. Each fact has label, value, "
                f"confidence (one of high|medium|low), and sources (list of filenames it came from). "
                f"Prefer these fact types when present: property address, parties/people, "
                f"insurance carrier, claim number, important dates, dollar amounts (appraised "
                f"value, claim amount, invoice totals, estimates), contract terms. "
                f"OMIT facts you cannot find -- do not invent or guess. "
                f"If no facts are extractable, return an empty list [].\n"
                f"- timeline: chronological events with dates extractable from the text. "
                f"date format YYYY-MM-DD. If no dates are extractable, return an empty list [].\n"
                f"- observations: short, evidence-based notes about discrepancies, gaps, or "
                f"oddities you noticed in the dossier (e.g. property type differs between two "
                f"appraisals). NOT a generic checklist. Empty list if nothing of note.\n\n"
                f"RULES:\n"
                f"- Use ONLY information from the dossier above. No outside knowledge.\n"
                f"- Cite filenames in `sources` for every fact/event.\n"
                f"- When unsure, OMIT the entry rather than guess.\n"
                f"- Reply with JSON ONLY -- no prose, no Markdown headers, no fences.\n"
            )
            try:
                summary_model = genai.GenerativeModel(model_name=GEMINI_MODEL)
                resp = summary_model.generate_content(prompt)
                raw = (resp.text or "").strip()
                cleaned = self._strip_json_fences(raw)
                if cleaned:
                    try:
                        candidate = json.loads(cleaned)
                        if isinstance(candidate, dict):
                            parsed = candidate
                        else:
                            print(f"[Phase1 summary] JSON parsed but not a dict: {type(candidate).__name__}")
                    except json.JSONDecodeError as je:
                        print(f"[Phase1 summary] JSON parse failed: {je}; raw len={len(raw)}")
            except Exception as e:
                print(f"[Phase1 summary] Gemini error: {e}")

        # ---- Deterministic fallback if Gemini failed or returned junk --
        # We never break the chat. If we couldn't get JSON we still emit
        # a valid structured_summary object with the inventory + open
        # items so the user sees something useful.
        if parsed is None:
            parsed = {
                "overview": (
                    f"Folder {folder_name!r} contains {total} indexed document(s); "
                    f"this summary is based on the {in_dossier} most relevant. "
                    f"Automatic narrative synthesis is unavailable."
                ),
                "key_facts":   [],
                "timeline":    [],
                "observations": [],
            }

        # ---- Coerce parsed values to the shape we promise the frontend --
        # Gemini can return slightly off-schema responses (string instead of
        # list, missing keys, nested confidence inside value, etc.). Defend
        # the structure here so downstream consumers don't have to.
        def _as_list_of_dicts(v):
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            return []

        def _as_list_of_strings(v):
            if isinstance(v, list):
                return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
            return []

        overview_str = parsed.get("overview")
        if not isinstance(overview_str, str):
            overview_str = ""
        overview_str = overview_str.strip()

        key_facts = _as_list_of_dicts(parsed.get("key_facts"))
        timeline  = _as_list_of_dicts(parsed.get("timeline"))
        observations = _as_list_of_strings(parsed.get("observations"))

        # ---- Folder-purpose classification ------------------------------
        # Drives both which checklist we apply AND whether the Open Items
        # render appears in chat. The structured object always carries
        # the full open_items list (so reporting/BigQuery see everything);
        # only the chat-Markdown view is gated. See _classify_folder_purpose
        # / _should_render_open_items / _pick_checklist_for_purpose.
        folder_purpose = self._classify_folder_purpose(inventory, folder_name)
        show_open_items = self._should_render_open_items(folder_purpose, query)
        checklist, checklist_name = self._pick_checklist_for_purpose(folder_purpose)

        # ---- Compute Python-side structured data -----------------------
        # Inventory (already grouped by bucket by _build_folder_dossier)
        # and open_items (deterministic from inventory) are NOT trusted to
        # Gemini -- they are the source of truth for reporting. For
        # unknown folders we leave open_items empty rather than apply
        # the default claim checklist, so reporting consumers can tell
        # "we didn't run a checklist" apart from "checklist ran and
        # everything was missing".
        if checklist is None:
            open_items_structured = []
        else:
            open_items_structured = self._compute_open_items_structured(
                inventory, checklist=checklist, checklist_name=checklist_name
            )

        # ---- Assemble the canonical structured object ------------------
        structured: Dict = {
            "folder_name":          folder_name,
            "file_count_total":     total,
            "file_count_in_dossier": in_dossier,
            "overview":             overview_str,
            "key_facts":            key_facts,
            "timeline":             timeline,
            "observations":         observations,
            "document_inventory":   inventory,
            "open_items":           open_items_structured,
            "folder_purpose":       folder_purpose,
            "show_open_items":      show_open_items,
            "checklist_name":       checklist_name,
        }

        # ---- Render compact Markdown for the chat answer ---------------
        answer = self._render_compact_summary_markdown(structured)

        # ---- Source chips (unchanged behavior) -------------------------
        sources_out = [
            {
                "title":     f["name"],
                "uri":       f["uri"],
                "subfolder": f["subfolder"],
            }
            for f in files
        ]

        # ---- Update session ---------------------------------------------
        session.history.append(ChatMessage(role="user", text=query))
        session.history.append(ChatMessage(role="model", text=answer))
        session.last_active = time.time()
        session.last_search_query = query
        session.last_search_time = time.time()
        session.cached_sources = [
            {"title": s["title"], "uri": s["uri"], "snippet": ""}
            for s in sources_out
        ]

        print(
            f"[Phase1 summary] folder={folder_name!r} "
            f"dossier={in_dossier}/{total} files "
            f"buckets={sorted(inventory.keys())} "
            f"purpose={folder_purpose} show_open_items={show_open_items} "
            f"key_facts={len(key_facts)} timeline={len(timeline)} "
            f"observations={len(observations)}"
        )

        # Normalize to canonical schema before returning.
        structured = self._normalize_structured_summary(
            structured,
            response_kind="folder_summary",
            query=query,
            sources=sources_out,
            confidence="high",
        )
        # Persist for future reporting/analytics (best-effort).
        _persist_structured_summary(structured)

        return IntelligenceResponse(
            answer=answer,
            sources=sources_out,
            search_results=total,
            confidence="high",
            job_context=session.job_context,
            suggested_followups=[
                "What's the appraised value?",
                "Are there any open invoices?",
                "Show me the contract",
            ],
            structured_summary=structured,
        )

    def chat(self, query: str, session_id: Optional[str] = None) -> IntelligenceResponse:
        # Get or create session
        session = self.get_session(session_id) if session_id else None
        if not session:
            sid = self.new_session()
            session = self._sessions[sid]

        # Extract job context from query
        detected = _extract_job_context(query)
        if detected: 
            session.job_context = detected

        # Build search query with context
        full_query = f"{session.job_context} {query}" if session.job_context else query

        # === Phase 1: folder-aware retrieval ============================
        # If the query references a known property/person folder, route
        # into mode-aware retrieval BEFORE falling through to the legacy
        # Vertex/local-fast-path logic. Folder detection uses the
        # local_index.detect_property_in_query method that already exists.
        #
        # Design contract: Phase 1 only kicks in when a folder is
        # identified. Non-folder queries skip this entire block and run
        # the existing flow unchanged.
        phase1_handled = False
        phase1_mode = None
        phase1_folder = None
        phase1_total = 0
        phase1_read = 0
        sources: List[Dict] = []
        num_results = 0

        detected_folder = None
        if _LOCAL_INDEX_AVAILABLE:
            try:
                idx = _get_local_index()
                detected_folder = idx.detect_property_in_query(query)
                # Follow-up fallback: when the bare query doesn't name a
                # folder but a prior turn established one (session.job_context),
                # try detecting against the context-prepended query. This
                # is what makes "whats missing" or "show me the contract"
                # inherit the prior "27 Manor Drive" folder instead of
                # falling through to the unanchored Vertex/Gemini path
                # (which produces "the document excerpts are empty..."
                # nonsense answers).
                if not detected_folder and session.job_context:
                    contextual_query = f"{session.job_context} {query}"
                    detected_folder = idx.detect_property_in_query(contextual_query)
                    if detected_folder:
                        print(
                            f"[Phase1] folder inherited from session context: "
                            f"{detected_folder!r} (via job_context={session.job_context!r})"
                        )
            except Exception as fe:
                print(f"[Phase1] folder detection error: {fe}")
                detected_folder = None

        if detected_folder:
            print(f"[Phase1] folder detected: {detected_folder!r}")
            folder_files = self._enumerate_folder(detected_folder)
            print(f"[Phase1] folder enumeration: {len(folder_files)} searchable files")
            if folder_files:
                # Open-items short-circuit: BEFORE classifier, BEFORE
                # dossier build. If the user explicitly asked about
                # missing items / open items / checklist / gaps and a
                # folder is in scope, route directly to the deterministic
                # checklist response. No Gemini classifier call (which
                # might mis-route to MODE_3 enumeration), no snippet
                # extraction. Pure inventory math.
                #
                # The open-items answer only needs bucket membership, not
                # snippet content -- the checklist asks "do we have any
                # invoice-type files", not "what does the invoice say".
                # Building a dossier for this query would be wasted work.
                if _OPEN_ITEMS_INTENT_RE.search(query or ""):
                    return self._build_open_items_only_response(
                        query, detected_folder, folder_files, session
                    )

                # Classify intent. One Gemini Flash call (~$0.0001).
                mode, _target = self._classify_query_mode(query, detected_folder)

                if mode == "MODE_3":
                    # Unbounded enumeration: list, no reading, return early.
                    # MODE_3's user contract is "everything on X" -- not
                    # "everything inside the X folder". Real OneDrive
                    # layouts have multiple folders per subject (Pampinella
                    # has at least 3: Pampinella-Giacomo-Legal,
                    # Pampinella-2120-6th-has-REBUILD, plus Giacomo-Tenant-...).
                    # Detection picks ONE; we enumerate ALL related folders
                    # plus filename matches and union them.
                    #
                    # Three sources of evidence:
                    #   (a) files inside the detected folder
                    #   (b) files inside related folders (those sharing a
                    #       distinctive token with the detected folder)
                    #   (c) files whose NAME matches the detected folder's
                    #       distinctive tokens, anywhere in the bucket
                    # Unioned, deduped by URI. Folder-internal files take
                    # precedence because folder context is the stronger
                    # signal; filename matches fill in coverage gaps.
                    #
                    # MODE_1 / MODE_2 deliberately do NOT do this union or
                    # multi-folder fan-out -- broadening factual lookups
                    # would re-introduce the filename-matching false-positive
                    # that the whole folder-scoped retrieval was built to fix.
                    union_files = list(folder_files)  # source (a)
                    seen_uris = {f.get("uri", "") for f in folder_files}

                    related_folders = self._find_related_folders(detected_folder)
                    related_added = 0
                    if related_folders:
                        print(f"[Phase1] MODE_3 related folders ({len(related_folders)}): {related_folders[:10]}")
                    for other in related_folders:
                        other_files = self._enumerate_folder(other)  # source (b)
                        for f in other_files:
                            uri = f.get("uri", "")
                            if uri and uri not in seen_uris:
                                seen_uris.add(uri)
                                union_files.append(f)
                                related_added += 1

                    name_matches = self._collect_filename_matches(
                        query, folder_name=detected_folder
                    )  # source (c)
                    new_from_name = 0
                    for f in name_matches:
                        uri = f.get("uri", "")
                        if uri and uri not in seen_uris:
                            seen_uris.add(uri)
                            union_files.append(f)
                            new_from_name += 1
                    print(
                        f"[Phase1] MODE_3 union: {len(folder_files)} folder "
                        f"+ {related_added} related-folder "
                        f"+ {new_from_name} name-only "
                        f"= {len(union_files)} total"
                    )
                    return self._build_mode_3_response(
                        query, detected_folder, union_files, session
                    )

                # Mode 1 / Mode 2: rank folder files by query relevance,
                # take top-N, fetch full text, hand off to the existing
                # Gemini synthesis block below as if Vertex had returned
                # them.
                ranked = self._rank_folder_files_by_relevance(folder_files, query)

                # Folder-summary branch (within MODE_2): if the query is
                # phrased as a summary/status/overview ask, build a
                # structured-section response instead of falling through
                # to free-form synthesis. This is the read-side product
                # feature: claim/property summaries with overview, key
                # facts, document inventory, timeline, and open questions.
                #
                # We only do this when:
                #   - mode is MODE_2 (the classifier already routed this
                #     as synthesis, not factual lookup or enumeration)
                #   - the query phrasing matches a summary pattern
                # MODE_1 (factual) and MODE_2-non-summary fall through
                # to the existing top-N + Gemini synthesis path below.
                if mode == "MODE_2" and self._is_folder_summary_query(query):
                    print(
                        f"[Phase1] MODE_2 summary branch: {detected_folder!r} "
                        f"({len(ranked)} ranked files)"
                    )
                    return self._build_folder_summary_response(
                        query, detected_folder, ranked, session
                    )

                read_limit = MODE_1_READ_LIMIT if mode == "MODE_1" else MODE_2_READ_LIMIT
                to_read = ranked[:read_limit]
                print(f"[Phase1] {mode} reading top {len(to_read)} of {len(folder_files)}")

                p1_sources: List[Dict] = []
                for f in to_read:
                    if not _FETCH_AVAILABLE:
                        # No fetcher -- include title-only.
                        p1_sources.append({
                            "title":   f["name"],
                            "uri":     f["uri"],
                            "snippet": "",
                        })
                        continue
                    try:
                        fetched = _fetch_doc_by_name(f["name"])
                        if fetched.get("ok") and fetched.get("text"):
                            p1_sources.append({
                                "title":   fetched["title"],
                                "uri":     fetched.get("uri", f["uri"]),
                                "snippet": fetched["text"][:3000],
                            })
                        else:
                            # Doc matched by folder but fetcher couldn't load.
                            # Include title-only so user still sees it.
                            p1_sources.append({
                                "title":   f["name"],
                                "uri":     f["uri"],
                                "snippet": "",
                            })
                    except Exception as fe:
                        print(f"[Phase1] fetch failed for {f['name']!r}: {fe}")
                        p1_sources.append({
                            "title":   f["name"],
                            "uri":     f["uri"],
                            "snippet": "",
                        })

                sources = p1_sources
                num_results = len(folder_files)  # report folder size, not read count
                phase1_handled = True
                phase1_mode = mode
                phase1_folder = detected_folder
                phase1_total = len(folder_files)
                phase1_read = len(to_read)
                # Cache so re-asking within window is instant.
                session.last_search_query = full_query
                session.last_search_time = time.time()
                session.cached_sources = sources

        # SMART CACHING: Reuse recent search if same context
        now = time.time()
        cache_valid = (
            session.last_search_query == full_query and 
            (now - session.last_search_time) < (CONTEXT_CACHE_MINUTES * 60) and
            session.cached_sources
        )
        
        if cache_valid:
            print(f"[Cache] Reusing search results from {int(now - session.last_search_time)}s ago")
            sources = session.cached_sources
            num_results = len(sources)
        elif phase1_handled:
            # Phase 1 already populated `sources` and `num_results`.
            # Skip the Vertex/local-fast-path block below.
            pass
        else:
            # ── LOCAL-FIRST PATH ────────────────────────────────────────
            # First try the in-memory filename index. If it finds strong
            # matches, we fetch them directly from GCS and SKIP Vertex
            # entirely — zero search-quota usage.
            sources = []
            num_results = 0
            local_hits_used = False
            if _LOCAL_INDEX_AVAILABLE and _FETCH_AVAILABLE:
                try:
                    idx = _get_local_index()
                    local_hits = idx.find(query, top_n=3)
                    # Score >= 100 means full-substring match — high confidence.
                    strong = [h for h in local_hits if h["score"] >= 100]
                    if strong:
                        local_hits_used = True
                        print(f"[diag] Local index strong-match: {[h['name'] for h in strong]} — SKIPPING Vertex")
                        for h in strong[:3]:
                            try:
                                fetched = _fetch_doc_by_name(h["name"])
                                if fetched.get("ok") and fetched.get("text"):
                                    sources.append({
                                        "title":   fetched["title"],
                                        "uri":     fetched.get("uri", h["uri"]),
                                        "snippet": fetched["text"][:3000],
                                    })
                                    print(f"[diag] Fetched local hit {fetched['title']!r}: {len(fetched['text'])} chars")
                            except Exception as fe:
                                print(f"[diag] Local-hit fetch failed for {h['name']!r}: {fe}")
                        num_results = len(sources)
                        if sources:
                            session.last_search_query = full_query
                            session.last_search_time = now
                            session.cached_sources = sources
                except Exception as ie:
                    print(f"[diag] Local index lookup error: {ie}")

            # ── FALLBACK: Vertex search if local index missed ───────────────
            if not local_hits_used:
                try:
                    sources, num_results = self._vertex_search(full_query)
                    session.last_search_query = full_query
                    session.last_search_time = now
                    session.cached_sources = sources
                except gapi_exceptions.ResourceExhausted as qe:
                    print(f"[Vertex] Quota wall hit: {qe}")
                    session.history.append(ChatMessage(role="user", text=query))
                    msg = (
                        "⚠ Vertex search is currently rate-limited. "
                        "For property/document name queries, the local index "
                        "should have caught this — try rephrasing with the "
                        "specific address or filename."
                    )
                    session.history.append(ChatMessage(role="model", text=msg))
                    session.last_active = time.time()
                    return IntelligenceResponse(
                        answer=msg, sources=[], search_results=0,
                        confidence="none", job_context=session.job_context,
                        suggested_followups=["Try again", "What documents do we have?"],
                    )
                except Exception as e:
                    print(f"[Vertex] {e}")
                    sources, num_results = [], 0

                # AUTO-RESCUE: If Vertex's top results all have empty content
                # (because Vertex's relevance ranking didn't surface the actual
                # match), proactively try a name-based fetch using the user's
                # query as a hint. This catches files like '106-Madison-Avenue-.pdf'
                # where Vertex prioritizes 'madison ave contract' instead.
                try:
                    all_empty = sources and all(not s.get("snippet", "").strip() for s in sources)
                    if all_empty and _FETCH_AVAILABLE:
                        print(f"[diag] Auto-rescue: all top sources have empty snippets; trying name-based fetch with {query!r}")
                        rescue = _fetch_doc_by_name(query)
                        if rescue.get("ok") and rescue.get("text"):
                            rescue_title = rescue["title"]
                            rescue_uri = rescue.get("uri", "")
                            print(f"[diag] Auto-rescue HIT: {rescue_title!r} ({len(rescue['text'])} chars)")
                            rescued = {
                                "title": rescue_title,
                                "uri": rescue_uri,
                                "snippet": rescue["text"][:3000],
                            }
                            existing_titles = {s.get("title") for s in sources}
                            if rescue_title not in existing_titles:
                                sources = [rescued] + sources
                            else:
                                sources = [rescued] + [s for s in sources if s.get("title") != rescue_title]
                            num_results = len(sources)
                            session.cached_sources = sources
                        else:
                            print(f"[diag] Auto-rescue MISS for {query!r}: {rescue.get('error', 'no match')}")
                except Exception as ex:
                    print(f"[diag] Auto-rescue error: {ex}")

        # Build answer
        if not sources:
            hint = f" (focused on: {session.job_context})" if session.job_context else ""
            answer = (f"No documents found{hint}. Try a specific address, "
                      f"permit number, loan number, dollar amount, or document name.")
        elif self._use_gemini:
            # GEMINI SYNTHESIS: Using retrieved context
            # Fold conversation history into the prompt as plain text rather
            # than using start_chat(history=...). The chat-history API is
            # strict about message ordering and frequently 400s; inlining is
            # bulletproof and works with every Gemini model version.
            history_lines = []
            for m in session.history[-(MAX_HISTORY*2):]:
                speaker = "User" if m.role == "user" else "Assistant"
                history_lines.append(f"{speaker}: {m.text}")
            history_block = "\n".join(history_lines) if history_lines else "(no prior conversation)"

            context_text = "\n\n".join([
                f"**{s['title']}**\n{s['snippet']}"
                for s in sources[:8]
            ])

            hint = f"\n[Job in focus: {session.job_context}]" if session.job_context else ""
            src_list = ", ".join(s["title"] for s in sources[:8])

            prompt = (
                f"Conversation so far:\n{history_block}\n\n"
                f"Documents found: {src_list}{hint}\n\n"
                f"Document excerpts:\n{context_text}\n\n"
                f"User's question: {query}\n\n"
                f"Answer based ONLY on the document excerpts above. "
                f"Cite specific documents. If the excerpts don't answer the question, say so."
            )

            try:
                if self._tools:
                    answer = self._run_tool_loop(prompt)
                else:
                    resp = self._gemini.generate_content(prompt)
                    answer = (resp.text or "").strip()
                if not answer:
                    answer = (
                        f"Gemini returned an empty response. "
                        f"Found {num_results} relevant document(s): {src_list}."
                    )
            except Exception as e:
                # Surface the real error so we can see it in the chat,
                # not just buried in the server log.
                err_str = str(e)
                print(f"[Gemini] {err_str}")
                answer = (
                    f"Found {num_results} relevant document(s): {src_list}. "
                    f"Gemini error during synthesis: {err_str[:300]}"
                )
        else:
            # NO GEMINI: Just list what was found
            src_list = ", ".join(s["title"] for s in sources[:5])
            answer = f"Found {num_results} documents: {src_list}. Use Gemini for synthesis."

        # Update session
        session.history.append(ChatMessage(role="user", text=query))
        session.history.append(ChatMessage(role="model", text=answer))
        session.last_active = time.time()

        # Build response
        return IntelligenceResponse(
            answer=answer,
            sources=[{"title": s["title"], "uri": s["uri"]} for s in sources[:8]],
            search_results=num_results,
            confidence=_score(num_results),
            job_context=session.job_context,
            suggested_followups=_followups(query, session.job_context))

    def clear_session(self, sid: str):
        s = self.get_session(sid)
        if s: 
            s.history.clear()
            s.job_context = None
            s.cached_sources.clear()

_intel = None
def get_intelligence():
    global _intel
    if _intel is None: 
        _intel = JobIntelligence()
    return _intel