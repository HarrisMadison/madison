"""
Debug script: check what property folders the local index discovered, and
whether detect_property_in_query() returns a match for a given query.

This script calls the REAL detect_property_in_query() method on the live
LocalFileIndex -- it does not duplicate the matching logic, so its output
is always in sync with what phase4 retrieval will actually do.

Usage:
    python scripts/debug_property_folders.py
        -> dump first 50 folders + run canned tests

    python scripts/debug_property_folders.py "give me everything on pampinella"
        -> show what the real detect_property_in_query returns for that query
           plus any folders containing the distinctive query words

    python scripts/debug_property_folders.py --grep pampinella
        -> show only folders whose name contains 'pampinella' (case-insensitive)

    python scripts/debug_property_folders.py --dump folders.txt
        -> write the full folder list to a file
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from local_index import get_index, _normalize, _strip_filler


def grep_folders(idx, needle: str):
    """Print only folders containing `needle` (case-insensitive)."""
    folders = sorted(idx.get_property_folders())
    needle_lower = needle.lower()
    matches = [f for f in folders if needle_lower in f.lower()]
    print(f"\nFolders matching {needle!r} (case-insensitive): {len(matches)}")
    print("-" * 60)
    for f in matches:
        print(f"  {f!r}")
    print("-" * 60)
    return matches


def dump_to_file(idx, path: str):
    folders = sorted(idx.get_property_folders())
    Path(path).write_text("\n".join(folders), encoding="utf-8")
    print(f"Wrote {len(folders)} folder names to {path}")


def main():
    args = sys.argv[1:]

    print("Loading local file index (walks the GCS bucket)...")
    idx = get_index()

    folders = sorted(idx.get_property_folders())
    print(f"Discovered {len(folders)} property folders.\n")

    # Subcommands
    if args and args[0] == "--grep" and len(args) >= 2:
        grep_folders(idx, args[1])
        return

    if args and args[0] == "--dump" and len(args) >= 2:
        dump_to_file(idx, args[1])
        return

    if args:
        # Treat as a query string
        query = " ".join(args)

        # Auto-grep for the most distinctive word in the query so the user
        # can see what folders contain it. Useful for debugging when the
        # detection doesn't match what they expected.
        norm_q = _normalize(query)
        core_q = _strip_filler(norm_q) or norm_q
        core_words = [w for w in core_q.split()
                      if len(w) >= 4 or any(c.isdigit() for c in w)]
        if core_words:
            print(f"Distinctive words in query: {core_words}")
            for w in core_words:
                grep_folders(idx, w)

        # Call the REAL method -- this is what phase4 retrieval will see.
        print(f"\n{'=' * 60}")
        print(f"REAL detect_property_in_query() result")
        print(f"{'=' * 60}")
        match = idx.detect_property_in_query(query)
        if match:
            print(f"  query:    {query!r}")
            print(f"  MATCHED:  {match!r}")
            print(f"\n  -> phase4 retrieval will run a Vertex filter:")
            print(f"     property: ANY({match!r})")
        else:
            print(f"  query:    {query!r}")
            print(f"  NO MATCH. phase4 retrieval will skip Pass 2.")
            print(f"\n  Possible reasons:")
            print(f"    1. The folder doesn't exist (check --grep <word>).")
            print(f"    2. Query word is in the noise-word list (e.g. 'photos',")
            print(f"       'permits', 'legal' won't anchor a match).")
            print(f"    3. Query word appears in too many folders (>5) -- not")
            print(f"       distinctive enough to be a strong anchor.")
        return

    # No args: dump first 50 + canned tests
    print("First 50 folders (alphabetical):")
    print("-" * 60)
    for f in folders[:50]:
        print(f"  {f!r}")
    print("-" * 60)
    print(f"... and {len(folders) - 50} more. Use --grep <word> or --dump <file>.\n")

    print("Detection test cases:")
    for q in ["pampinella", "PAMPINELLA", "northridge", "15 northridge",
              "weber", "Yolaine Renard"]:
        match = idx.detect_property_in_query(q)
        print(f"  {q!r:40s} -> {match!r}")


if __name__ == "__main__":
    main()
