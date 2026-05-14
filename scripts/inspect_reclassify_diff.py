"""Read-only spot-check of reclassification changes.

Compares the in-memory sidecar (what the running system uses) against
the local preview JSON (what reclassify_doc_types.py just produced)
and prints sample filenames for specific before/after transitions.

Purpose: before uploading a refreshed sidecar to GCS, eyeball whether
the changes look right. Particularly:
  - Files that LOST a useful classification (e.g. appraisal -> document)
  - Files that GAINED a useful classification (e.g. document -> insurance_policy)
  - Any transition the user wants to inspect via --before/--after filters

Read-only -- never writes anything, never touches GCS.

Usage:
    # Show every transition with a sample of filenames
    python scripts/inspect_reclassify_diff.py

    # Focus on a specific transition
    python scripts/inspect_reclassify_diff.py --before appraisal

    # Show more samples per transition
    python scripts/inspect_reclassify_diff.py --samples 20

    # Use a different preview path
    python scripts/inspect_reclassify_diff.py --preview path/to/preview.json
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_PREVIEW = REPO_ROOT / "data" / "doc_type_index.preview.json"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Spot-check before/after transitions from a reclassify dry-run.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--preview", type=Path, default=DEFAULT_PREVIEW,
                   help="path to the preview JSON written by reclassify_doc_types.py")
    p.add_argument("--before", default=None,
                   help="filter: only show transitions where the BEFORE tag matches")
    p.add_argument("--after", default=None,
                   help="filter: only show transitions where the AFTER tag matches")
    p.add_argument("--samples", type=int, default=10,
                   help="how many sample filenames to show per transition")
    p.add_argument("--min-count", type=int, default=1,
                   help="hide transitions with fewer than this many files")
    return p.parse_args()


def _load_index_and_classifier():
    """Lazy import. We need the LocalFileIndex for both the old sidecar
    (already loaded from GCS at startup) and the URI -> filename lookup."""
    try:
        from local_index import get_index  # type: ignore
    except Exception as e:
        print(f"ERROR: cannot import local_index ({e}).", file=sys.stderr)
        sys.exit(2)
    return get_index()


def main() -> int:
    args = _parse_args()

    print("=" * 70)
    print("Reclassification spot-check (read-only)")
    print("=" * 70)

    # ── Load preview ─────────────────────────────────────────────────────
    if not args.preview.exists():
        print(f"ERROR: preview file not found: {args.preview}", file=sys.stderr)
        print(f"       Run reclassify_doc_types.py first to generate it.",
              file=sys.stderr)
        return 2
    with open(args.preview, "r", encoding="utf-8") as fh:
        new_sidecar: Dict[str, str] = json.load(fh)
    print(f"Loaded preview: {len(new_sidecar)} entries from {args.preview}")

    # ── Load index + current sidecar ─────────────────────────────────────
    print(f"\nLoading local index (this also reads the current GCS sidecar)...")
    idx = _load_index_and_classifier()
    old_sidecar: Dict[str, str] = dict(getattr(idx, "_doc_type_by_uri", {}) or {})

    # Build a URI -> filename lookup from the index so we can show real names.
    uri_to_name: Dict[str, str] = {}
    for entry in getattr(idx, "_files", []) or []:
        if len(entry) != 3:
            continue
        _norm_name, real_name, gs_uri = entry
        if gs_uri:
            uri_to_name[gs_uri] = real_name

    # ── Group URIs by (before, after) transition ─────────────────────────
    transitions: Dict[tuple, List[str]] = defaultdict(list)
    for uri, new_tag in new_sidecar.items():
        old_tag = old_sidecar.get(uri, "(none)")
        if old_tag == new_tag:
            continue
        if args.before and old_tag != args.before:
            continue
        if args.after and new_tag != args.after:
            continue
        transitions[(old_tag, new_tag)].append(uri)

    if not transitions:
        print(f"\nNo transitions match the filters. Try different --before/--after.")
        return 0

    # Sort transitions by descending count so big-impact rows print first.
    ordered = sorted(transitions.items(), key=lambda kv: -len(kv[1]))

    # ── Print samples for each transition ────────────────────────────────
    for (old_tag, new_tag), uris in ordered:
        if len(uris) < args.min_count:
            continue
        print(f"\n{'=' * 70}")
        print(f"  {old_tag!r}  ->  {new_tag!r}    ({len(uris)} file(s))")
        print('=' * 70)
        # Sample up to --samples filenames. Use sorted order for stable
        # output across runs.
        sampled = sorted(uris)[:args.samples]
        for uri in sampled:
            name = uri_to_name.get(uri, "(filename not in index)")
            # Strip the long gs:// prefix for readability
            short_uri = uri
            if uri.startswith("gs://"):
                tail = uri.split("/", 3)[-1] if uri.count("/") >= 3 else uri
                short_uri = ".../" + tail[-60:] if len(tail) > 60 else tail
            print(f"    {name}")
            print(f"      uri: {short_uri}")
        if len(uris) > args.samples:
            print(f"    ...and {len(uris) - args.samples} more")

    print(f"\n{'=' * 70}")
    print(f"Total transitions shown: {sum(1 for k, v in ordered if len(v) >= args.min_count)}")
    print(f"Total files moved across all shown transitions: "
          f"{sum(len(v) for k, v in ordered if len(v) >= args.min_count)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
