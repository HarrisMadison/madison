"""
Phase 4C: Flask Blueprint — Chat Routes
=========================================
Drop-in routes for app.py.

HOW TO INTEGRATE INTO app.py
─────────────────────────────
1. Copy this file to the same folder as app.py  (web/ or root)
2. Add these two lines to app.py after the Flask app is created:

       from phase4_routes import phase4_bp
       app.register_blueprint(phase4_bp)

3. Ensure job_intelligence.py is importable from the same directory
   (or adjust the import path below).

NEW ENDPOINTS:
  POST /api/chat/new          → create session, returns {session_id}
  POST /api/chat              → send a message, returns IntelligenceResponse
  GET  /api/chat/<session_id> → get session history
  DELETE /api/chat/<session_id> → clear session history
  GET  /chat                  → serve the chat UI

REQUIRED ENV:
  GEMINI_API_KEY  (or service-account.json for Vertex-hosted Gemini)
"""

from flask import Blueprint, request, jsonify, render_template, current_app
from dataclasses import asdict

# Import the intelligence engine
# Adjust the import if job_intelligence.py is in a sub-folder
try:
    from job_intelligence import get_intelligence
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from job_intelligence import get_intelligence

phase4_bp = Blueprint("phase4", __name__)


# ── POST /api/chat/new ─────────────────────────────────────────────────────
@phase4_bp.route("/api/chat/new", methods=["POST"])
def new_chat_session():
    """
    Create a new conversation session.
    Returns: {"session_id": "..."}
    """
    try:
        intel = get_intelligence()
        sid   = intel.new_session()
        return jsonify({"session_id": sid, "status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── POST /api/chat ─────────────────────────────────────────────────────────
@phase4_bp.route("/api/chat", methods=["POST"])
def chat():
    """
    Send a message and receive an answer.

    Request JSON:
      {
        "query":      "Which Brooklyn jobs are still open?",  // required
        "session_id": "abc-123"                               // optional
      }

    Response JSON:
      {
        "answer":              "...",
        "sources":             ["doc1.pdf", "doc2.pdf"],
        "search_results":      7,
        "confidence":          "high",        // high | medium | low | none
        "job_context":         "332 Parkville Ave",
        "suggested_followups": ["...", "..."],
        "session_id":          "abc-123"
      }
    """
    data = request.get_json(silent=True) or {}
    query      = (data.get("query") or "").strip()
    session_id = data.get("session_id")

    if not query:
        return jsonify({"error": "query is required"}), 400

    try:
        intel    = get_intelligence()
        response = intel.chat(query, session_id=session_id)

        result = asdict(response)

        # Resolve the active session_id (may have been created during the call)
        if not session_id:
            sessions = intel._sessions
            if sessions:
                latest = max(sessions.values(), key=lambda s: s.last_active)
                session_id = latest.session_id
        result["session_id"] = session_id

        # Ensure media_links is always present (empty list for older responses)
        result.setdefault("media_links", [])
        # source_uris: {filename: gs://... uri} for download chips
        result.setdefault("source_uris", result.pop("_source_uris", {}))

        return jsonify(result)
    except Exception as e:
        current_app.logger.error(f"[phase4/chat] {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── GET /api/chat/<session_id> ─────────────────────────────────────────────
@phase4_bp.route("/api/chat/<session_id>", methods=["GET"])
def get_session_history(session_id: str):
    """
    Return the conversation history for a session.
    Useful for the UI to restore an in-progress conversation.
    """
    intel   = get_intelligence()
    session = intel.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found or expired"}), 404

    history = [
        {"role": msg.role, "text": msg.text, "timestamp": msg.timestamp}
        for msg in session.history
    ]
    return jsonify({
        "session_id":  session_id,
        "job_context": session.job_context,
        "history":     history,
    })


# ── DELETE /api/chat/<session_id> ──────────────────────────────────────────
@phase4_bp.route("/api/chat/<session_id>", methods=["DELETE"])
def clear_session(session_id: str):
    """Clear conversation history without deleting the session."""
    intel = get_intelligence()
    intel.clear_session(session_id)
    return jsonify({"status": "cleared", "session_id": session_id})


# ── GET /chat (canonical) and /bob (legacy alias) ─────────────────────────────
@phase4_bp.route("/chat")
@phase4_bp.route("/bob")  # legacy alias — keeps existing bookmarks working
def chat_dashboard():
    """
    Serve the chat UI.
    Looks for the chat template in Flask's templates folder.
    """
    try:
        import os as _os
        company = _os.getenv("COMPANY_NAME", "").strip()
        gemini  = _os.getenv("GEMINI_MODEL", "Gemini").replace("models/", "").replace(r"-\d{8}$", "")
        # Split company name for amber accent on last word if business type
        biz_words = {"construction","realty","realtors","group","llc","inc","corp","properties"}
        parts = company.split()
        if len(parts) > 1 and parts[-1].lower() in biz_words:
            company_main = " ".join(parts[:-1])
            company_accent = parts[-1]
        else:
            company_main   = company
            company_accent = ""
        return render_template(
            "bob_chat.html",
            company=company,
            company_main=company_main,
            company_accent=company_accent,
            gemini_model=gemini,
        )
    except Exception:
        # If template isn't found, serve the file directly from phase4 folder
        import os
        html_path = os.path.join(os.path.dirname(__file__), "bob_chat.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html"}
        return (
            "<h2>Chat UI not found.</h2>"
            "<p>Copy the chat template to your Flask templates folder.</p>",
            404,
        )
