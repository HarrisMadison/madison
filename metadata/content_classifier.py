"""Content-based document classifier.

Pure functions, zero side effects, zero I/O. Given the extracted text of a
document (and optionally its filename as a weak hint), return what kind of
document it is.

Design contract:
    - Content is the PRIMARY signal. Filename is a weak hint, used only when
      content is empty or ambiguous.
    - Classification rules are layered:
          STRONG fingerprint  -> any single match classifies the doc.
          WEAK fingerprint    -> 2+ distinct matches required.
    - Returns a structured dict so the caller can record both the doc_type
      and how confident the verdict is. Calling code should write the result
      to BOTH `doc_type` and `document_type` so future queries don't need to
      re-classify.
    - This module is intentionally narrow. It does NOT touch GCS, Vertex,
      Gemini, or any other I/O. It does NOT load files. It accepts already-
      extracted plain text. That keeps it trivially testable and reusable
      from the ingestion pipeline (write side) and from query-time ranking
      (read-side fallback).

First class supported: appraisal. The structure (rules table + classifier
function) is reusable; new doc types are added by appending a new
``_RULES_<TYPE>`` block and registering it in ``_DOC_TYPE_RULES``.

Public surface:
    classify_text(text: str, filename: str = "") -> dict
        Returns:
            {
              "doc_type":   str | None,    # canonical type or None
              "confidence": str | None,    # "strong" | "weak" | None
              "signals":    list[str],     # human-readable matched signals
              "source":     str | None,    # "content" | "filename" | None
            }

    is_appraisal(text: str) -> tuple[bool, list[str]]
        Convenience wrapper for the test case. Returns (matched, signals).
"""
from __future__ import annotations

import re
from typing import Optional


# ─── APPRAISAL FINGERPRINTS ────────────────────────────────────────────────
# Phrases observed in real appraisal documents (FNMA 1004, FNMA 1007, URAR,
# BPO reports, narrative appraisals). All matching is case-insensitive.
#
# Strong fingerprints: these phrases are characteristic enough that a single
# occurrence is sufficient to classify the document as appraisal-class. We
# require word-boundary matching (or near equivalents) so casual mentions
# in a contract or letter don't cause false positives.
#
# Weak fingerprints: appear in appraisals but also in other docs that
# REFERENCE valuations (e.g. a contract with an appraisal contingency clause
# may say "appraised value"). Two or more of these in the same document is
# evidence the doc is an appraisal itself, not just a reference to one.
#
# The lists are deliberately small and high-signal. Adding noisy phrases
# here will produce false positives faster than it gains coverage.

_APPRAISAL_STRONG: list[str] = [
    # Form titles -- only an actual form has the form's own title
    r"uniform\s+residential\s+appraisal\s+report",
    r"appraisal\s+report\s*[\u2013\u2014\-:]\s*subject\s+property",
    r"single\s*-?\s*family\s+comparable\s+rent\s+schedule",
    r"broker\s+price\s+opinion",
    r"\bbpo\s+report\b",
    # FNMA form 1004 / 1007 explicit form-number references at the top of
    # an appraisal page header. Word boundaries prevent matches against
    # random digit runs.
    r"freddie\s+mac\s+form\s+70",
    r"fannie\s+mae\s+form\s+1004\b",
    r"fannie\s+mae\s+form\s+1007\b",
    # The "Opinion of Value" heading on an appraisal -- usually appears as
    # its own line. Requires colon or bracketing whitespace to avoid matching
    # casual prose like "in my opinion of the value of this approach".
    r"opinion\s+of\s+(?:market\s+)?value\s*[:\-\u2013\u2014]",
    r"final\s+(?:opinion|estimate)\s+of\s+(?:market\s+)?value",
    # URAR / appraiser certification language unique to appraisals
    r"appraiser\s*'?s?\s+certification",
    r"scope\s+of\s+work\s+for\s+(?:this\s+)?appraisal",
]

