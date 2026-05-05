"""Simple Flask web UI for Vertex AI Search + Gemini (Phase 4).

/bob is now the default landing page (always).
/api/status returns live connection info displayed in the /bob status panel.

Env file discovery (first match wins):
  1. $VERTEX_ENV_FILE (explicit override)
  2. <cwd>/Phase3_Bootstrap/secrets/.env
  3. <cwd>/.env
  4. <repo>/Phase3_Bootstrap/secrets/.env
"""
import os
import sys
from pathlib import Path
from flask import Flask, jsonify, render_template_string, request as flask_request
from dotenv import load_dotenv


def discover_env_file() -> Path | None:
    candidates = []
    override = os.environ.get("VERTEX_ENV_FILE")
    if override:
        candidates.append(Path(override))
    cwd = Path.cwd()
    candidates.append(cwd / "Phase3_Bootstrap" / "secrets" / ".env")
    candidates.append(cwd / ".env")
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / "Phase3_Bootstrap" / "secrets" / ".env")
    for c in candidates:
        try:
            if c.exists():
                return c
        except Exception:
            continue
    return None


BOOTSTRAP_ENV = discover_env_file()
if BOOTSTRAP_ENV:
    print(f"  Loading env: {BOOTSTRAP_ENV}")
    load_dotenv(BOOTSTRAP_ENV)
    SA_KEY_PATH = BOOTSTRAP_ENV.parent / "service-account.json"
