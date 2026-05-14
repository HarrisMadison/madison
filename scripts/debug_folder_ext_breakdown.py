"""Quick read-only count: how many files of each extension live under
each test folder? Helps confirm whether the 0-file enumeration is a
normalization bug or simply an extension-filter exclusion.
"""
from __future__ import annotations
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from local_index import get_index, _normalize  # type: ignore

FOLDERS = [
    "916950_Labon - claim paid & closed",
    "Michelle Berry -toilet overflow google lead - 198-19 118th Ave",
    "Trish Wallace (dad) Albert - Yelp, Lead Mold & Water - 2 Harvard Pl",
    "Chris Simon Reco Phil Trustfi - 24 Laurie Blvd",
    "27 Manor Drive",
]

idx = get_index()
files = getattr(idx, "_files", []) or []
print(f"Index has {len(files)} files")
print()
print(f"{'folder':<60} {'files':>6}  extension breakdown")
print(f"{'-' * 60} {'-' * 6}  {'-' * 40}")

for folder in FOLDERS:
    folder_norm = _normalize(folder)
    ext_counter: Counter = Counter()
    sample_names: list[str] = []
    for entry in files:
        if len(entry) != 3:
            continue
        _norm_name, real_name, gs_uri = entry
        if not gs_uri.startswith("gs://"):
            continue
        rest = gs_uri[5:].split("/", 1)
        if len(rest) != 2:
            continue
        path = rest[1]
        segments = path.split("/")
        for seg in segments[:-1]:
            if _normalize(seg) == folder_norm:
                ext = Path(real_name).suffix.lower() or "(no-ext)"
                ext_counter[ext] += 1
                if len(sample_names) < 3:
                    sample_names.append(real_name)
                break

    total = sum(ext_counter.values())
    breakdown = ", ".join(f"{e} x{c}" for e, c in ext_counter.most_common())
    folder_display = folder if len(folder) <= 58 else folder[:55] + "..."
    print(f"{folder_display:<60} {total:>6}  {breakdown}")
    for n in sample_names:
        print(f"{'':<60} {'':<6}    ex: {n}")