_APPRAISAL_WEAK: list[str] = [
    # Valuation language -- can appear in contracts and letters too
    r"\bopinion\s+of\s+value\b",
    r"\bappraised\s+value\b",
    r"\bmarket\s+value\b",
    r"\bas[\s\-]is\s+value\b",
    r"\bsubject\s+property\b",
    # Comp / sales-comparison language
    r"\bcomparable\s+sales?\b",
    r"\bsales?\s+comparison(?:\s+approach)?\b",
    r"\bcomparables?\s+(?:1|2|3|one|two|three)\b",
    # Final-reconciliation pattern (appraisers write a "reconciliation"
    # section that synthesizes the three approaches)
    r"\bfinal\s+reconciliation\b",
    r"\breconciliation\s+of\s+value\b",
    # Approaches to value -- appraisal-only structural language
    r"\bcost\s+approach\b",
    r"\bincome\s+approach\b",
    r"\bsales\s+comparison\s+approach\b",
    # Effective date is universal in appraisals
    r"\beffective\s+date\s+of\s+(?:appraisal|value)\b",
]


# Compile once at module load. If a pattern is malformed we surface it
# loudly here rather than failing silently at first use.
def _compile(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            # Skip the bad pattern rather than crash the whole module.
            # Production caller will see one missing fingerprint, not a
            # blown classifier.
            print(f"[content_classifier] bad regex {p!r}: {e}")
    return out


_APPRAISAL_STRONG_RE = _compile(_APPRAISAL_STRONG)
_APPRAISAL_WEAK_RE   = _compile(_APPRAISAL_WEAK)


# ─── PERMIT FINGERPRINTS ────────────────────────────────────────────
# Construction/building permits issued by municipalities. Strong signals
# are government-issued language that appears almost exclusively on actual
# permit documents. Weak signals are construction-context terms that
# appear on permits AND in related contracts/estimates.
_PERMIT_STRONG: list[str] = [
    r"\bbuilding\s+permit\b",
    r"\bdemolition\s+permit\b",
    r"\belectrical\s+permit\b",
    r"\bplumbing\s+permit\b",
    r"\bmechanical\s+permit\b",
    r"\bpermit\s+(?:number|no\.?|#)\s*[:\-]?\s*[A-Z0-9\-]+",
    r"\bcertificate\s+of\s+occupancy\b",
    r"\bnotice\s+of\s+violation\b",
    r"\bstop\s+work\s+order\b",
    r"\bdepartment\s+of\s+buildings\b",
    r"\bbureau\s+of\s+buildings\b",
    r"\bissuing\s+(?:agency|authority|department)\b",
    # NY-specific: DOB, BSA references commonly appear on local permits
    r"\bdob\s+(?:permit|application|filing)\b",
]
_PERMIT_WEAK: list[str] = [
    r"\bpermit\s+(?:fee|expires?|expiration|issued|holder)\b",
    r"\bissue\s+date\b",
    r"\bexpiration\s+date\b",
    r"\bcode\s+enforcement\b",
    r"\binspector\s+(?:name|signature|approved)\b",
    r"\boccupancy\s+(?:type|group|load|classification)\b",
    r"\bzoning\s+(?:district|classification|use)\b",
    r"\bblock\s+(?:and|&)\s+lot\b",
    r"\bapplicant\s+(?:name|signature|address)\b",
    r"\bcontractor\s+license\s+(?:number|no\.?|#)\b",
]


# ─── INSURANCE / CLAIM FINGERPRINTS ────────────────────────────────
# Catches both insurance POLICIES (carrier documents) and CLAIM documents
# (correspondence, adjuster reports, settlement statements). Strong
# signals are insurance-specific structural language.
_INSURANCE_STRONG: list[str] = [
    r"\bpolicy\s+(?:number|no\.?|#)\s*[:\-]?\s*[A-Z0-9\-]+",
    r"\bclaim\s+(?:number|no\.?|#)\s*[:\-]?\s*[A-Z0-9\-]+",
    r"\bdeclarations?\s+page\b",
    r"\bdwelling\s+coverage\s*(?:[A-Z]|limit|amount)?\b",
    r"\bpersonal\s+property\s+coverage\b",
    r"\bloss\s+of\s+use\s+coverage\b",
    r"\b(?:named\s+)?insured\s*[:\-]",
    r"\bproof\s+of\s+loss\b",
    r"\bsworn\s+statement\s+in\s+proof\s+of\s+loss\b",
    r"\bdate\s+of\s+loss\b",
    r"\bcause\s+of\s+loss\b",
    r"\bperil(?:s)?\s+insured\s+against\b",
    r"\bACV\b|\bactual\s+cash\s+value\b",
    r"\breplacement\s+cost\s+value\b|\bRCV\b",
    r"\bexamination\s+under\s+oath\b",
    # Adjuster language
    r"\bfield\s+adjuster\b|\bdesk\s+adjuster\b|\bpublic\s+adjuster\b",
]
_INSURANCE_WEAK: list[str] = [
    r"\bdeductible\b",
    r"\bcoverage\s+(?:limit|amount|period)\b",
    r"\bcarrier\b",
    r"\bunderwriter\b",
    r"\bendorsement\b",
    r"\bsubrogation\b",
    r"\binsurer\b",
    r"\bpremium\s+(?:paid|due|amount)\b",
    r"\bloss\s+(?:adjuster|amount|report)\b",
    r"\bclaim\s+(?:status|representative|adjuster|amount)\b",
]


# ─── INSPECTION / ENVIRONMENTAL REPORT FINGERPRINTS ──────────────────────
# Professional inspection or testing reports: home inspection, soot/smoke
# damage testing, asbestos/lead, mold remediation, environmental hygiene.
_INSPECTION_STRONG: list[str] = [
    r"\binspection\s+report\b",
    r"\binspector\s*'?s?\s+(?:report|findings|observations)\b",
    r"\bscope\s+of\s+(?:inspection|investigation)\b",
    r"\b(?:home|property|building)\s+inspection\s+report\b",
    r"\benvironmental\s+(?:assessment|investigation|hygiene|consultant)\s+report\b",
    r"\bindustrial\s+hygiene\s+(?:report|investigation|assessment)\b",
    # Specific damage/hazard testing
    r"\bsoot\s+(?:and\s+char\s+)?(?:analysis|sampling|particulate|residue)\b",
    r"\bsmoke\s+(?:damage|residue)\s+assessment\b",
    r"\basbestos\s+(?:survey|inspection|testing|abatement)\b",
    r"\blead-?based\s+paint\s+(?:inspection|assessment|risk)\b",
    r"\bmold\s+(?:assessment|remediation|investigation)\s+report\b",
    r"\bair\s+(?:quality|sampling)\s+(?:test|report|results)\b",
    r"\bmoisture\s+(?:mapping|reading|survey)\b",
    r"\bdrying\s+log\b|\bmoisture\s+log\b",
    # Health & safety inspection -- common in Madison's corpus
    r"\bhealth\s*(?:and|&)\s*safety\s+(?:initial\s+)?inspection\b",
]
_INSPECTION_WEAK: list[str] = [
    r"\bobservations?\b",
    r"\brecommendation(?:s)?\b",
    r"\bfindings\b",
    r"\bdate\s+of\s+inspection\b",
    r"\binspector\s+(?:name|signature|license)\b",
    r"\bsampling\s+(?:locations?|results?|method)\b",
    r"\blaboratory\s+(?:results?|analysis|report)\b",
    r"\bchain\s+of\s+custody\b",
    r"\baffected\s+(?:area|room|surface)s?\b",
    r"\bvisual\s+inspection\b",
]


# ─── ESTIMATE / SCOPE OF WORK FINGERPRINTS ─────────────────────────────
# Construction/restoration estimates. Often produced by Xactimate, Symbility,
# or similar tools, which leave recognizable structural fingerprints.
_ESTIMATE_STRONG: list[str] = [
    r"\bscope\s+of\s+work\b",
    r"\bestimate\s+summary\b",
    r"\bline\s+item(?:\s+detail)?\b",
    # Xactimate-specific
    r"\bxactimate\b",
    r"\bXactware\b",
    # Symbility-specific
    r"\bSymbility\s+(?:Claims?|Pro)?\b",
    # Generic estimate document structure
    r"\bestimate\s+(?:number|no\.?|#)\s*[:\-]?\s*[A-Z0-9\-]+",
    r"\btotal\s+(?:job|project)?\s*estimate\b",
    r"\bRCV\s+(?:total|subtotal|amount)\b",
    r"\boverhead\s+(?:and|&)\s+profit\b",
    r"\bunit\s+cost\s+breakdown\b",
    # Construction-job-cost language
    r"\bmaterial\s+and\s+labor\s+(?:cost|estimate|breakdown)\b",
]
_ESTIMATE_WEAK: list[str] = [
    r"\bO\s*&\s*P\b",  # Overhead & Profit shorthand
    r"\bdepreciation\b",
    r"\bquantity\b",
    r"\bunit\s+(?:price|cost|rate)\b",
    r"\bdescription\s+of\s+(?:work|repair|services)\b",
    r"\btotal\s+(?:cost|amount|estimate)\b",
    r"\bsubtotal\b",
    r"\bgrand\s+total\b",
    r"\bestimate\b",  # generic enough that it's only weak
    r"\bremoval\s+and\s+(?:disposal|replacement)\b",
]


# ─── INVOICE FINGERPRINTS ──────────────────────────────────────────
# Billing documents from vendors, subcontractors, or service providers.
# Strong signals are invoice-specific structural language.
_INVOICE_STRONG: list[str] = [
    r"\binvoice\s+(?:number|no\.?|#)\s*[:\-]?\s*[A-Z0-9\-]+",
    r"\binvoice\s+date\b",
    r"\bbill\s+to\b",
    r"\bremit\s+(?:payment\s+)?to\b",
    r"\bamount\s+due\b",
    r"\bbalance\s+due\b",
    r"\btotal\s+due\b",
    r"\bpayment\s+terms\b",
    r"\bnet\s+(?:15|30|45|60|90)\b",  # "net 30", etc.
    r"\bdue\s+upon\s+receipt\b",
    r"\bpurchase\s+order\s+(?:number|no\.?|#)\b",
]
_INVOICE_WEAK: list[str] = [
    r"\binvoice\b",
    r"\bship\s+to\b",
    r"\bship\s+date\b",
    r"\btax\s+id\b",
    r"\bfederal\s+id\s+(?:number|no\.?|#)\b",
    r"\bquantity\s+ordered\b",
    r"\bunit\s+price\b",
    r"\bextended\s+(?:amount|price)\b",
    r"\bsales\s+tax\b",
    r"\bpaid\s+(?:in\s+full|by|via)\b",
]


# ─── CONTRACT FINGERPRINTS ─────────────────────────────────────────
# Signed agreements. Distinctive language: parties, recitals, witnesseth,
# consideration, signature blocks. Many other documents REFER to contracts
# ("per the contract terms"), so weak signals shouldn't fire on incidental
# mentions.
_CONTRACT_STRONG: list[str] = [
    r"\bthis\s+(?:agreement|contract)\s+(?:is\s+)?(?:made|entered)\s+(?:and\s+entered\s+)?(?:into\s+)?(?:as\s+of|on|by)\b",
    r"\bin\s+witness\s+whereof\b",
    r"\bwitnesseth\b",
    r"\bnow,?\s+therefore,?\s+in\s+consideration\s+of\b",
    r"\bparties\s+hereto\s+(?:agree|covenant)\b",
    r"\bthe\s+parties\s+(?:hereby\s+)?agree\s+as\s+follows\b",
    r"\bsignatures?\s+of\s+the\s+parties\b",
    r"\bsigned\s+(?:and\s+)?(?:sealed|delivered|dated)\b",
    r"\beffective\s+date\s+of\s+this\s+(?:agreement|contract)\b",
    # Construction-contract-specific
    r"\bcontract\s+(?:price|sum|amount)\s*[:\-]",
    r"\bdirection\s+of\s+payment\b",
    r"\bassignment\s+of\s+(?:benefits|proceeds|insurance\s+benefits)\b",
    # Real-estate-specific
    r"\bdeed\s+of\s+(?:trust|record|sale)\b",
    r"\bquitclaim\s+deed\b",
    r"\bbargain\s+and\s+sale\s+deed\b",
]
_CONTRACT_WEAK: list[str] = [
    r"\bcontract\s+(?:terms|provisions)\b",
    r"\bgoverning\s+law\b",
    r"\bentire\s+agreement\b",
    r"\bseverability\b",
    r"\bindemnif(?:y|ication)\b",
    r"\bbreach\s+of\s+(?:this\s+)?(?:agreement|contract)\b",
    r"\btermination\s+(?:clause|provision|of\s+this)\b",
    r"\bbinding\s+(?:on|upon)\s+(?:the\s+)?parties\b",
    r"\bsuccessors?\s+and\s+assigns?\b",
    r"\bnotice\s+to\s+the\s+(?:other\s+)?party\b",
]


# ─── CORRESPONDENCE / DEMAND LETTER FINGERPRINTS ────────────────────────
# Narrowly scoped to LEGAL/DEMAND correspondence -- the kind that matters
# in a claim or legal matter. Friendly letters and emails don't classify
# here (too broad), but a demand letter, cease-and-desist, or formal
# legal demand should.
_CORRESPONDENCE_STRONG: list[str] = [
    r"\bdemand\s+(?:letter|for\s+payment|for\s+performance)\b",
    r"\bfinal\s+demand\b",
    r"\bcease\s+and\s+desist\b",
    r"\bnotice\s+of\s+(?:default|cancellation|intent\s+to\s+sue|claim)\b",
    r"\bformal\s+(?:demand|notice|complaint)\b",
    r"\bwithout\s+prejudice\b",
    # Common demand-letter closers
    r"\bif\s+(?:we\s+do\s+not\s+)?(?:hear|receive).{0,40}(?:legal\s+action|further\s+action|proceedings)\b",
    r"\b(?:legal|further)\s+action\s+will\s+be\s+(?:taken|pursued|commenced)\b",
    r"\bre(?:garding)?\s*:\s*demand\s+for\b",
]
_CORRESPONDENCE_WEAK: list[str] = [
    r"\bplease\s+(?:be\s+advised|consider|note)\b",
    r"\bgoverned\s+by\s+(?:applicable\s+)?law\b",
    r"\bcounsel\s+for\b",
    r"\bclient\s+(?:asserts|maintains|denies)\b",
    r"\bin\s+response\s+to\s+your\s+(?:letter|correspondence)\b",
    r"\bfailure\s+to\s+(?:respond|comply|cure|pay)\b",
    r"\bwithin\s+(?:7|10|14|15|30)\s+(?:business\s+)?days\b",
    r"\bsincerely\s*,?\s*\n",
    r"\bvery\s+truly\s+yours\b",
]


# Compile all the new pattern lists. Done in one block so a regex error in
# any new pattern surfaces during module load, not at first use.
_PERMIT_STRONG_RE         = _compile(_PERMIT_STRONG)
_PERMIT_WEAK_RE           = _compile(_PERMIT_WEAK)
_INSURANCE_STRONG_RE      = _compile(_INSURANCE_STRONG)
_INSURANCE_WEAK_RE        = _compile(_INSURANCE_WEAK)
_INSPECTION_STRONG_RE     = _compile(_INSPECTION_STRONG)
_INSPECTION_WEAK_RE       = _compile(_INSPECTION_WEAK)
_ESTIMATE_STRONG_RE       = _compile(_ESTIMATE_STRONG)
_ESTIMATE_WEAK_RE         = _compile(_ESTIMATE_WEAK)
_INVOICE_STRONG_RE        = _compile(_INVOICE_STRONG)
_INVOICE_WEAK_RE          = _compile(_INVOICE_WEAK)
_CONTRACT_STRONG_RE       = _compile(_CONTRACT_STRONG)
_CONTRACT_WEAK_RE         = _compile(_CONTRACT_WEAK)
_CORRESPONDENCE_STRONG_RE = _compile(_CORRESPONDENCE_STRONG)
_CORRESPONDENCE_WEAK_RE   = _compile(_CORRESPONDENCE_WEAK)

# Configurable thresholds. Surfaced as module constants so a future caller
# can tune them without forking the module.
WEAK_FINGERPRINT_THRESHOLD = 2  # strong-OR-2-weak rule per design
MIN_TEXT_CHARS_FOR_CONTENT_CLASSIFY = 200  # below this, fall back to filename


# ─── public API ────────────────────────────────────────────────────────────

def _classify_against_rules(text: str,
                             strong_rules: list[re.Pattern],
                             weak_rules: list[re.Pattern]) -> tuple[bool, list[str]]:
    """Shared strong-OR-2-weak evaluation against pre-compiled rule lists.

    Returns (matched, signals). Used by every per-type ``is_<type>``
    function so the threshold logic lives in exactly one place. Distinct
    weak counting is by REGEX, not by occurrence -- a doc that says
    "market value" five times still counts as 1 weak signal.
    """
    if not text:
        return False, []

    # Strong hit short-circuits.
    for rx in strong_rules:
        if rx.search(text):
            return True, [f"strong:{rx.pattern}"]

    # Otherwise collect distinct weak hits and check threshold.
    weak_hits: list[str] = []
    for rx in weak_rules:
        if rx.search(text):
            weak_hits.append(f"weak:{rx.pattern}")

    if len(weak_hits) >= WEAK_FINGERPRINT_THRESHOLD:
        return True, weak_hits
    return False, weak_hits


def is_appraisal(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the appraisal classifier."""
    return _classify_against_rules(text, _APPRAISAL_STRONG_RE, _APPRAISAL_WEAK_RE)


def is_permit(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the permit / certificate-of-occupancy classifier."""
    return _classify_against_rules(text, _PERMIT_STRONG_RE, _PERMIT_WEAK_RE)


def is_insurance(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the insurance / claim-document classifier."""
    return _classify_against_rules(text, _INSURANCE_STRONG_RE, _INSURANCE_WEAK_RE)


def is_inspection_report(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the inspection / environmental-report classifier."""
    return _classify_against_rules(text, _INSPECTION_STRONG_RE, _INSPECTION_WEAK_RE)


def is_estimate(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the estimate / scope-of-work classifier."""
    return _classify_against_rules(text, _ESTIMATE_STRONG_RE, _ESTIMATE_WEAK_RE)


def is_invoice(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the invoice / billing-document classifier."""
    return _classify_against_rules(text, _INVOICE_STRONG_RE, _INVOICE_WEAK_RE)


def is_contract(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the contract / signed-agreement classifier."""
    return _classify_against_rules(text, _CONTRACT_STRONG_RE, _CONTRACT_WEAK_RE)


def is_correspondence(text: str) -> tuple[bool, list[str]]:
    """Return (matched, [signals]) for the legal/demand-correspondence classifier.

    Narrowly scoped to LEGAL/DEMAND letters -- this is NOT a general
    "this is a letter" classifier. Friendly emails and routine letters do
    not classify here. Adjust the strong patterns if your corpus has a
    different mix of correspondence types.
    """
    return _classify_against_rules(text, _CORRESPONDENCE_STRONG_RE, _CORRESPONDENCE_WEAK_RE)


# Registry pattern: as new doc types are added, register their classifier
# function here. Each must return (matched, signals). The first registered
# type to match wins -- order them MOST-SPECIFIC to LEAST-SPECIFIC because
# real-world documents often contain language overlapping multiple types
# (e.g. an insurance claim packet contains estimate-like line items, but
# it's more accurately an "insurance" doc than an "estimate").
#
# Rationale for the current order:
#   appraisal     - very narrow strong signals (form titles, certifications)
#   permit        - government-issued language, unambiguous when it hits
#   insurance     - policy/claim language, often paired with claim numbers
#   inspection    - inspector certifications, sampling/lab language
#   estimate      - Xactimate/scope-of-work fingerprints, line items
#   invoice       - invoice number + bill-to + due-amount triad
#   contract      - witnesseth/now-therefore/parties-hereto, signed format
#   correspondence- narrowest demand-letter language so it doesn't gobble
#                   contracts/insurance that happen to contain letter prose
_DOC_TYPE_RULES: list[tuple[str, callable]] = [
    ("appraisal",            is_appraisal),
    ("permit",               is_permit),
    ("insurance_policy",     is_insurance),
    ("inspection_report",    is_inspection_report),
    ("estimate",             is_estimate),
    ("invoice",              is_invoice),
    ("contract",             is_contract),
    ("correspondence",       is_correspondence),
]


def classify_text(text: str, filename: str = "") -> dict:
    """Classify a document by its extracted text.

    Returns:
        {
          "doc_type":   str | None,    # e.g. "appraisal" or None
          "confidence": str | None,    # "strong" | "weak" | None
          "signals":    list[str],     # which fingerprints matched
          "source":     str | None,    # "content" | None
        }

    The caller is expected to combine this with filename hints separately.
    This function does NOT consult `filename` -- that's the caller's job
    (Layer 3 in the layered ranking model). Filename is accepted for future
    extensibility and currently ignored.

    Rationale: keeping content classification pure makes the test surface
    simple. The ingestion pipeline already runs the filename classifier
    in phase6_ocr_metadata; combining here would duplicate that work.
    """
    # Guard against empty / extremely short text (e.g. a passthrough doc
    # where extraction returned just "[Untitled]"). Below the threshold,
    # we say "unknown" and let the caller fall back to filename hints.
    if not text or len(text) < MIN_TEXT_CHARS_FOR_CONTENT_CLASSIFY:
        return {
            "doc_type":   None,
            "confidence": None,
            "signals":    [],
            "source":     None,
        }

    for doc_type, classifier_fn in _DOC_TYPE_RULES:
        matched, signals = classifier_fn(text)
        if matched:
            confidence = "strong" if any(s.startswith("strong:") for s in signals) else "weak"
            return {
                "doc_type":   doc_type,
                "confidence": confidence,
                "signals":    signals,
                "source":     "content",
            }

    return {
        "doc_type":   None,
        "confidence": None,
        "signals":    [],
        "source":     None,
    }
