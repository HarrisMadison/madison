"""Read-only audit of files currently lacking a useful doc_type classification.

Reads:
  - LocalFileIndex (the in-memory file list, ~11.8k entries)
  - manifests/doc_type_index.json (the sidecar, ~9.8k entries)

Reports for every file whose doc_type is either missing entirely or
tagged 'other' / 'document' / 'unknown':
  - the filename and folder path
  - the current tag (if any)
  - the file extension
  - a heuristic guess at what the doc_type and bucket would be if the
    classifier looked at filename keywords (e.g. 'allstate_policy.pdf'
    looks like insurance)
  - rolled-up counts of what kinds of documents are being missed

Pure local. No GCS fetches, no Gemini calls, no API cost. Read-only --
does not write back to the sidecar or modify any state.

Usage:
    python scripts/audit_other_doc_types.py
    python scripts/audit_other_doc_types.py --limit 200
    python scripts/audit_other_doc_types.py --folder-filter "claim"
    python scripts/audit_other_doc_types.py --type-filter other,document
    python scripts/audit_other_doc_types.py --csv audit.csv

Goal:
    Tell us whether the classifier is missing obvious document types
    that already exist in the corpus (high leverage: tighten classifier
    rules and many "unknown" folders should flip to claim_restoration
    or property_appraisal) or whether the corpus genuinely lacks those
    document types (lower leverage: classifier tuning won't help; the
    indexed files really are mostly admin/spreadsheet/aggregate).
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# What we consider "unclassified" for audit purposes. Empty string = no
# sidecar entry at all; the rest are explicit catch-all tags that pass
# through the classifier without surfacing useful structure.
UNCLASSIFIED_TAGS = frozenset({"", "other", "document", "unknown"})

# Heuristic patterns. Each entry: (guess_bucket, guess_doc_type, regex).
# Order matters -- the first match wins so more-specific patterns sit
# above more-general ones. These are AUDIT heuristics for the script's
# recommendation only; they are NOT proposed as classifier rules yet.
# The whole point is for the user to see the matches and decide.
#
# Patterns are matched against the lowercased "name + path" string so
# folder context contributes -- e.g. an estimate sitting in an
# "Estimates" subfolder gets credit even if the filename is generic.
HEURISTICS: List[Tuple[str, str, re.Pattern]] = [
    # ── Insurance ────────────────────────────────────────────────
    ("insurance", "insurance_policy",
     re.compile(r"\b(policy|declaration[\s_-]*page|dec[\s_-]*page|coverage)\b")),
    ("insurance", "claim_documents",
     re.compile(r"\b(claim|adjust(er|ment)|acv|rcv|loss[\s_-]*run|sworn[\s_-]*statement)\b")),
    ("insurance", "insurance_policy",
     re.compile(r"\b(allstate|state[\s_-]*farm|geico|liberty[\s_-]*mutual|"
                r"farmers|nationwide|usaa|travelers|chubb|progressive|aaa|"
                r"foremost|hippo|lemonade|amica|hartford)\b")),
    # ── Estimate / scope of work ─────────────────────────────────
    ("estimate", "estimate",
     re.compile(r"\b(estimate|scope[\s_-]*of[\s_-]*work|sow|xactimate|symbility|"
                r"repair[\s_-]*quote)\b")),
    # ── Invoice / receipts ───────────────────────────────────────
    ("invoice", "invoice",
     re.compile(r"\b(invoice|inv[\s_-]?\d{2,}|bill|receipt|payment[\s_-]*statement|"
                r"draw[\s_-]*request)\b")),
    # ── Contract / agreement ─────────────────────────────────────
    ("contract", "contract",
     re.compile(r"\b(contract|agreement|aob|authorization|work[\s_-]*auth|"
                r"signed[\s_-]*(contract|agreement))\b")),
    # ── Appraisal / valuation ────────────────────────────────────
    ("appraisal", "appraisal",
     re.compile(r"\b(appraisal|fnma|opinion[\s_-]*of[\s_-]*value|"
                r"valuation|bpo|cma|appraised[\s_-]*value|1004)\b")),
    # ── Reports (inspections, IICRC, environmental) ──────────────
    ("report", "inspection_report",
     re.compile(r"\b(inspection|iicrc|certificate[\s_-]*of[\s_-]*completion|coc|"
                r"final[\s_-]*draft|moisture[\s_-]*log|drying[\s_-]*log)\b")),
    ("report", "environmental_report",
     re.compile(r"\b(environmental|mold|asbestos|lead|soot|"
                r"indoor[\s_-]*air[\s_-]*quality|iaq)\b")),
    # ── Closing / financial (route to appraisal bucket) ──────────
    ("appraisal", "closing_statement",
     re.compile(r"\b(closing[\s_-]*statement|hud[\s_-]*1|settlement[\s_-]*statement|"
                r"net[\s_-]*proceeds)\b")),
    ("appraisal", "closing_package",
     re.compile(r"\bclosing[\s_-]*(package|docs?)\b")),
    # ── Deed / title (route to contract bucket) ──────────────────
    ("contract", "deed",
     re.compile(r"\b(deed|title[\s_-]*(report|search|insurance))\b")),
    ("contract", "loan_document",
     re.compile(r"\b(loan|mortgage|note|promissory)\b")),
    # ── Permits / compliance ─────────────────────────────────────
    ("permit", "permit",
     re.compile(r"\b(permit|certificate[\s_-]*of[\s_-]*occupancy|c\.?o\.?|"
                r"violation|dob)\b")),
    # ── Correspondence ───────────────────────────────────────────
    ("correspondence", "correspondence",
     re.compile(r"\b(letter|email|memo|reply|response|notice)\b")),
    # ── Photos (extension fallback usually catches these; this is
    #    for filenames mentioning photos in a non-image file) ─────
    ("photos", "photos",
     re.compile(r"\b(photo[\s_-]*report|photos|photographs|gallery)\b")),
]

# Extension fallbacks. If filename keywords didn't match anything,
# extension can still tell us something useful (e.g. .xlsx is almost
# always a spreadsheet). Listed by precedence.
EXTENSION_FALLBACKS = [
    (re.compile(r"\.(xlsx|xls|csv|tsv)$"), "spreadsheet", "spreadsheet"),
    (re.compile(r"\.(jpg|jpeg|png|heic|heif|tiff|tif|webp|gif)$"), "photos", "photos"),
    (re.compile(r"\.(eml|msg)$"), "correspondence", "email"),
    (re.compile(r"\.(pptx|ppt)$"), "presentation", "presentation"),
]

# When normalizing filenames for "top patterns" grouping, replace
# variable bits (long digit runs, dates, GUIDs) so similar files
# collapse to a single pattern.
_NORMALIZE_RES = [
    (re.compile(r"\d{4}[-_]\d{2}[-_]\d{2}"),       "<DATE>"),
    (re.compile(r"\d{2}[-_/]\d{2}[-_/]\d{2,4}"),   "<DATE>"),
    (re.compile(r"\b20\d{2}\b"),                    "<YEAR>"),
    (re.compile(r"\b\d{6,}\b"),                     "<NUM>"),
    (re.compile(r"\b[a-f0-9]{8,}\b", re.IGNORECASE), "<HEX>"),
    (re.compile(r"\s*\(\d+\)"),                     ""),         # "filename (1).pdf"
]


def _normalize_for_pattern(name: str) -> str:
    """Collapse variable parts of a filename for pattern grouping."""
    n = name.lower()
    for rx, repl in _NORMALIZE_RES:
        n = rx.sub(repl, n)
    return n


def _guess(name: str, path: str) -> Tuple[Optional[str], Optional[str], str]:
    """Return (guess_bucket, guess_doc_type, basis).

    basis is one of "filename_match", "extension", or "no_signal" so the
    caller can show the user which kind of evidence triggered the guess.
    """
    haystack = (name + " " + path).lower()
    for bucket, doc_type, rx in HEURISTICS:
        if rx.search(haystack):
            return bucket, doc_type, "filename_match"
    for rx, bucket, doc_type in EXTENSION_FALLBACKS:
        if rx.search(name.lower()):
            return bucket, doc_type, "extension"
    return None, None, "no_signal"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Audit files currently lacking useful doc_type classification.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of audit rows (top-N after filtering); "
                        "omitted = process all unclassified files")
    p.add_argument("--folder-filter", type=str, default=None,
                   help="case-insensitive substring; restrict to files whose path contains it")
    p.add_argument("--type-filter", type=str,
                   default="other,document,unknown,(empty)",
                   help="comma-separated tag values to audit; use '(empty)' "
                        "for no-sidecar-entry files")
    p.add_argument("--csv", type=Path, default=None,
                   help="write per-file detail to this CSV path")
    p.add_argument("--samples-per-bucket", type=int, default=8,
                   help="how many example filenames to show per guessed bucket")
    p.add_argument("--top-patterns", type=int, default=15,
                   help="how many top filename patterns to show")
    return p.parse_args()


def _load_index():
    """Lazy import to keep --help fast and avoid touching Google libs."""
    try:
        from local_index import get_index  # type: ignore
    except Exception as e:
        print(f"ERROR: cannot import local_index ({e}).", file=sys.stderr)
        sys.exit(2)
    return get_index()


def _iter_files(idx) -> Iterable[Tuple[str, str, str, str]]:
    """Yield (name, uri, path, doc_type) for every searchable file."""
    for entry in getattr(idx, "_files", []) or []:
        if len(entry) != 3:
            continue
        _norm_name, real_name, gs_uri = entry
        path = ""
        if gs_uri.startswith("gs://"):
            rest = gs_uri[5:].split("/", 1)
            if len(rest) == 2:
                path = rest[1]
        doc_type = idx.get_doc_type(gs_uri) or ""
        yield real_name, gs_uri, path, doc_type


def _matches_type_filter(doc_type: str, filter_set: set) -> bool:
    """The (empty) token represents files with no sidecar entry."""
    if doc_type == "":
        return "(empty)" in filter_set
    return doc_type.lower() in filter_set


def main() -> int:
    args = _parse_args()
    type_filter = {t.strip().lower() for t in args.type_filter.split(",") if t.strip()}
    folder_filter = args.folder_filter.lower() if args.folder_filter else None

    print("=" * 70)
    print("doc_type audit: files lacking useful classification")
    print("=" * 70)
    print(f"Type filter: {sorted(type_filter)}")
    if folder_filter:
        print(f"Folder filter: {folder_filter!r}")
    if args.limit:
        print(f"Limit: top {args.limit} unclassified rows")

    print(f"\nLoading local index...")
    idx = _load_index()

    # ── Pass 1: classification distribution across ALL files ──────────
    print(f"\nPass 1: classification distribution across all indexed files...")
    total_seen = 0
    classified_buckets: Counter = Counter()
    for name, uri, path, dt in _iter_files(idx):
        total_seen += 1
        if dt == "":
            classified_buckets["(empty / no sidecar entry)"] += 1
        elif dt.lower() in UNCLASSIFIED_TAGS:
            classified_buckets[dt or "(empty)"] += 1
        else:
            classified_buckets["(classified -- out of audit scope)"] += 1
    print(f"  Indexed files: {total_seen}")
    print(f"\n  doc_type distribution:")
    for label, n in classified_buckets.most_common():
        pct = (n / total_seen) * 100.0 if total_seen else 0.0
        print(f"    {label:<45} {n:>6}  ({pct:5.1f}%)")

    # ── Pass 2: collect unclassified rows that match filters ──────────
    print(f"\nPass 2: applying filters and guessing types...")
    unclassified: List[dict] = []
    for name, uri, path, dt in _iter_files(idx):
        if not _matches_type_filter(dt, type_filter):
            continue
        if folder_filter and folder_filter not in path.lower():
            continue
        ext = ""
        if "." in name:
            ext = "." + name.rsplit(".", 1)[-1].lower()
        bucket, doc_type_guess, basis = _guess(name, path)
        unclassified.append({
            "name":             name,
            "uri":              uri,
            "path":             path,
            "current_doc_type": dt or "(empty)",
            "extension":        ext,
            "guess_bucket":     bucket or "(no_guess)",
            "guess_doc_type":   doc_type_guess or "(no_guess)",
            "guess_basis":      basis,
        })
    print(f"  Unclassified files matching filters: {len(unclassified)}")

    if not unclassified:
        print("\nNothing to audit. Try a different --type-filter or --folder-filter.")
        return 0

    # Optional cap (after filter so the cap acts as a sampling limit
    # rather than skipping entire categories prematurely).
    if args.limit and len(unclassified) > args.limit:
        unclassified_full = unclassified
        unclassified = unclassified[:args.limit]
        print(f"  Capped to top {args.limit} for display (full set = {len(unclassified_full)})")
    else:
        unclassified_full = unclassified

    # ── Roll-up: bucket guesses ───────────────────────────────────────
    print("\n" + "=" * 70)
    print("Roll-up: guessed buckets for unclassified files")
    print("=" * 70)
    bucket_counts: Counter = Counter(r["guess_bucket"] for r in unclassified)
    basis_counts: Counter = Counter(r["guess_basis"] for r in unclassified)
    print(f"\n  Guess basis distribution:")
    for label, n in basis_counts.most_common():
        pct = (n / len(unclassified)) * 100.0
        print(f"    {label:<20} {n:>6}  ({pct:5.1f}%)")

    print(f"\n  Guessed-bucket distribution:")
    for label, n in bucket_counts.most_common():
        pct = (n / len(unclassified)) * 100.0
        print(f"    {label:<20} {n:>6}  ({pct:5.1f}%)")

    # ── Roll-up: extension distribution ───────────────────────────────
    print(f"\n  Extension distribution:")
    ext_counts: Counter = Counter(r["extension"] for r in unclassified)
    for ext, n in ext_counts.most_common(10):
        pct = (n / len(unclassified)) * 100.0
        print(f"    {ext or '(none)':<10} {n:>6}  ({pct:5.1f}%)")

    # ── Top filename patterns (normalized) ────────────────────────────
    print(f"\n" + "=" * 70)
    print(f"Top filename patterns (normalized; top {args.top_patterns})")
    print("=" * 70)
    pattern_counts: Counter = Counter(_normalize_for_pattern(r["name"])
                                       for r in unclassified)
    print(f"\n  (Normalizes dates, year, long IDs, hex strings, and '(N)' suffix)")
    print(f"  {'pattern':<60} {'count':>6}")
    print(f"  {'-' * 60} {'-' * 6}")
    for pat, n in pattern_counts.most_common(args.top_patterns):
        truncated = pat if len(pat) <= 58 else pat[:55] + "..."
        print(f"  {truncated:<60} {n:>6}")

    # ── Sample filenames per guessed bucket ───────────────────────────
    print(f"\n" + "=" * 70)
    print(f"Sample filenames per guessed bucket "
          f"(up to {args.samples_per_bucket} each)")
    print("=" * 70)
    by_bucket: Dict[str, List[dict]] = defaultdict(list)
    for r in unclassified:
        by_bucket[r["guess_bucket"]].append(r)
    # Show non-(no_guess) buckets first -- those are the actionable
    # findings the user cares about most.
    bucket_order = [b for b in sorted(by_bucket.keys()) if b != "(no_guess)"]
    if "(no_guess)" in by_bucket:
        bucket_order.append("(no_guess)")
    for bucket in bucket_order:
        rows = by_bucket[bucket]
        print(f"\n  -- {bucket} -- ({len(rows)} files) --")
        for r in rows[:args.samples_per_bucket]:
            print(f"    {r['name']}")
            print(f"      path: {r['path'][:80]}{'...' if len(r['path']) > 80 else ''}")
            print(f"      current_doc_type={r['current_doc_type']!r} "
                  f"guess={r['guess_doc_type']} basis={r['guess_basis']}")

    # ── Optional CSV export ───────────────────────────────────────────
    if args.csv:
        out_path = args.csv.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "name", "path", "current_doc_type", "extension",
                "guess_bucket", "guess_doc_type", "guess_basis", "uri",
            ])
            for r in unclassified_full:
                w.writerow([
                    r["name"], r["path"], r["current_doc_type"],
                    r["extension"], r["guess_bucket"], r["guess_doc_type"],
                    r["guess_basis"], r["uri"],
                ])
        print(f"\nCSV written to: {out_path}")

    # ── Recommendation ────────────────────────────────────────────────
    print(f"\n" + "=" * 70)
    print(f"Recommendation")
    print("=" * 70)
    actionable = sum(n for label, n in bucket_counts.items()
                     if label not in ("(no_guess)",))
    no_signal = bucket_counts.get("(no_guess)", 0)
    actionable_pct = (actionable / len(unclassified)) * 100.0 if unclassified else 0.0

    print(f"\n  Of {len(unclassified)} unclassified files audited:")
    print(f"    {actionable} ({actionable_pct:.1f}%) have a filename/extension")
    print(f"    signal that suggests a more specific doc_type.")
    print(f"    {no_signal} ({100 - actionable_pct:.1f}%) have no obvious signal.")

    # Specific bucket call-outs that matter most for folder_purpose.
    claim_relevant_buckets = ("insurance", "estimate", "photos")
    claim_signal_count = sum(bucket_counts.get(b, 0) for b in claim_relevant_buckets)
    if claim_signal_count > 0:
        print(f"\n  Claim-relevant signal found: {claim_signal_count} file(s)")
        print(f"  break down as:")
        for b in claim_relevant_buckets:
            n = bucket_counts.get(b, 0)
            if n > 0:
                print(f"    {b:<14} {n}")
        print(f"\n  THESE are the files most worth re-classifying. Even a few")
        print(f"  hundred of these flipping from 'other' to 'insurance' or")
        print(f"  'estimate' would shift many folders from 'unknown' to")
        print(f"  'claim_restoration'.")
    else:
        print(f"\n  No claim-relevant signal (insurance/estimate/photos) found")
        print(f"  among the unclassified files. The corpus genuinely lacks")
        print(f"  these document types as indexed files -- classifier tuning")
        print(f"  alone will not produce more claim_restoration folders.")

    print(f"\n  Verdict on classifier tuning:")
    if actionable_pct >= 30:
        print(f"    WORTH IT. ~{actionable_pct:.0f}% of unclassified files have a")
        print(f"    filename or extension signal the classifier could use.")
        print(f"    Suggested next step: review the per-bucket samples above")
        print(f"    and the CSV (if exported); pick the highest-yield patterns")
        print(f"    to add as classifier rules.")
    elif actionable_pct >= 10:
        print(f"    MARGINAL. ~{actionable_pct:.0f}% have a signal. Worth at least")
        print(f"    looking at the per-bucket samples, but the expected impact")
        print(f"    on folder_purpose distribution is modest.")
    else:
        print(f"    LOW LEVERAGE. Only {actionable_pct:.0f}% of unclassified files")
        print(f"    have a filename signal. The classifier isn't the bottleneck;")
        print(f"    most unclassified files are genuinely ambiguous from name")
        print(f"    alone. Consider OCR-based classification or accept the")
        print(f"    'unknown' rate.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
