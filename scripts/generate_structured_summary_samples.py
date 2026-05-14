"""Controlled batch sample generator for structured_summary records.

Two modes:

    INVENTORY MODE (--inventory)
        Scans every folder the LocalFileIndex knows about, computes
        cheap metadata (file count, bucket distribution, predicted
        folder_purpose, sample filenames), and prints a report.
        Optionally writes the inventory to a CSV at --export-csv so
        you can sort/filter/annotate in a spreadsheet before picking
        a representative sample.
        Pure local -- no HTTP, no Gemini, no API cost.

    GENERATE MODE (default)
        Reads a curated folder list from --folders-file (one folder
        name per line) and drives the existing /api/chat endpoint
        with deterministic prompts for each. Records are persisted
        through the existing JSONL sink.

The two-step workflow:

    1. python scripts/generate_structured_summary_samples.py \\
           --inventory --export-csv folder_inventory.csv
    2. Open the CSV, pick representative folders, write their names
       to selected_folders.txt (one per line).
    3. python scripts/generate_structured_summary_samples.py \\
           --folders-file selected_folders.txt --dry-run
    4. python scripts/generate_structured_summary_samples.py \\
           --folders-file selected_folders.txt
    5. python scripts/analyze_structured_summaries.py

Read-only against the chat module -- inventory replicates the chat
path's deterministic bucket and purpose classification locally rather
than importing job_intelligence (which would pull in Google client
libraries and Gemini auth). The replicated logic is small and stable;
if either side changes, this script's output will drift until they
re-align.
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_API_BASE = "http://localhost:5000"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "structured_summaries" / "structured_summary_events.jsonl"

SUMMARY_PROMPT_TEMPLATE = "summarize {folder}"
OPEN_ITEMS_PROMPT = "what's missing"
INTER_CALL_SLEEP_SEC = 0.5

# Number of sample filenames to surface per folder in inventory mode.
INVENTORY_SAMPLE_FILES = 3
# Cap folder enumeration per folder in inventory mode -- exact counts
# beyond this aren't useful; report "500+" instead.
INVENTORY_FILE_COUNT_CAP = 500


# ── Replicated chat-path constants (kept in sync manually) ─────────────
# Mirrors _FOLDER_SEARCHABLE_EXTS in job_intelligence.py. Drift risk is
# low because these are extension lists, not behavior.
_SEARCHABLE_EXTS = (
    ".pdf", ".docx", ".doc", ".xlsx", ".xls",
    ".csv", ".txt", ".pptx", ".ppt", ".md",
)

# Mirrors _DOC_TYPE_BUCKETS in job_intelligence.py. Used for folder_purpose
# prediction in inventory mode. If the chat path adds a new doc_type, this
# script will categorize those files as "other" until updated -- harmless
# but worth noting.
_DOC_TYPE_BUCKETS: Dict[str, str] = {
    "appraisal": "appraisal", "assessment": "appraisal",
    "estimate": "estimate", "scope_of_work": "estimate", "sow": "estimate",
    "invoice": "invoice", "bill": "invoice", "receipt": "invoice",
    "contract": "contract", "agreement": "contract", "signed_contract": "contract",
    "insurance_policy": "insurance", "claim": "insurance",
    "claim_documents": "insurance", "adjustment": "insurance",
    "inspection_report": "report", "environmental_report": "report",
    "soot_report": "report", "report": "report",
    "permit": "permit", "certificate_of_occupancy": "permit",
    "violation_report": "permit",
    "correspondence": "correspondence", "demand_letter": "correspondence",
    "letter": "correspondence", "email": "correspondence",
    "spreadsheet": "spreadsheet",
    "closing_statement": "appraisal", "closing_package": "appraisal",
    "pl_statement": "spreadsheet",
    "deed": "contract", "title_report": "contract",
    "loan_document": "contract", "draw_request": "invoice",
}
_CLAIM_BUCKETS = {"insurance", "estimate", "photos"}
_PROPERTY_BUCKETS = {"appraisal", "contract"}


def _bucket_for(doc_type: str, filename: str = "") -> str:
    """Mirror of JobIntelligence._bucket_for_doc_type. See note above."""
    dt = (doc_type or "").strip().lower()
    if dt in _DOC_TYPE_BUCKETS:
        return _DOC_TYPE_BUCKETS[dt]
    nl = (filename or "").lower()
    if any(nl.endswith(e) for e in (".xlsx", ".xls", ".csv")):
        return "spreadsheet"
    if any(nl.endswith(e) for e in (".jpg", ".jpeg", ".png", ".heic",
                                      ".heif", ".tiff", ".tif", ".webp", ".gif")):
        return "photos"
    return "other"


def _classify_purpose(buckets_present: set) -> str:
    """Mirror of JobIntelligence._classify_folder_purpose. Pure function."""
    if buckets_present & _CLAIM_BUCKETS:
        return "claim_restoration"
    if buckets_present & _PROPERTY_BUCKETS:
        return "property_appraisal"
    return "unknown"


# ── Arg parsing ────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Inventory folders and/or generate structured_summary samples.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Mode flags ---------------------------------------------------------
    p.add_argument("--inventory", action="store_true",
                   help="run inventory mode (read-only scan, no HTTP, no Gemini)")
    p.add_argument("--export-csv", type=Path, default=None,
                   help="(inventory mode) write the full inventory to this CSV path")

    # Folder selection ---------------------------------------------------
    p.add_argument("--folder-filter", type=str, default=None,
                   help="(any mode) case-insensitive substring; restrict folders to those matching")
    p.add_argument("--folders-file", type=Path, default=None,
                   help="(generate mode) text file with one folder name per line; only these will be sampled")
    p.add_argument("--limit", type=int, default=None,
                   help="(inventory mode) cap the inventory at N folders for quick scans; full corpus if omitted")

    # Execution ----------------------------------------------------------
    p.add_argument("--dry-run", action="store_true",
                   help="(generate mode) list planned prompts but make no HTTP calls")
    p.add_argument("--include-open-items", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="(generate mode) also send 'what's missing' for each folder")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help="JSONL sink path (used for line-count reporting only)")
    p.add_argument("--api-base", default=DEFAULT_API_BASE,
                   help="(generate mode) base URL of the Flask server")
    p.add_argument("--yes", action="store_true",
                   help="(generate mode) skip the confirmation prompt")

    return p.parse_args()


# ── Folder discovery + per-folder scan (inventory mode) ────────────────
def _discover_index():
    """Load LocalFileIndex lazily. Returns (index, all_folders_sorted)."""
    try:
        from local_index import get_index  # type: ignore
    except Exception as e:
        print(f"ERROR: cannot import local_index ({e}).", file=sys.stderr)
        sys.exit(2)
    idx = get_index()
    folders = sorted(idx.get_property_folders())
    return idx, folders


def _files_under_folder(idx, folder_name: str) -> List[Tuple[str, str]]:
    """Enumerate (filename, uri) pairs under `folder_name`. Mirrors
    JobIntelligence._enumerate_folder but skips path/subfolder bookkeeping
    that inventory mode doesn't need. Capped at INVENTORY_FILE_COUNT_CAP+1
    so we can report "500+" without paying for huge folders we don't care
    about at this granularity.
    """
    from local_index import _normalize  # type: ignore
    folder_norm = _normalize(folder_name)
    if not folder_norm:
        return []
    out: List[Tuple[str, str]] = []
    for entry in getattr(idx, "_files", []) or []:
        if len(entry) != 3:
            continue
        _norm_name, real_name, gs_uri = entry
        rl = real_name.lower()
        if not any(rl.endswith(ext) for ext in _SEARCHABLE_EXTS):
            continue
        path = ""
        if gs_uri.startswith("gs://"):
            rest = gs_uri[5:].split("/", 1)
            if len(rest) == 2:
                path = rest[1]
        if not path:
            continue
        segments = path.split("/")
        matched = False
        for seg in segments[:-1]:
            if _normalize(seg) == folder_norm:
                matched = True
                break
        if not matched:
            continue
        out.append((real_name, gs_uri))
        if len(out) > INVENTORY_FILE_COUNT_CAP:
            break
    return out


def _inventory_folder(idx, folder_name: str) -> Dict:
    """Compute metadata for one folder. Pure local."""
    files = _files_under_folder(idx, folder_name)
    capped = len(files) > INVENTORY_FILE_COUNT_CAP
    if capped:
        files = files[:INVENTORY_FILE_COUNT_CAP]
    bucket_counts: Counter = Counter()
    for name, uri in files:
        try:
            doc_type = idx.get_doc_type(uri) or ""
        except Exception:
            doc_type = ""
        bucket_counts[_bucket_for(doc_type, name)] += 1
    buckets_present = {b for b, n in bucket_counts.items() if n > 0}
    purpose = _classify_purpose(buckets_present)
    samples = [name for name, _ in files[:INVENTORY_SAMPLE_FILES]]
    return {
        "folder_name": folder_name,
        "file_count": len(files),
        "file_count_display": f"{INVENTORY_FILE_COUNT_CAP}+" if capped else str(len(files)),
        "predicted_purpose": purpose,
        "buckets": dict(bucket_counts),
        "sample_files": samples,
    }


def _format_buckets(buckets: Dict[str, int]) -> str:
    """Compact bucket display: 'appraisal=2, contract=2, other=4'."""
    if not buckets:
        return "(empty)"
    items = sorted(buckets.items(), key=lambda x: (-x[1], x[0]))
    return ", ".join(f"{b}={n}" for b, n in items)


def _run_inventory(args: argparse.Namespace) -> int:
    print("=" * 70)
    print("Folder inventory mode")
    print("=" * 70)
    print("Reading folder list from local index...")
    idx, all_folders = _discover_index()
    print(f"  Discovered {len(all_folders)} property folders.")

    # Filter -------------------------------------------------------------
    candidates = all_folders
    if args.folder_filter:
        needle = args.folder_filter.lower()
        candidates = [f for f in candidates if needle in f.lower()]
        print(f"  After --folder-filter {args.folder_filter!r}: {len(candidates)}")
    if args.limit and len(candidates) > args.limit:
        candidates = candidates[:args.limit]
        print(f"  After --limit {args.limit}: {len(candidates)} "
              f"(alphabetical head -- prefer --folder-filter for targeted scans)")

    if not candidates:
        print("\nNo folders match the filter. Exiting.")
        return 0

    # Scan ---------------------------------------------------------------
    print(f"\nScanning {len(candidates)} folder(s) (read-only, no API calls)...")
    rows: List[Dict] = []
    t0 = time.time()
    for i, folder in enumerate(candidates, 1):
        rows.append(_inventory_folder(idx, folder))
        # Light progress every 100 folders for big scans.
        if i % 100 == 0:
            print(f"  scanned {i}/{len(candidates)}...")
    elapsed = time.time() - t0
    print(f"  scan complete in {elapsed:.1f}s")

    # Roll-up summary ----------------------------------------------------
    print("\n" + "=" * 70)
    print("Roll-up")
    print("=" * 70)
    purposes: Counter = Counter(r["predicted_purpose"] for r in rows)
    print(f"\n  Predicted folder_purpose distribution:")
    for p, n in purposes.most_common():
        pct = (n / len(rows)) * 100.0
        print(f"    {p:<22} {n:>5}  ({pct:5.1f}%)")
    file_counts = [r["file_count"] for r in rows]
    print(f"\n  File counts: total={sum(file_counts)} "
          f"avg={sum(file_counts) / len(file_counts):.1f} "
          f"min={min(file_counts)} max={max(file_counts)}")
    empty_count = sum(1 for r in rows if r["file_count"] == 0)
    print(f"  Empty folders (zero searchable files): {empty_count}")

    bucket_totals: Counter = Counter()
    for r in rows:
        for b, n in r["buckets"].items():
            bucket_totals[b] += n
    print(f"\n  Doc-type bucket totals across all scanned folders:")
    for b, n in bucket_totals.most_common():
        print(f"    {b:<16} {n}")

    # Stratified samples for the user to look at ------------------------
    # For each predicted purpose, show a handful of example folders the
    # user can consider selecting. This is the value-add over `--limit
    # 50 --dry-run` -- you see candidates GROUPED by what they look like.
    print(f"\n" + "=" * 70)
    print("Sample folders by predicted purpose (for selection)")
    print("=" * 70)
    by_purpose: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        by_purpose[r["predicted_purpose"]].append(r)
    for purpose in ("claim_restoration", "property_appraisal", "unknown"):
        bucket = by_purpose.get(purpose, [])
        if not bucket:
            continue
        # Sort: prefer folders with more files (more substantive samples)
        bucket.sort(key=lambda r: -r["file_count"])
        print(f"\n  -- {purpose} -- ({len(bucket)} folders total) --")
        for r in bucket[:8]:
            print(f"    {r['folder_name']!r:<60}")
            print(f"      files={r['file_count_display']}  "
                  f"buckets={_format_buckets(r['buckets'])}")
            if r["sample_files"]:
                for f in r["sample_files"]:
                    print(f"        - {f}")

    # CSV export ---------------------------------------------------------
    if args.export_csv:
        out_path = args.export_csv.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Stable column set: identity + scan results + samples flattened.
        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "folder_name",
                "file_count",
                "predicted_purpose",
                "buckets",
                "sample_file_1",
                "sample_file_2",
                "sample_file_3",
                "select",  # blank column for the user to mark Y/N
            ])
            for r in rows:
                samples = r["sample_files"] + [""] * 3
                w.writerow([
                    r["folder_name"],
                    r["file_count_display"],
                    r["predicted_purpose"],
                    _format_buckets(r["buckets"]),
                    samples[0], samples[1], samples[2],
                    "",
                ])
        print(f"\n  CSV written to: {out_path}")
        print(f"  Suggestion: open in Excel, mark Y in the 'select' column,")
        print(f"  then either filter and copy folder_names to a text file,")
        print(f"  or run: awk -F, '$8==\"Y\"{{print $1}}' file.csv > selected_folders.txt")

    print(f"\n" + "=" * 70)
    print("Inventory complete.")
    print("Next: create a text file of folders to sample (one per line) and run:")
    print("  python scripts/generate_structured_summary_samples.py \\")
    print("      --folders-file selected_folders.txt --dry-run")
    print("=" * 70)
    return 0


# ── HTTP helpers (generate mode) ───────────────────────────────────────
def _post(url: str, body: dict, timeout: float = 180.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _server_reachable(api_base: str) -> bool:
    try:
        _post(api_base + "/api/chat/new", {}, timeout=5.0)
        return True
    except (urllib.error.URLError, TimeoutError):
        return False


def _count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _read_folders_file(path: Path) -> List[str]:
    """Read a text file with one folder name per line.

    Strips whitespace, drops blank lines and lines starting with '#'.
    Preserves order so the user can control sample sequencing.
    Reports unknown folders (lines that don't match any known folder
    name) but doesn't refuse to run.
    """
    if not path.exists():
        print(f"ERROR: --folders-file not found: {path}", file=sys.stderr)
        sys.exit(2)
    folders: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            folders.append(stripped)
    if not folders:
        print(f"ERROR: --folders-file is empty: {path}", file=sys.stderr)
        sys.exit(2)
    return folders


def _run_one_folder(api_base: str, session_id: str, folder: str,
                     include_open_items: bool) -> dict:
    result = {
        "folder": folder,
        "summary": {"ok": False, "kind": None, "purpose": None, "error": None},
    }
    if include_open_items:
        result["open_items"] = {"ok": False, "kind": None, "purpose": None, "error": None}

    prompt = SUMMARY_PROMPT_TEMPLATE.format(folder=folder)
    try:
        r = _post(api_base + "/api/chat",
                   {"query": prompt, "session_id": session_id})
        ss = r.get("structured_summary")
        if isinstance(ss, dict):
            result["summary"]["ok"] = True
            result["summary"]["kind"] = ss.get("response_kind")
            result["summary"]["purpose"] = ss.get("folder_purpose")
        else:
            result["summary"]["error"] = (
                f"no structured_summary (folder not detected; "
                f"answer={(r.get('answer') or '')[:80]!r})"
            )
    except Exception as e:
        result["summary"]["error"] = f"{type(e).__name__}: {e}"

    if not include_open_items:
        return result

    time.sleep(INTER_CALL_SLEEP_SEC)
    try:
        r = _post(api_base + "/api/chat",
                   {"query": OPEN_ITEMS_PROMPT, "session_id": session_id})
        ss = r.get("structured_summary")
        if isinstance(ss, dict):
            result["open_items"]["ok"] = True
            result["open_items"]["kind"] = ss.get("response_kind")
            result["open_items"]["purpose"] = ss.get("folder_purpose")
        else:
            result["open_items"]["error"] = (
                "no structured_summary on follow-up (context may not have anchored)"
            )
    except Exception as e:
        result["open_items"]["error"] = f"{type(e).__name__}: {e}"

    return result


def _summary_block(results: List[dict], include_open_items: bool,
                    baseline_lines: int, final_lines: int) -> None:
    print("\n" + "=" * 70)
    print("Run report")
    print("=" * 70)
    ok_summary = sum(1 for r in results if r["summary"]["ok"])
    ok_oi = sum(1 for r in results
                if include_open_items and r.get("open_items", {}).get("ok"))
    fail_summary = len(results) - ok_summary

    print(f"\n  Folders attempted:        {len(results)}")
    print(f"  Summary prompts ok:       {ok_summary} / {len(results)}")
    if include_open_items:
        print(f"  Open-items prompts ok:    {ok_oi} / {len(results)}")
    print(f"  Summary prompts failed:   {fail_summary}")

    purposes: Counter = Counter()
    for r in results:
        if r["summary"]["ok"]:
            purposes[r["summary"]["purpose"] or "(none)"] += 1
    if purposes:
        print(f"\n  Observed folder_purpose distribution:")
        for p, n in purposes.most_common():
            print(f"    {p:<22} {n}")

    print(f"\n  JSONL rows: {baseline_lines} -> {final_lines}  "
          f"(+{final_lines - baseline_lines})")

    failed = [r for r in results if not r["summary"]["ok"]]
    if failed:
        print(f"\n  Failures:")
        for r in failed:
            print(f"    {r['folder']!r}: {r['summary']['error']}")


def _run_generate(args: argparse.Namespace) -> int:
    print("=" * 70)
    print("structured_summary sample generator")
    print("=" * 70)
    print(f"API base:     {args.api_base}")
    print(f"Output JSONL: {args.output}")

    # Selection: --folders-file is required for generate mode now. The
    # old random-sampling path is intentionally removed -- the inventory
    # output showed that random samples are not representative.
    if not args.folders_file:
        print("\nERROR: --folders-file is required for generate mode.", file=sys.stderr)
        print("       Run --inventory first to choose folders, then create a", file=sys.stderr)
        print("       text file (one folder name per line) and pass it via", file=sys.stderr)
        print("       --folders-file.", file=sys.stderr)
        return 2

    selected = _read_folders_file(args.folders_file)
    print(f"Folders from {args.folders_file}: {len(selected)}")

    # Cross-check against the local index so users see immediately when
    # a curated name doesn't match a known folder.
    print(f"\nValidating folder names against local index...")
    _, all_folders = _discover_index()
    known = set(all_folders)
    unknown = [f for f in selected if f not in known]
    matched = [f for f in selected if f in known]
    print(f"  Known:   {len(matched)}")
    print(f"  Unknown: {len(unknown)}")
    if unknown:
        print(f"\n  Unknown folder names (these will still be sent -- the chat")
        print(f"  endpoint may fuzzy-match them, but be aware they aren't")
        print(f"  exact-match in the index):")
        for f in unknown[:20]:
            print(f"    {f!r}")
        if len(unknown) > 20:
            print(f"    ...and {len(unknown) - 20} more")

    # Plan ---------------------------------------------------------------
    prompts_per_folder = 2 if args.include_open_items else 1
    total_calls = len(selected) * prompts_per_folder
    print(f"\nPlanned prompts ({total_calls} total HTTP call(s)):")
    preview = selected[:20]
    for f in preview:
        print(f"  - summarize {f}")
        if args.include_open_items:
            print(f"      what's missing")
    if len(selected) > 20:
        print(f"  ...and {len(selected) - 20} more folder(s)")

    if args.dry_run:
        print("\n[dry-run] No HTTP calls made. Re-run without --dry-run to execute.")
        return 0

    # Reachability + confirmation ---------------------------------------
    if not _server_reachable(args.api_base):
        print(f"\nERROR: cannot reach Flask server at {args.api_base}.",
              file=sys.stderr)
        return 2

    if not args.yes:
        billable = len(selected)
        print(f"\nThis run will make {total_calls} HTTP call(s) "
              f"(~{billable} Gemini Flash call(s)).")
        try:
            answer = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    # Execute ------------------------------------------------------------
    baseline_lines = _count_jsonl_lines(args.output)
    print(f"\nBaseline JSONL line count: {baseline_lines}")
    print(f"\nStarting batch...")
    results = []
    start = time.time()
    for i, folder in enumerate(selected, 1):
        try:
            session = _post(args.api_base + "/api/chat/new", {}, timeout=10.0)
            session_id = session.get("session_id")
            if not session_id:
                print(f"  [{i}/{len(selected)}] {folder!r}: FAIL (no session_id)")
                results.append({"folder": folder,
                                "summary": {"ok": False, "error": "no session_id"}})
                continue
        except Exception as e:
            print(f"  [{i}/{len(selected)}] {folder!r}: FAIL (session: {e})")
            results.append({"folder": folder,
                            "summary": {"ok": False, "error": f"session: {e}"}})
            continue

        print(f"  [{i}/{len(selected)}] {folder!r}...", end=" ", flush=True)
        result = _run_one_folder(args.api_base, session_id, folder,
                                  args.include_open_items)
        results.append(result)

        s = result["summary"]
        oi = result.get("open_items")
        bits = []
        if s["ok"]:
            bits.append(f"summary=OK ({s.get('purpose')})")
        else:
            bits.append("summary=FAIL")
        if oi is not None:
            bits.append("open_items=OK" if oi["ok"] else "open_items=FAIL")
        print(", ".join(bits))

        time.sleep(INTER_CALL_SLEEP_SEC)
    elapsed = time.time() - start

    time.sleep(0.2)
    final_lines = _count_jsonl_lines(args.output)
    _summary_block(results, args.include_open_items, baseline_lines, final_lines)
    print(f"\n  Elapsed: {elapsed:.1f}s")
    print(f"\nNext step:")
    print(f"  python scripts/analyze_structured_summaries.py")
    return 0


# ── Main ───────────────────────────────────────────────────────────────
def main() -> int:
    args = _parse_args()
    if args.inventory:
        return _run_inventory(args)
    return _run_generate(args)


if __name__ == "__main__":
    sys.exit(main())
