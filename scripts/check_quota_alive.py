"""
Single Vertex search call to confirm if 429 cooldown has expired.
ONE call, fail-fast, no retries. Run once a day at most.
"""
import os, sys
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
from google.api_core import exceptions as gapi_exceptions
from google.oauth2 import service_account

SA_KEY = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or str(
    REPO / "Phase3_Bootstrap" / "secrets" / "service-account.json")
SERVING_CONFIG = os.getenv("VERTEX_SERVING_CONFIG", "")

print("Testing Vertex search reachability with ONE call...")
print(f"Serving config: {SERVING_CONFIG[:80]}...")

creds = service_account.Credentials.from_service_account_file(
    SA_KEY, scopes=["https://www.googleapis.com/auth/cloud-platform"])
client = de.SearchServiceClient(credentials=creds)

req = de.SearchRequest(
    serving_config=SERVING_CONFIG,
    query="andover",
    page_size=3,
    content_search_spec=de.SearchRequest.ContentSearchSpec(
        snippet_spec=de.SearchRequest.ContentSearchSpec.SnippetSpec(return_snippet=True),
    ),
)

try:
    resp = client.search(req)
    results = list(resp)
    print()
    print("=" * 60)
    print(f" SUCCESS — Vertex returned {len(results)} results")
    print("=" * 60)
    print("Quota cooldown is OVER. Chat should work now.")
    print()
    if results:
        for i, r in enumerate(results[:3], 1):
            try:
                sd = dict(r.document.struct_data) if r.document.struct_data else {}
            except Exception:
                sd = {}
            title = sd.get("title", "(no title)")
            print(f"  [{i}] {title}")
except gapi_exceptions.ResourceExhausted as e:
    print()
    print("=" * 60)
    print(" 429 STILL ACTIVE — quota window not yet reset")
    print("=" * 60)
    print(f" Error: {str(e)[:200]}")
    print()
    print(" Wait longer before testing the chat. Each test extends the window.")
except Exception as e:
    print()
    print(f"Different error: {type(e).__name__}: {e}")
