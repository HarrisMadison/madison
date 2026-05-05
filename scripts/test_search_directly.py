"""
Direct Vertex search test — bypasses all chat logic.

Runs the simplest possible search with the simplest possible query and
prints the raw response. If this returns 0 results, the Vertex search
index hasn't rebuilt yet from the import. If it returns docs but no
snippets, we have a snippet-shape problem. If it returns docs WITH
snippets, the chat code is dropping results somewhere.
"""
import os, sys, json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from google.cloud import discoveryengine_v1 as de
from google.oauth2 import service_account

SA_KEY = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or str(
    REPO / "Phase3_Bootstrap" / "secrets" / "service-account.json")
SERVING_CONFIG = os.getenv("VERTEX_SERVING_CONFIG", "")

print(f"Serving config: {SERVING_CONFIG}")
print()

creds = service_account.Credentials.from_service_account_file(
    SA_KEY, scopes=["https://www.googleapis.com/auth/cloud-platform"])
client = de.SearchServiceClient(credentials=creds)

QUERIES = ["andover", "9 Andover", "Andover P&L", "claims"]

for query in QUERIES:
    print("=" * 70)
    print(f" QUERY: {query!r}")
    print("=" * 70)

    content_spec = de.SearchRequest.ContentSearchSpec(
        snippet_spec=de.SearchRequest.ContentSearchSpec.SnippetSpec(
            return_snippet=True,
            max_snippet_count=5,
        ),
        extractive_content_spec=de.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
            max_extractive_segment_count=3,
            max_extractive_answer_count=3,
        ),
    )
    req = de.SearchRequest(
        serving_config=SERVING_CONFIG,
        query=query,
        page_size=5,
        content_search_spec=content_spec,
    )

    try:
        resp = client.search(req)
        results = list(resp)
        print(f"  Returned {len(results)} results")

        for i, r in enumerate(results[:3], 1):
            doc = r.document
            sd = dict(doc.struct_data) if doc.struct_data else {}
            title = sd.get("title", "(no title)")
            source_uri = sd.get("source_uri", "(no source_uri)")

            print(f"\n  [{i}] {title}")
            print(f"      doc_id:     {doc.id[:60]}")
            print(f"      source_uri: {source_uri}")

            # Check derived
            derived = dict(doc.derived_struct_data) if doc.derived_struct_data else {}
            snips = derived.get("snippets") or []
            segs = derived.get("extractive_segments") or []
            answers = derived.get("extractive_answers") or []
            print(f"      snippets:            {len(snips)}")
            print(f"      extractive_segments: {len(segs)}")
            print(f"      extractive_answers:  {len(answers)}")

            # Show actual content from first of each
            if snips:
                s = snips[0]
                txt = s.get("snippet") if isinstance(s, dict) else getattr(s, "snippet", "")
                print(f"      first snippet text:  {str(txt)[:200]!r}")
            if segs:
                s = segs[0]
                txt = s.get("content") if isinstance(s, dict) else getattr(s, "content", "")
                print(f"      first segment text:  {str(txt)[:200]!r}")
            if answers:
                s = answers[0]
                txt = s.get("content") if isinstance(s, dict) else getattr(s, "content", "")
                print(f"      first answer text:   {str(txt)[:200]!r}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
    print()

print("=" * 70)
print(" Verdict")
print("=" * 70)
print("""
If Returned=0 for all queries:
  Vertex search index hasn't rebuilt yet. The import imported docs into
  the warehouse, but the search index needs to catch up. Wait 15-30 min
  and try again.

If Returned>0 but all snippet/segment/answer counts are 0:
  Vertex isn't generating extractives for our docs. The rawBytes content
  format we used is text/plain — Vertex may not snippet plaintext. We'll
  need to switch the manifest to point Vertex at the original GCS files
  (passthrough mode) and let Vertex parse them itself.

If Returned>0 with non-zero snippet/segment counts:
  Search and content are fine. The chat code is the problem — likely
  job_context being prepended is making the query too specific.
""")
