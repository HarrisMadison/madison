"""
Debug script: inspect the actual GCS paths and structData of docs that
the local_index considers part of the 'Pampinella, Giacomo - Legal' folder.

This tells us what the path layout actually looks like in this bucket
versus what the metadata extractor expects.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from local_index import get_index


def main():
    target_folder = "Pampinella, Giacomo - Legal"
    if len(sys.argv) > 1:
        target_folder = sys.argv[1]

    print(f"Looking for files inside folder: {target_folder!r}\n")

    idx = get_index()

    # The local_index stores files as (norm, real_name, gs_uri). We want to
    # find URIs where the path contains the target folder name.
    matching = []
    for norm, name, uri in idx._files:
        # The URI is gs://bucket/onedrive-mirror/.../FOLDER/.../file.pdf
        # Check if the target folder name appears as a path segment.
        if f"/{target_folder}/" in uri:
            matching.append((name, uri))

    print(f"Found {len(matching)} files in this folder.\n")

    if not matching:
        print("No files matched. Try grepping all paths containing 'pampinella':")
        for norm, name, uri in idx._files:
            if "pampinella" in uri.lower():
                print(f"  {uri}")
        return

    print("Sample paths (first 15):")
    print("-" * 80)
    for name, uri in matching[:15]:
        # Strip gs://bucket/ prefix to make the path layout obvious
        path = uri.split("/", 3)[-1] if "/" in uri[5:] else uri
        print(f"  {path}")
    print("-" * 80)
    print()

    # Decompose the structure: how many path segments before the folder name?
    print("Path-segment analysis:")
    sample_uri = matching[0][1]
    # gs://bucket-name/seg1/seg2/seg3/.../filename
    after_bucket = sample_uri[5:].split("/", 1)[1] if "/" in sample_uri[5:] else ""
    parts = after_bucket.split("/")
    for i, p in enumerate(parts):
        marker = "  <-- TARGET FOLDER" if p == target_folder else ""
        print(f"  parts[{i}] = {p!r}{marker}")

    # What does the metadata extractor expect?
    print()
    print("=" * 80)
    print("Metadata extractor expects layout:")
    print("  <mirror_prefix>/<properties_folder>/<property>/<category>/...")
    print("Where:")
    print("  parts[0] = mirror_prefix      (e.g. 'onedrive-mirror')")
    print("  parts[1] = properties_folder  (e.g. 'Properties')")
    print("  parts[2] = property           (THIS is what gets indexed as `property`)")
    print("  parts[3] = category           (e.g. '04-Permits')")
    print()
    print(f"In your bucket, the {target_folder!r} folder is at parts[?]")


if __name__ == "__main__":
    main()
