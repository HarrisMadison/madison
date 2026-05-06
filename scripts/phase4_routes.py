import os, io
from pathlib import Path
from flask import Blueprint, request, jsonify, render_template, current_app, Response
from dataclasses import asdict

try:
    from job_intelligence import get_intelligence
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from job_intelligence import get_intelligence

phase4_bp = Blueprint("phase4", __name__)
GDRIVE_FOLDER = os.getenv("GDRIVE_FOLDER_IDS", "17oev3CKYBXn4wOuK2K0DxXe5z1v7C0kz")
_FILE_CACHE = {}

def _sa_key():
    # Mirror the discovery order used elsewhere in the codebase (local_index,
    # job_intelligence) so this route works regardless of how the user
    # launched simple_web. Order:
    #   1. GOOGLE_APPLICATION_CREDENTIALS env var (if set and file exists)
    #   2. <repo>/Phase3_Bootstrap/secrets/service-account.json   <-- bootstrap default
    #   3. <repo>/service-account.json                            <-- legacy
    #   4. <scripts>/service-account.json                         <-- legacy
    repo_root = Path(__file__).resolve().parent.parent
    candidates = []
    env_key = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if env_key:
        candidates.append(Path(env_key))
    candidates.extend([
        repo_root / "Phase3_Bootstrap" / "secrets" / "service-account.json",
        repo_root / "service-account.json",
        Path(__file__).resolve().parent / "service-account.json",
    ])
    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except Exception:
            continue
    return None

def _drive_service():
    from google.oauth2 import service_account as sa_mod
    from googleapiclient.discovery import build
    sa = _sa_key()
    if not sa:
        raise RuntimeError("service-account.json not found")
    creds = sa_mod.Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _build_cache():
    global _FILE_CACHE
    try:
        svc = _drive_service()
        results = []
        page_token = None
        while True:
            params = dict(
                q="'{}' in parents and trashed=false".format(GDRIVE_FOLDER),
                fields="nextPageToken,files(id,name,mimeType)",
                pageSize=200)
            if page_token:
                params["pageToken"] = page_token
            resp = svc.files().list(**params).execute()
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        _FILE_CACHE = {}
        for f in results:
            _FILE_CACHE[f["name"].lower()] = f
            stem = Path(f["name"]).stem.lower()
            if stem not in _FILE_CACHE:
                _FILE_CACHE[stem] = f
        print("[Drive] {} files cached".format(len(results)))
    except Exception as e:
        print("[Drive] cache error: {}".format(e))

def _find_file(title):
    if not _FILE_CACHE:
        _build_cache()
    t = title.lower().strip()
    if t in _FILE_CACHE:
        return _FILE_CACHE[t]
    stem = Path(t).stem
    if stem in _FILE_CACHE:
        return _FILE_CACHE[stem]
    for k, v in _FILE_CACHE.items():
        if t in k or k in t:
            return v
    return None