else:
    print("  No .env found -- set VERTEX_ENV_FILE or cd to your workspace.")
    SA_KEY_PATH = Path(__file__).resolve().parent.parent / "Phase3_Bootstrap" / "secrets" / "service-account.json"

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Register Phase 4 blueprint if available
try:
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    # Repo root MUST be first so 'from vertex.document_fetch import ...'
    # inside phase4 modules can resolve the top-level 'vertex' package.
    sys.path.insert(0, str(_REPO_ROOT))
    sys.path.insert(0, str(_REPO_ROOT / "phase4"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from phase4_routes import phase4_bp
    app.register_blueprint(phase4_bp)
    print("  Phase 4 /bob blueprint loaded.")
except ImportError as e:
    print(f"  Phase 4 blueprint not loaded: {e}")


# ── /api/status ─────────────────────────────────────────────────────────────

# Cache the expensive parts of /api/status so that opening the dark UI,
# clicking Admin, or refreshing the page does NOT burn a Vertex search call
# every time. The status panel updates every 5 minutes — plenty fresh, and
# saves us hundreds of search_requests_regional quota hits per day.
_STATUS_CACHE = {"data": None, "ts": 0}
_STATUS_CACHE_SECONDS = 1800   # 30 minutes — status panel doesn't need fresh


@app.route("/api/status")
def api_status():
    """
    Returns live connection / config info for the /bob status panel.
    Uses a broad Vertex search (page_size=1) to confirm connectivity
    and read response.total_size as the indexed document count.
    Also reads data store metadata (creation time, tier).

    Result is cached for 5 minutes to avoid burning the
    `discoveryengine.googleapis.com/search_requests_regional` quota on
    every page-load / admin-panel-open.
    """
    import time as _time

    # Serve cached result if fresh
    if _STATUS_CACHE["data"] is not None and (_time.time() - _STATUS_CACHE["ts"]) < _STATUS_CACHE_SECONDS:
        cached = dict(_STATUS_CACHE["data"])
        cached["_cached"] = True
        cached["_cache_age_seconds"] = int(_time.time() - _STATUS_CACHE["ts"])
        return jsonify(cached)

    project_id   = os.getenv("GCP_PROJECT_ID", "")
    engine_id    = os.getenv("VERTEX_ENGINE_ID", "")
    data_store   = os.getenv("VERTEX_DATA_STORE_ID", "")
    tier         = os.getenv("VERTEX_TIER", "")
    company      = os.getenv("COMPANY_NAME", "")
    gemini_model = os.getenv("GEMINI_MODEL", "")
    phase4       = os.getenv("PHASE4_ENABLED", "false").lower() == "true"
    serving_cfg  = os.getenv("VERTEX_SERVING_CONFIG", SERVING_CONFIG)

    connectors = {
        "gdrive":   os.getenv("GDRIVE_ENABLED",   "false").lower() == "true",
        "gmail":    os.getenv("GMAIL_ENABLED",    "false").lower() == "true",
        "onedrive": os.getenv("ONEDRIVE_ENABLED", "false").lower() == "true",
    }

    doc_count    = None
    vertex_ok    = False
    vertex_error = ""
    ds_created   = ""
    ds_doc_count = None

    try:
        from google.cloud import discoveryengine_v1 as discoveryengine
        from google.oauth2 import service_account as gsa
        from google.api_core.client_options import ClientOptions

        location = os.getenv("GCP_LOCATION", "global")
        sa_key   = str(SA_KEY_PATH)

        creds = None
        if Path(sa_key).exists():
            creds = gsa.Credentials.from_service_account_file(
                sa_key,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        client_opts = ClientOptions(api_endpoint="discoveryengine.googleapis.com")

        # ── 1. Broad search ping -- gets total_size (indexed doc count) ──────
        search_client = discoveryengine.SearchServiceClient(
            credentials=creds, client_options=client_opts
        )

        search_req = discoveryengine.SearchRequest(
            serving_config=serving_cfg,
            query="",        # empty = match all indexed docs
            page_size=1,     # we only need the total_size, not real results
        )
        search_resp = search_client.search(search_req)
        # Consume first page so total_size is populated
        _ = list(search_resp.pages)[0]
        ds_doc_count = getattr(search_resp, "total_size", None)
        vertex_ok = True

        # ── 2. Data store metadata (creation time) ───────────────────────────
        try:
            project_num = os.getenv("GCP_PROJECT_NUMBER") or project_id
            ds_client = discoveryengine.DataStoreServiceClient(
                credentials=creds, client_options=client_opts
            )
            ds_name = (
                f"projects/{project_num}/locations/{location}"
                f"/collections/default_collection/dataStores/{data_store}"
            )
            ds_info = ds_client.get_data_store(name=ds_name)
            if hasattr(ds_info, "create_time") and ds_info.create_time:
                ds_created = ds_info.create_time.strftime("%Y-%m-%d")
        except Exception:
            pass  # metadata is bonus info, don't fail the whole call

    except Exception as ex:
        vertex_error = str(ex)[:140]

    payload = {
        "company":        company,
        "project_id":     project_id,
        "engine_id":      engine_id,
        "data_store_id":  data_store,
        "tier":           tier,
        "gemini_model":   gemini_model,
        "phase4_enabled": phase4,
        "connectors":     connectors,
        "vertex_ok":      vertex_ok,
        "vertex_error":   vertex_error,
        "doc_count":      ds_doc_count,   # integer from total_size
        "ds_created":     ds_created,     # "YYYY-MM-DD" or ""
    }

    # Cache the result so the next 5 minutes of page-loads / admin-opens
    # do NOT each fire a Vertex search.
    _STATUS_CACHE["data"] = payload
    _STATUS_CACHE["ts"]   = _time.time()

    return jsonify(payload)


# ── Root redirect → /bob ─────────────────────────────────────────────────────

@app.route("/")
def index_redirect():
    """Always redirect / to /bob (the Gemini-powered chat UI)."""
    from flask import redirect
    return redirect("/bob")


# ── Legacy /api/query (Phase 3 plain search) ─────────────────────────────────

PROJECT_ID     = os.getenv("GCP_PROJECT_ID", "")
ENGINE_ID      = os.getenv("VERTEX_ENGINE_ID", "")
LOCATION       = os.getenv("GCP_LOCATION", "global")
SERVING_CONFIG = os.getenv("VERTEX_SERVING_CONFIG", "")


def _safe_struct_get(struct_data, key: str, default=""):
    try:
        if hasattr(struct_data, "get"):
            return struct_data.get(key) or default
        if hasattr(struct_data, "fields"):
            v = struct_data.fields.get(key)
            if v is None:
                return default
            kind = v.WhichOneof("kind")
            if kind == "string_value":
                return v.string_value
            if kind == "number_value":
                return str(v.number_value)
            return default
    except Exception:
        return default
    return default


def is_empty_answer(text: str) -> bool:
    if not text:
        return True
    lower = text.lower().strip()
    empties = [
        "i don't have", "i do not have", "no information",
        "not found", "no relevant", "cannot find",
        "couldn't find", "no results",
    ]
    return any(e in lower for e in empties)


def friendly_empty_message(query: str) -> str:
    return (
        f"I searched the indexed documents but couldn't find specific information "
        f"about \"{query}\". Try rephrasing, or check that the relevant documents "
        f"have been synced and indexed."
    )



# ── /api/download  (GCS file proxy) ─────────────────────────────────────────

@app.route("/api/download")
def api_download():
    """
    Stream a file from GCS to the browser as a download.
    Query param: ?uri=gs://bucket/path/to/file.pdf
    Uses the service account already loaded by the app.
    """
    from flask import Response, stream_with_context
    import urllib.parse

    raw_uri = flask_request.args.get("uri", "").strip()
    if not raw_uri:
        return jsonify({"error": "uri param required"}), 400

    # Only allow gs:// URIs (no SSRF via http:// etc.)
    if not raw_uri.startswith("gs://"):
        return jsonify({"error": "Only gs:// URIs are supported"}), 400

    try:
        from google.cloud import storage as gcs_storage
        from google.oauth2 import service_account as gsa

        creds = None
        if SA_KEY_PATH.exists():
            creds = gsa.Credentials.from_service_account_file(
                str(SA_KEY_PATH),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        # Parse  gs://bucket/blob/path
        without_scheme = raw_uri[5:]          # strip "gs://"
        bucket_name, _, blob_path = without_scheme.partition("/")
        gcs_client  = gcs_storage.Client(credentials=creds)
        bucket      = gcs_client.bucket(bucket_name)
        blob        = bucket.blob(blob_path)

        filename = urllib.parse.quote(blob_path.split("/")[-1])
        content_type = "application/octet-stream"
        name_lower   = filename.lower()
        if name_lower.endswith(".pdf"):
            content_type = "application/pdf"
        elif name_lower.endswith(".docx"):
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif name_lower.endswith(".xlsx"):
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        def generate():
            with blob.open("rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)  # 256 KB chunks
                    if not chunk:
                        break
                    yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type":        content_type,
        }
        return Response(stream_with_context(generate()), headers=headers, status=200)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/query", methods=["POST"])
def api_query():
    data  = flask_request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    try:
        from google.cloud import discoveryengine_v1 as discoveryengine
        from google.oauth2 import service_account as gsa
        from google.api_core.client_options import ClientOptions

        creds = None
        if SA_KEY_PATH.exists():
            creds = gsa.Credentials.from_service_account_file(
                str(SA_KEY_PATH),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        client_opts = ClientOptions(api_endpoint="discoveryengine.googleapis.com")
        client = discoveryengine.SearchServiceClient(
            credentials=creds, client_options=client_opts
        )

        content_spec = discoveryengine.SearchRequest.ContentSearchSpec(
            snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                return_snippet=True,
            ),
            # NO summary_spec — that field requires the
            # discoveryengine.googleapis.com/llm_requests quota, which is
            # tightly capped on free-tier projects (frequent 429s). Snippets
            # are enough for the UI; Gemini does the synthesis elsewhere.
        )

        req = discoveryengine.SearchRequest(
            serving_config=SERVING_CONFIG,
            query=query,
            page_size=10,
            content_search_spec=content_spec,
        )

        response = client.search(req)
        results  = list(response)

        answer_text = ""
        if hasattr(response, "summary") and response.summary:
            answer_text = response.summary.summary_text or ""

        sources = []
        for result in results:
            doc   = result.document
            title = _safe_struct_get(doc.struct_data, "title", "Document")
            uri   = _safe_struct_get(doc.struct_data, "uri", "")
            if title == "Document":
                title = _safe_struct_get(doc.derived_struct_data, "title", "Document")
            sources.append({"title": title, "uri": uri})

        seen, unique = set(), []
        for s in sources:
            k = s.get("title")
            if k and k not in seen:
                seen.add(k)
                unique.append(s)

        empty = is_empty_answer(answer_text) or (not unique and not answer_text)
        if empty:
            answer_text = friendly_empty_message(query)

        return jsonify({
            "text":    answer_text or friendly_empty_message(query),
            "sources": unique[:5],
            "empty":   empty,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Startup ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    from threading import Timer

    port = int(os.environ.get("PORT", 5000))
    url  = f"http://localhost:{port}"

    print(f"\n  HarrisPepe RAG Platform")
    print(f"  Company : {os.getenv('COMPANY_NAME', '(not set)')}")
    print(f"  Project : {os.getenv('GCP_PROJECT_ID', '(not set)')}")
    print(f"  Engine  : {os.getenv('VERTEX_ENGINE_ID', '(not set)')}")
    print(f"  Gemini  : {os.getenv('GEMINI_MODEL', 'disabled')}")
    print(f"  UI      : {url}/bob\n")

    # Always open /bob -- it is the default landing page
    Timer(1.5, lambda: webbrowser.open(f"{url}/bob")).start()
    app.run(host="0.0.0.0", port=port, debug=False)