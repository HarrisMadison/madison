"""Re-classify all indexed filenames with the current classifier and
emit a refreshed doc_type sidecar.

This script makes the updated _classify_doc_type rules in
Phase5_oneDrive/phase6_ocr_metadata.py actually take effect on the
existing corpus *without* re-running the full OneDrive sync. The sync
is expensive (downloads files, runs OCR, talks to Vertex). All this
script needs to do is read the existing local index, run each filename
through the current classifier, and write a new sidecar JSON.

Defaults to a SAFE dry-run mode: writes the refreshed sidecar to a
local file, prints a before/after diff summary, and does NOT touch GCS.
Pass --upload to push the new sidecar to GCS (replacing the production
sidecar). The Flask server will pick up the new sidecar on its next
restart.

Usage:
    # Dry-run: show what would change, write local preview, don't touch GCS
    python scripts/reclassify_doc_types.py

    # Same but write the preview to a specific path
    python scripts/reclassify_doc_types.py --output preview.json

    # Apply: upload to GCS and overwrite the production sidecar
    python scripts/reclassify_doc_types.py --upload

    # Apply, but also keep a local backup of the previous sidecar
    python scripts/reclassify_doc_types.py --upload --backup-prev backup.json
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "Phase5_oneDrive"))

# ── Tags that must be preserved across reclassification ───────────────
# These are system-generated markers that the filename classifier in
# Phase5_oneDrive/phase6_ocr_metadata.py cannot reproduce. Overwriting
# them with a filename-derived tag loses information.
#
# large_pdf_pointer:
#   Set by Phase5_oneDrive/onedrive_sync.py when a PDF exceeds 8 MB.
#   The size threshold is what assigns this tag, not the filename. The
#   sync code DELIBERATELY forces this tag back over the result of
#   _enrich_metadata (see onedrive_sync.py line 561), making it clear
#   the author wanted size-based routing to win over filename rules.
#   No current retrieval code branches on this tag, but the information
#   ("this PDF has no extracted content because it's too big") is real
#   and worth keeping for future features.
#
# Add new tags here only after confirming they are produced by a code
# path OTHER than _classify_doc_type and convey information that
# filename rules cannot recover.
_PRESERVE_TAGS: frozenset = frozenset({
    "large_pdf_pointer",
})


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Re-classify indexed filenames with the current rules.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--output", type=Path,
                   default=REPO_ROOT / "data" / "doc_type_index.preview.json",
                   help="local path for the refreshed sidecar JSON (always written)")
    p.add_argument("--upload", action="store_true",
                   help="upload the refreshed sidecar to GCS "
                        "(manifests/doc_type_index.json) -- production-affecting")
    p.add_argument("--backup-prev", type=Path, default=None,
                   help="(with --upload) save the previous GCS sidecar to this path "
                        "before overwriting, for rollback")
    p.add_argument("--top-transitions", type=int, default=20,
                   help="how many before->after transitions to show in the diff summary")
    return p.parse_args()


def _load_index_and_classifier():
    """Lazy imports -- keeps --help fast and avoids Google libs unless we run."""
    try:
        from local_index import get_index  # type: ignore
    except Exception as e:
        print(f"ERROR: cannot import local_index ({e}).", file=sys.stderr)
        sys.exit(2)
    try:
        from phase6_ocr_metadata import _classify_doc_type  # type: ignore
    except Exception as e:
        print(f"ERROR: cannot import phase6_ocr_metadata ({e}).", file=sys.stderr)
        sys.exit(2)
    return get_index(), _classify_doc_type


def _diff_summary(old: Dict[str, str], new: Dict[str, str]) -> dict:
    """Compute a structured diff between old and new sidecar maps.

    Returns counts of unchanged, changed, added (new entries), removed.
    Also returns a 'transitions' Counter of (old_value, new_value) pairs.
    """
    unchanged = 0
    changed = 0
    added = 0
    transitions: Counter = Counter()

    all_uris = set(old) | set(new)
    for uri in all_uris:
        o = old.get(uri)
        n = new.get(uri)
        if o is None and n is not None:
            added += 1
            transitions[("(none)", n)] += 1
        elif o is not None and n is None:
            # Should never happen in this script -- we never drop entries.
            transitions[(o, "(none)")] += 1
        elif o == n:
            unchanged += 1
        else:
            changed += 1
            transitions[(o, n)] += 1
    return {
        "unchanged":   unchanged,
        "changed":     changed,
        "added":       added,
        "transitions": transitions,
        "total_old":   len(old),
        "total_new":   len(new),
    }


def main() -> int:
    args = _parse_args()

    print("=" * 70)
    print("doc_type re-classification (current rules -> refreshed sidecar)")
    print("=" * 70)

    print("\nLoading local index + classifier...")
    idx, classify = _load_index_and_classifier()
    files = getattr(idx, "_files", []) or []
    print(f"  Indexed files: {len(files)}")

    # ── Build the new sidecar by re-running the classifier on every file ─
    # System tags are preserved: if the current sidecar entry has a tag
    # in _PRESERVE_TAGS, we keep that tag instead of running the filename
    # classifier. This protects size-based markers like large_pdf_pointer
    # that filename rules can't reproduce.
    print(f"\nRe-classifying with current rules "
          f"(preserving tags: {sorted(_PRESERVE_TAGS)})...")
    old_sidecar_for_preserve: Dict[str, str] = dict(
        getattr(idx, "_doc_type_by_uri", {}) or {}
    )
    new_sidecar: Dict[str, str] = {}
    preserved_counts: Counter = Counter()
    for entry in files:
        if len(entry) != 3:
            continue
        _norm_name, real_name, gs_uri = entry
        if not gs_uri:
            continue
        current_tag = old_sidecar_for_preserve.get(gs_uri)
        if current_tag in _PRESERVE_TAGS:
            # Keep the system-generated tag verbatim.
            new_sidecar[gs_uri] = current_tag
            preserved_counts[current_tag] += 1
        else:
            new_sidecar[gs_uri] = classify(real_name)

    print(f"  Produced {len(new_sidecar)} entries.")
    if preserved_counts:
        print(f"  Preserved system tags:")
        for tag, n in preserved_counts.most_common():
            print(f"    {tag:<28} {n}")

    # ── Diff against the currently-loaded sidecar ─────────────────────────
    old_sidecar: Dict[str, str] = dict(getattr(idx, "_doc_type_by_uri", {}) or {})
    print(f"\nCurrent in-memory sidecar (loaded from GCS at startup): "
          f"{len(old_sidecar)} entries")

    diff = _diff_summary(old_sidecar, new_sidecar)
    print(f"\n  Unchanged: {diff['unchanged']}")
    print(f"  Changed:   {diff['changed']}")
    print(f"  Added (no prior entry): {diff['added']}")

    # ── Distribution comparison ──────────────────────────────────────────
    old_dist = Counter(old_sidecar.values())
    new_dist = Counter(new_sidecar.values())
    print("\n  doc_type distribution before -> after:")
    print(f"    {'doc_type':<28} {'before':>8} {'after':>8} {'delta':>8}")
    print(f"    {'-' * 28} {'-' * 8} {'-' * 8} {'-' * 8}")
    all_types = sorted(set(old_dist) | set(new_dist),
                       key=lambda t: -(new_dist[t] - old_dist[t]) if (new_dist[t] + old_dist[t]) else 0)
    for t in all_types:
        b = old_dist[t]
        a = new_dist[t]
        d = a - b
        sign = "+" if d > 0 else ("" if d == 0 else "")
        print(f"    {t:<28} {b:>8} {a:>8} {sign}{d:>+7}")

    # Files where 'document' was the prior label -- of these, how many
    # moved to a useful classification?
    document_before = {u for u, v in old_sidecar.items() if v == "document"}
    document_to_better = sum(
        1 for u in document_before
        if new_sidecar.get(u, "document") != "document"
    )
    if document_before:
        pct = 100.0 * document_to_better / len(document_before)
        print(f"\n  Of {len(document_before)} files previously tagged 'document':")
        print(f"    {document_to_better} ({pct:.1f}%) now classified as something specific.")

    # ── Top transitions ──────────────────────────────────────────────────
    print(f"\n  Top {args.top_transitions} transitions (before -> after, by count):")
    print(f"    {'before':<28} {'after':<28} {'count':>6}")
    print(f"    {'-' * 28} {'-' * 28} {'-' * 6}")
    for (b, a), n in diff["transitions"].most_common(args.top_transitions):
        print(f"    {b:<28} {a:<28} {n:>6}")

    # ── Always write the local preview ──────────────────────────────────
    out_path = args.output.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(new_sidecar, fh, indent=2)
    print(f"\nWrote refreshed sidecar to: {out_path}")
    print(f"  ({len(new_sidecar)} entries, {out_path.stat().st_size} bytes)")

    # ── Optional GCS upload ──────────────────────────────────────────────
    if args.upload:
        print(f"\n--upload set; uploading to GCS...")
        try:
            from google.cloud import storage  # type: ignore
        except Exception as e:
            print(f"ERROR: google.cloud.storage not available: {e}", file=sys.stderr)
            return 2
        # Bucket name lives on the LocalFileIndex instance. The attribute
        # is `bucket_name` (no underscore prefix); also fall back to env
        # vars so this works even if a future refactor renames it.
        bucket_name = (
            getattr(idx, "bucket_name", None)
            or getattr(idx, "_bucket_name", None)
            or os.environ.get("GCS_BUCKET_NAME")
            or os.environ.get("GCS_BUCKET_RAW")
        )
        if not bucket_name:
            print("ERROR: cannot determine GCS bucket from LocalFileIndex "
                  "or environment (GCS_BUCKET_NAME / GCS_BUCKET_RAW).",
                  file=sys.stderr)
            return 2
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        sidecar_blob = bucket.blob("manifests/doc_type_index.json")
        if args.backup_prev and sidecar_blob.exists():
            backup_path = args.backup_prev.resolve()
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(sidecar_blob.download_as_text())
            print(f"  Backed up previous sidecar -> {backup_path}")
        sidecar_blob.upload_from_string(
            json.dumps(new_sidecar, indent=2),
            content_type="application/json",
        )
        print(f"  Uploaded: gs://{bucket_name}/manifests/doc_type_index.json "
              f"({len(new_sidecar)} entries)")
        print(f"\n  Restart the Flask server for the new sidecar to take effect.")
    else:
        print(f"\n(Dry-run: GCS not modified. Pass --upload to push to production.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
