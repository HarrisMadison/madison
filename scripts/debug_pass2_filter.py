"""
Debug script: confirm what the `property` field value actually is on
indexed Vertex docs for a given property folder. This tells us whether
the Pass 2 filter `property: ANY("Pampinella, Giacomo - Legal")` will
actually match anything in the index.

Usage:
    python scripts/debug_pass2_filter.py "Pampinella, Giacomo - Legal"

Outputs:
  - Whether a metadata-filtered Vertex search returns any docs
  - The first few hits with their actual property field values
  - The names of docs returned (so we can see if Pass 2 finds the
    folder docs that the regular text search misses)
"""
import os
import sys
from pathlib import Path

# Load env -- same discovery as simple_web.py
REPO_ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    for env in (REPO_ROOT / "Phase3_Bootstrap" / "secrets" / ".env",
                REPO_ROOT / ".env"):
        if env.exists():
            load_dotenv(env)
            print(f"Loaded env from {env}")
            break
except Exception as e:
    print(f"dotenv load error: {e}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/debug_pass2_filter.py "
              "\"Pampinella, Giacomo - Legal\"")
        sys.exit(1)

    folder_name = sys.argv[1]
    print(f"\nTesting Vertex filter for property folder: {folder_name!r}\n")

    from google.cloud import discoveryengine_v1 as discoveryengine
    from google.oauth2 import service_account
    from google.api_core.client_options import ClientOptions

    project_id = os.getenv("GCP_PROJECT_ID", "")
    engine_id = os.getenv("VERTEX_ENGINE_ID", "")
    location = os.getenv("GCP_LOCATION", "global")

    sa_key = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not sa_key or not Path(sa_key).exists():
        sa_key = str(REPO_ROOT / "Phase3_Bootstrap" / "secrets"
                     / "service-account.json")

    print(f"  project: {project_id}")
    print(f"  engine:  {engine_id}")
    print(f"  sa_key:  {sa_key}\n")

    creds = service_account.Credentials.from_service_account_file(
        sa_key, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    client = discoveryengine.SearchServiceClient(
        credentials=creds,
        client_options=ClientOptions(
            api_endpoint=f"{location}-discoveryengine.googleapis.com"
            if location != "global"
            else "discoveryengine.googleapis.com"))
    serving_config = (
        f"projects/{project_id}/locations/{location}/"
        f"collections/default_collection/engines/{engine_id}/"
        f"servingConfigs/default_search")

    safe_folder = folder_name.replace('"', '\\"')
    prop_filter = f'property: ANY("{safe_folder}")'
    print(f"Filter expression: {prop_filter}\n")

    # Run the filtered search with a generic query
    req = discoveryengine.SearchRequest(
        serving_config=serving_config,
        query="*",
        filter=prop_filter,
        page_size=20,
        content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
            snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                return_snippet=False)))

    try:
        resp = client.search(req)
        results = list(resp)
        total = getattr(resp, "total_size", None)
        print(f"Search returned {len(results)} results "
              f"(total_size={total})\n")
    except Exception as e:
        print(f"FILTER REJECTED by Vertex: {e}\n")
        print("This means Pass 2 in retrieve() will fail and silently fall "
              "back to Pass 1 results. The folder name may have characters "
              "Vertex's filter parser cannot handle, or the `property` field "
              "may not be indexed for filtering on this datastore.")
        return

    if not results:
        print(f"FILTER WORKED but matched ZERO docs.\n")
        print(f"This means the docs in folder {folder_name!r} have a "
              f"DIFFERENT value in their `property` structData field, OR "
              f"they were never indexed.\n")
        print("Try the same query without the filter to see if any docs "
              "exist for this folder at all:")
        req2 = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=folder_name,
            page_size=10)
        resp2 = client.search(req2)
        results2 = list(resp2)
        print(f"  Unfiltered text search for {folder_name!r}: {len(results2)} hits")
        for i, r in enumerate(results2[:5], 1):
            sd = dict(r.document.struct_data) if r.document.struct_data else {}
            prop_val = sd.get('property', '<no property field>')
            title = sd.get('title') or sd.get('filename') or r.document.id
            print(f"    [{i}] title={title!r}  property={prop_val!r}")
        return

    print(f"FILTER WORKED -- Pass 2 should be returning these docs:\n")
    for i, r in enumerate(results[:20], 1):
        doc = r.document
        sd = dict(doc.struct_data) if doc.struct_data else {}
        title = sd.get('title') or sd.get('filename') or doc.id
        prop_val = sd.get('property', '<no property field>')
        print(f"  [{i:2d}] title={title!r}")
        print(f"        property={prop_val!r}")


if __name__ == "__main__":
    main()
