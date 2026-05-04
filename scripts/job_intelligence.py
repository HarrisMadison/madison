"""
Phase 4 Job Intelligence — Optimized Architecture
- Vertex AI Search: RETRIEVAL ONLY (no LLM summarization, saves quota)
- Gemini: ALL synthesis and answering (cheap, fast, better)
- Smart caching: Don't re-search unnecessarily
"""
import os, re, uuid, time
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from google.cloud import discoveryengine_v1 as discoveryengine
from google.oauth2 import service_account
import google.auth
import google.generativeai as genai

SERVING_CONFIG = os.getenv("VERTEX_SERVING_CONFIG", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
SESSION_TTL    = 3600
MAX_HISTORY    = 6
CONTEXT_CACHE_MINUTES = 15  # Reuse search results for this long

SYSTEM_PROMPT = """You are a document intelligence assistant.
You help the user find information in their indexed documents about jobs, properties, permits, loans, appraisals, and claims.
You will receive search results from the document index plus conversation history.

RULES:
1. Answer directly and conversationally - no fluff
2. Cite specific documents when making claims
3. If search results are empty or irrelevant, say so clearly
4. Highlight key numbers, dates, and names
5. Keep responses under 250 words unless asked for more detail
6. When unsure, acknowledge it - don't make up information"""

def _load_creds():
    for key in [
        Path(__file__).resolve().parent.parent / "service-account.json",
        Path(__file__).resolve().parent / "service-account.json",
    ]:
        if key.exists():
            return service_account.Credentials.from_service_account_file(
                str(key), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return creds

def _safe_struct_get(struct, key, default=""):
    if struct is None: return default
    try:
        if hasattr(struct,"get"): val = struct.get(key, default)
        else: val = struct[key] if key in struct else default
        return str(val or default)
    except: return default

NO_RESULT = ("no results could be found","try rephrasing","i could not find","summary could not be generated")

def _is_empty(text):
    if not text: return True
    return any(m in text.lower() for m in NO_RESULT)

@dataclass
class ChatMessage:
    role: str
    text: str
    timestamp: float = field(default_factory=time.time)

@dataclass
class ChatSession:
    session_id: str
    history: List[ChatMessage] = field(default_factory=list)
    job_context: Optional[str] = None
    last_active: float = field(default_factory=time.time)
    last_search_query: Optional[str] = None
    last_search_time: float = 0
    cached_sources: List[Dict] = field(default_factory=list)

@dataclass
class IntelligenceResponse:
    answer: str
    sources: List[Dict]
    search_results: int
    confidence: str
    job_context: Optional[str]
    suggested_followups: List[str]

def _extract_job_context(text):
    # Extract property addresses or job identifiers
    m = re.search(r"\d{1,5}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}(?:\s+(?:Ave|Blvd|St|Rd|Dr|Ln|Way|Ct|Pl)\.?)?", text)
    return m.group(0).strip() if m else None

def _score(n):
    if n == 0: return "none"
    if n >= 5: return "high"
    if n >= 2: return "medium"
    return "low"

def _followups(query, ctx):
    q = query.lower()
    s = []
    if any(w in q for w in ["loan","draw","balance","payment"]): 
        s += ["Any other draws?","Current loan balance?"]
    elif any(w in q for w in ["permit","inspection","certificate"]): 
        s += ["When does the permit expire?","Are there any failed inspections?"]
    elif any(w in q for w in ["apprais","value","comparable"]): 
        s += ["Comparable sales used?","Site value?"]
    elif any(w in q for w in ["claim","insurance","adjuster"]): 
        s += ["Who is the insurer?","Approved scope amount?"]
    elif any(w in q for w in ["owner","lender","contact"]): 
        s += ["Permits for this owner?","Who is the lender?"]
    else: 
        s += ["Any permits on file?","What documents exist for this job?"]
    if ctx: s.append(f"More on {ctx}?")
    return s[:3]

class JobIntelligence:
    def __init__(self):
        self._creds = _load_creds()
        self._sessions = {}
        self._use_gemini = False
        
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            try:
                genai.configure(api_key=api_key)
                self._gemini = genai.GenerativeModel(
                    model_name=GEMINI_MODEL, 
                    system_instruction=SYSTEM_PROMPT)
                self._use_gemini = True
                print(f"[Phase4] Gemini synthesis ON ({GEMINI_MODEL})")
            except Exception as e:
                print(f"[Phase4] Gemini init failed: {e}")
        else:
            print("[Phase4] No GEMINI_API_KEY — direct search results only")

    def new_session(self):
        sid = str(uuid.uuid4())
        self._sessions[sid] = ChatSession(session_id=sid)
        return sid

    def get_session(self, sid):
        s = self._sessions.get(sid)
        if s and time.time() - s.last_active > SESSION_TTL:
            del self._sessions[sid]
            return None
        return s

    def _vertex_search(self, query: str) -> tuple[List[Dict], int]:
        """
        Vertex AI Search: RETRIEVAL ONLY
        - No LLM summarization (saves quota)
        - Returns raw document snippets
        - Fast and cheap
        """
        client = discoveryengine.SearchServiceClient(credentials=self._creds)

        # Ask Vertex for both snippets AND extractive segments. Some doc types
        # (short single-page invoices, scanned-text PDFs, .docx with tables)
        # only return content under one of these, not both. We then read
        # whichever fields actually have text.
        content_spec = discoveryengine.SearchRequest.ContentSearchSpec(
            snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                return_snippet=True,
                max_snippet_count=5,
            ),
            extractive_content_spec=discoveryengine.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
                max_extractive_segment_count=3,
                max_extractive_answer_count=3,
            ),
        )

        req = discoveryengine.SearchRequest(
            serving_config=SERVING_CONFIG,
            query=query,
            page_size=10,
            content_search_spec=content_spec,
        )

        response = client.search(req)
        results = list(response)  # Materialize

        sources = []
        seen = set()
        for r in results:
            doc = r.document
            title = _safe_struct_get(doc.struct_data, "title", "")
            uri   = _safe_struct_get(doc.struct_data, "uri", "")

            # Extract content text robustly. derived_struct_data is a proto
            # MapComposite that behaves like a dict but doesn't expose nested
            # fields as Python attributes. Walk it as a dict.
            text_parts = []
            try:
                derived = doc.derived_struct_data
                if derived:
                    # Convert to plain dict so .get() works regardless of proto type
                    d = dict(derived) if not isinstance(derived, dict) else derived

                    # 1) snippets[]: each item has 'snippet' (HTML) and 'snippet_status'
                    for snip in (d.get("snippets") or []):
                        s = snip.get("snippet") if isinstance(snip, dict) else getattr(snip, "snippet", "")
                        if s:
                            text_parts.append(re.sub(r"<[^>]+>", "", str(s)))

                    # 2) extractive_segments[]: longer chunks of doc text
                    for seg in (d.get("extractive_segments") or []):
                        s = seg.get("content") if isinstance(seg, dict) else getattr(seg, "content", "")
                        if s:
                            text_parts.append(str(s))

                    # 3) extractive_answers[]: short answers extracted from doc
                    for ans in (d.get("extractive_answers") or []):
                        s = ans.get("content") if isinstance(ans, dict) else getattr(ans, "content", "")
                        if s:
                            text_parts.append(str(s))
            except Exception as e:
                print(f"[Vertex] snippet extract error: {e}")

            snippet_text = "\n".join(text_parts).strip()

            if not title:
                title = _safe_struct_get(doc.derived_struct_data, "title", "")

            label = title or (Path(uri).name if uri else doc.id or "Document")

            if label and label not in seen:
                seen.add(label)
                sources.append({
                    "title": label,
                    "uri": uri,
                    "snippet": snippet_text[:3000],  # 3000-char window per doc
                })

        return sources[:10], len(sources)

    def chat(self, query: str, session_id: Optional[str] = None) -> IntelligenceResponse:
        # Get or create session
        session = self.get_session(session_id) if session_id else None
        if not session:
            sid = self.new_session()
            session = self._sessions[sid]

        # Extract job context from query
        detected = _extract_job_context(query)
        if detected: 
            session.job_context = detected

        # Build search query with context
        full_query = f"{session.job_context} {query}" if session.job_context else query

        # SMART CACHING: Reuse recent search if same context
        now = time.time()
        cache_valid = (
            session.last_search_query == full_query and 
            (now - session.last_search_time) < (CONTEXT_CACHE_MINUTES * 60) and
            session.cached_sources
        )
        
        if cache_valid:
            print(f"[Cache] Reusing search results from {int(now - session.last_search_time)}s ago")
            sources = session.cached_sources
            num_results = len(sources)
        else:
            # NEW SEARCH: Vertex retrieval only
            try:
                sources, num_results = self._vertex_search(full_query)
                session.last_search_query = full_query
                session.last_search_time = now
                session.cached_sources = sources
            except Exception as e:
                print(f"[Vertex] {e}")
                sources, num_results = [], 0

        # Build answer
        if not sources:
            hint = f" (focused on: {session.job_context})" if session.job_context else ""
            answer = (f"No documents found{hint}. Try a specific address, "
                      f"permit number, loan number, dollar amount, or document name.")
        elif self._use_gemini:
            # GEMINI SYNTHESIS: Using retrieved context
            # Fold conversation history into the prompt as plain text rather
            # than using start_chat(history=...). The chat-history API is
            # strict about message ordering and frequently 400s; inlining is
            # bulletproof and works with every Gemini model version.
            history_lines = []
            for m in session.history[-(MAX_HISTORY*2):]:
                speaker = "User" if m.role == "user" else "Assistant"
                history_lines.append(f"{speaker}: {m.text}")
            history_block = "\n".join(history_lines) if history_lines else "(no prior conversation)"

            context_text = "\n\n".join([
                f"**{s['title']}**\n{s['snippet']}"
                for s in sources[:8]
            ])

            hint = f"\n[Job in focus: {session.job_context}]" if session.job_context else ""
            src_list = ", ".join(s["title"] for s in sources[:8])

            prompt = (
                f"Conversation so far:\n{history_block}\n\n"
                f"Documents found: {src_list}{hint}\n\n"
                f"Document excerpts:\n{context_text}\n\n"
                f"User's question: {query}\n\n"
                f"Answer based ONLY on the document excerpts above. "
                f"Cite specific documents. If the excerpts don't answer the question, say so."
            )

            try:
                resp = self._gemini.generate_content(prompt)
                answer = (resp.text or "").strip()
                if not answer:
                    answer = (
                        f"Gemini returned an empty response. "
                        f"Found {num_results} relevant document(s): {src_list}."
                    )
            except Exception as e:
                # Surface the real error so we can see it in the chat,
                # not just buried in the server log.
                err_str = str(e)
                print(f"[Gemini] {err_str}")
                answer = (
                    f"Found {num_results} relevant document(s): {src_list}. "
                    f"Gemini error during synthesis: {err_str[:300]}"
                )
        else:
            # NO GEMINI: Just list what was found
            src_list = ", ".join(s["title"] for s in sources[:5])
            answer = f"Found {num_results} documents: {src_list}. Use Gemini for synthesis."

        # Update session
        session.history.append(ChatMessage(role="user", text=query))
        session.history.append(ChatMessage(role="model", text=answer))
        session.last_active = time.time()

        # Build response
        return IntelligenceResponse(
            answer=answer,
            sources=[{"title": s["title"], "uri": s["uri"]} for s in sources[:8]],
            search_results=num_results,
            confidence=_score(num_results),
            job_context=session.job_context,
            suggested_followups=_followups(query, session.job_context))

    def clear_session(self, sid: str):
        s = self.get_session(sid)
        if s: 
            s.history.clear()
            s.job_context = None
            s.cached_sources.clear()

_intel = None
def get_intelligence():
    global _intel
    if _intel is None: 
        _intel = JobIntelligence()
    return _intel