@phase4_bp.route("/api/chat/new", methods=["POST"])
def new_chat_session():
    try:
        return jsonify({"session_id": get_intelligence().new_session(), "status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@phase4_bp.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    session_id = data.get("session_id")
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        intel = get_intelligence()
        resp = intel.chat(query, session_id=session_id)
        result = asdict(resp)
        if not session_id and intel._sessions:
            latest = max(intel._sessions.values(), key=lambda s: s.last_active)
            session_id = latest.session_id
        result["session_id"] = session_id
        return jsonify(result)
    except Exception as e:
        current_app.logger.error("[chat] {}".format(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@phase4_bp.route("/api/chat/<session_id>", methods=["GET"])
def get_session_history(session_id):
    intel = get_intelligence()
    sess = intel.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found or expired"}), 404
    return jsonify({"session_id": session_id, "job_context": sess.job_context,
                    "history": [{"role": m.role, "text": m.text} for m in sess.history]})

@phase4_bp.route("/api/chat/<session_id>", methods=["DELETE"])
def clear_session(session_id):
    get_intelligence().clear_session(session_id)
    return jsonify({"status": "cleared"})

@phase4_bp.route("/api/debug/sources")
def debug_sources():
    q = request.args.get("q", "northridge")
    intel = get_intelligence()
    try:
        summary, sources = intel._vertex_search(q)
        for s in sources:
            f = _find_file(s.get("title", ""))
            s["drive_match"] = f["name"] if f else None
            s["drive_id"] = f["id"] if f else None
        return jsonify({"query": q, "sources": sources, "cache_size": len(_FILE_CACHE)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@phase4_bp.route("/api/drive/files")
def list_drive_files():
    _build_cache()
    return jsonify({"files": len(_FILE_CACHE), "titles": sorted(_FILE_CACHE.keys())})

@phase4_bp.route("/api/download")
def download_doc():
    """Download a document. Accepts either:
      ?uri=gs://bucket/path/to/file.pdf   — stream directly from GCS (preferred,
                                            works with the local-index path
                                            that bypasses Vertex/Drive)
      ?title=<filename>                   — look up in Google Drive cache
      ?id=<drive_file_id>                 — fetch directly from Drive by ID
    The gs:// path is the one chips use now because the local-index returns
    GCS URIs, not Drive file IDs. Drive lookup is kept as a fallback.
    """
    gs_uri  = request.args.get("uri", "").strip()
    title   = request.args.get("title", "").strip()
    file_id = request.args.get("id", "").strip()

    # ── Path 1: gs:// URI — stream directly from GCS ───────────────────
    if gs_uri.startswith("gs://"):
        try:
            from google.cloud import storage as gcs_storage
            from google.oauth2 import service_account as sa_mod
            sa = _sa_key()
            if not sa:
                return jsonify({"error": "service-account.json not found"}), 500
            creds = sa_mod.Credentials.from_service_account_file(
                sa, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            client = gcs_storage.Client(credentials=creds)

            parts = gs_uri[5:].split("/", 1)
            if len(parts) != 2:
                return jsonify({"error": "malformed gs:// URI"}), 400
            bucket_name, blob_name = parts
            blob = client.bucket(bucket_name).blob(blob_name)
            if not blob.exists():
                return jsonify({"error": "GCS object not found: {}".format(gs_uri)}), 404

            # Reload metadata so content_type is populated. Without this the
            # client-side download often gets application/octet-stream and
            # the browser doesn't know how to preview it.
            blob.reload()
            data = blob.download_as_bytes()
            fname = Path(blob_name).name or "download"
            mime = blob.content_type or "application/octet-stream"
            return Response(data, headers={
                "Content-Disposition": "attachment; filename=\"{}\"".format(fname),
                "Content-Type": mime,
            })
        except Exception as e:
            current_app.logger.error("[download gs] {}".format(e), exc_info=True)
            return jsonify({"error": "GCS download failed: {}".format(e)}), 500

    # ── Path 2: Drive title/id lookup (legacy) ──────────────────────────
    if not title and not file_id:
        return jsonify({"error": "uri, title, or id required"}), 400
    fname = title or file_id
    mime = "application/octet-stream"
    if not file_id:
        f = _find_file(title)
        if not f:
            _build_cache()
            f = _find_file(title)
        if not f:
            return jsonify({"error": "No Drive file found matching: {}".format(title)}), 404
        file_id = f["id"]
        fname = f["name"]
        mime = f["mimeType"]
    try:
        from googleapiclient.http import MediaIoBaseDownload
        svc = _drive_service()
        if not title:
            meta = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
            fname = meta.get("name", fname)
            mime = meta.get("mimeType", mime)
        EXPORT = {
            "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
            "application/vnd.google-apps.spreadsheet":
                ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
            "application/vnd.google-apps.presentation":
                ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
        }
        buf = io.BytesIO()
        if mime in EXPORT:
            export_mime, ext = EXPORT[mime]
            req = svc.files().export_media(fileId=file_id, mimeType=export_mime)
            if not fname.endswith(ext):
                fname += ext
            out_mime = export_mime
        else:
            req = svc.files().get_media(fileId=file_id)
            out_mime = mime
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        buf.seek(0)
        return Response(buf.read(), headers={
            "Content-Disposition": "attachment; filename=\"{}\"".format(fname),
            "Content-Type": out_mime})
    except Exception as e:
        current_app.logger.error("[download] {}".format(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

@phase4_bp.route("/api/admin/reload-index", methods=["POST", "GET"])
def reload_local_index():
    """Force-reload the in-memory filename index from GCS.
    Call after running onedrive_sync.py so new files become searchable
    via the local-index path without restarting simple_web."""
    try:
        from local_index import reload_index
        result = reload_index()
        status = 200 if result.get("ok") else 500
        return jsonify(result), status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@phase4_bp.route("/bob")
def bob_dashboard():
    try:
        return render_template("bob_chat.html")
    except Exception:
        p = Path(__file__).parent / "bob_chat.html"
        if p.exists():
            return p.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html"}
        return "<h2>bob_chat.html not found</h2>", 404