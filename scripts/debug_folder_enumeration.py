"""Read-only diagnostic for _enumerate_folder zero-file failures.

For a list of property folder names, walks the LocalFileIndex._files and
reports:
  1. How many files would currently match (strict _normalize equality).
  2. How many files would match under various loosened comparisons,
     to identify which normalization rule is the bottleneck.

Output is a per-folder table so the user can see WHICH messy folder
names hit which kind of mismatch. No code changes, no GCS writes.

Usage:
    python scripts/debug_folder_enumeration.py \\
        --folder "916950_Labon - claim paid & closed" \\
        --folder "Michelle Berry -toilet overflow google lead - 198-19 118th Ave" \\
        --folder "Trish Wallace (dad) Albert - Yelp, Lead Mold & Water - 2 Harvard Pl" \\
        --folder "Chris Simon Reco Phil Trustfi - 24 Laurie Blvd" \\
        --folder "27 Manor Drive"

For each folder, prints:
    folder_name                   strict   contains  prefix5  prefix10
    ---------------------------- -------- --------- -------- --------
    ...                              0       0        45       45

Where:
  strict   = current _enumerate_folder logic (_normalize(seg) == folder_norm)
  contains = any segment normalizes to a SUPERSTRING of folder_norm
  prefix5  = any segment shares the first 5 normalized tokens with folder_norm
  prefix10 = any segment shares the first 10 normalized tokens

Also prints up to 3 sample paths from the "contains" hit set so you can
SEE what the actual GCS path segments look like vs. what was expected.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Diagnose why _enumerate_folder returns 0 files for messy folder names.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--folder", action="append", default=[],
                   help="folder name to test; repeat for multiple folders")
    p.add_argument("--samples", type=int, default=3,
                   help="how many sample matched-path lines to show per folder")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.folder:
        print("ERROR: pass at least one --folder", file=sys.stderr)
        return 2

    print("=" * 72)
    print("_enumerate_folder zero-file diagnostic (read-only)")
    print("=" * 72)

    try:
        from local_index import get_index, _normalize  # type: ignore
    except Exception as e:
        print(f"ERROR: cannot import local_index ({e}).", file=sys.stderr)
        return 2

    idx = get_index()
    files = getattr(idx, "_files", []) or []
    print(f"Index has {len(files)} files")
    print(f"Index has {len(getattr(idx, '_property_folders', set()))} property folders")
    print()

    # Per-folder analysis
    print(f"  {'folder':<60} {'strict':>7} {'contains':>9} {'pref5':>7} {'pref10':>7}")
    print(f"  {'-' * 60} {'-' * 7} {'-' * 9} {'-' * 7} {'-' * 7}")

    detail_rows = []
    for folder in args.folder:
        folder_norm = _normalize(folder)
        folder_tokens = folder_norm.split()
        if not folder_norm:
            print(f"  {folder!r:<60} <empty normalize>")
            continue

        # Walk every file once, accumulate counts for each comparison type
        strict_hits = []
        contains_hits = []   # segment normalized contains folder_norm as a substring
        prefix5_hits = []
        prefix10_hits = []
        # Also: capture ALL distinct path segments that share at least 3 tokens
        # with the folder name (for sample reporting)
        seg_samples = {}      # normalized_segment -> example real path

        for entry in files:
            if len(entry) != 3:
                continue
            _norm_name, real_name, gs_uri = entry
            if not gs_uri or not gs_uri.startswith("gs://"):
                continue
            # Extract path after gs://bucket/
            rest = gs_uri[5:].split("/", 1)
            if len(rest) != 2:
                continue
            path = rest[1]
            segments = path.split("/")
            # Examine all folder segments (not the file)
            for seg in segments[:-1]:
                seg_norm = _normalize(seg)
                if not seg_norm:
                    continue
                seg_tokens = seg_norm.split()

                # Strict equality (what _enumerate_folder uses)
                if seg_norm == folder_norm:
                    strict_hits.append(path)

                # Contains: folder_norm is a substring of seg_norm
                # OR seg_norm is a substring of folder_norm (loose containment)
                if (folder_norm in seg_norm and seg_norm != folder_norm) \
                        or (seg_norm in folder_norm and len(seg_norm) >= 10):
                    contains_hits.append(path)
                    if seg_norm not in seg_samples:
                        seg_samples[seg_norm] = path

                # Prefix-5: first 5 normalized tokens match
                if (len(seg_tokens) >= 5 and len(folder_tokens) >= 5
                        and seg_tokens[:5] == folder_tokens[:5]
                        and seg_norm != folder_norm):
                    prefix5_hits.append(path)
                    if seg_norm not in seg_samples:
                        seg_samples[seg_norm] = path

                # Prefix-10
                if (len(seg_tokens) >= 10 and len(folder_tokens) >= 10
                        and seg_tokens[:10] == folder_tokens[:10]
                        and seg_norm != folder_norm):
                    prefix10_hits.append(path)

        folder_display = folder if len(folder) <= 58 else folder[:55] + "..."
        print(f"  {folder_display!r:<60} "
              f"{len(strict_hits):>7} {len(contains_hits):>9} "
              f"{len(prefix5_hits):>7} {len(prefix10_hits):>7}")
        detail_rows.append((folder, folder_norm, strict_hits, contains_hits,
                            prefix5_hits, prefix10_hits, seg_samples))

    print()
    print("=" * 72)
    print("Per-folder detail: sample mismatching segments")
    print("=" * 72)
    for (folder, folder_norm, strict_hits, contains_hits,
         prefix5_hits, prefix10_hits, seg_samples) in detail_rows:
        print()
        print(f"FOLDER: {folder!r}")
        print(f"  folder_norm: {folder_norm!r}")
        print(f"  STRICT match (current _enumerate_folder): {len(strict_hits)} hits")
        if strict_hits:
            for p in strict_hits[:args.samples]:
                print(f"      {p}")

        # Identify the most informative samples: segments that are
        # CLOSE to folder_norm but not equal.
        if not strict_hits:
            print(f"  CLOSE-BUT-NOT-EQUAL segments seen on file paths:")
            shown = 0
            for seg_norm, sample_path in seg_samples.items():
                if shown >= args.samples:
                    break
                # Length-difference summary so user can see what's off
                len_diff = len(seg_norm) - len(folder_norm)
                # Token set diff to highlight what's different
                fset = set(folder_norm.split())
                sset = set(seg_norm.split())
                missing_in_seg = fset - sset
                extra_in_seg = sset - fset
                print(f"    seen segment normalized: {seg_norm!r}")
                print(f"      sample path:           {sample_path}")
                print(f"      length diff:           {len_diff:+d} chars")
                if missing_in_seg:
                    print(f"      tokens in folder, not seg: "
                          f"{sorted(missing_in_seg)[:10]}")
                if extra_in_seg:
                    print(f"      tokens in seg, not folder: "
                          f"{sorted(extra_in_seg)[:10]}")
                shown += 1
            if shown == 0:
                print(f"    (none -- no segment shares 5+ tokens or contains folder name)")
    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
