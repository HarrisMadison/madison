"""
Phase 4 Job Intelligence — Optimized Architecture
- Vertex AI Search: RETRIEVAL ONLY (no LLM summarization, saves quota)
- Gemini: ALL synthesis and answering (cheap, fast, better)
- Smart caching: Don't re-search unnecessarily
"""
import os, re, uuid, time
from collections import deque
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from google.cloud import discoveryengine_v1 as discoveryengine
from google.api_core import exceptions as gapi_exceptions
from google.oauth2 import service_account
import google.auth
import google.generativeai as genai

# Shared full-text fetcher — imported from vertex/. Falls back gracefully if
# unavailable so this module still works in isolation.
try:
    import sys
    _REPO = Path(__file__).resolve().parent.parent
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    from vertex.document_fetch import get_document_by_name as _fetch_doc_by_name
    _FETCH_AVAILABLE = True
except Exception as _e:
    print(f"[Phase4-scripts] document_fetch unavailable: {_e}")
    _FETCH_AVAILABLE = False

# Local filename index — lets us answer name-based questions WITHOUT calling
# Vertex search. Eliminates 95% of search-quota usage.
try:
    from local_index import get_index as _get_local_index
    _LOCAL_INDEX_AVAILABLE = True
except Exception as _e:
    print(f"[Phase4-scripts] local_index unavailable: {_e}")
    _LOCAL_INDEX_AVAILABLE = False

SERVING_CONFIG = os.getenv("VERTEX_SERVING_CONFIG", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
SESSION_TTL    = 3600
MAX_HISTORY    = 6
CONTEXT_CACHE_MINUTES = 15  # Reuse search results for this long

# ─── Vertex rate-limit guards ─────────────────────────────────────────────
# Google's `search_requests_regional` quota = 300/min, NOT adjustable.
# Windowed limiter: never exceed 240 calls in any rolling 60-second window
# (240 = 80% of cap, leaves headroom for bursts and other code paths).
VERTEX_MAX_PER_MINUTE = 200
_vertex_call_times: deque = deque()

def _throttle_vertex_call():
    """Block until making one more search call would not exceed the cap.
    Drops timestamps older than 60s, sleeps if we're at the ceiling."""
    now = time.time()
    cutoff = now - 60.0
    while _vertex_call_times and _vertex_call_times[0] < cutoff:
        _vertex_call_times.popleft()
    if len(_vertex_call_times) >= VERTEX_MAX_PER_MINUTE:
        # Wait until the oldest call ages out of the window
        sleep_for = (_vertex_call_times[0] + 60.0) - now + 0.05
        if sleep_for > 0:
            print(f"[throttle] At {len(_vertex_call_times)}/min cap — sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
    _vertex_call_times.append(time.time())


# Module-level result cache. Key: normalized query string. Value: (sources,
# num_results, timestamp). Lets repeated identical questions skip Vertex.
_GLOBAL_RESULT_CACHE: Dict[str, tuple] = {}
_GLOBAL_CACHE_TTL = 1800  # 30 minutes — same query rarely changes its answer

def _cache_key(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())

def _cache_get(query: str):
    key = _cache_key(query)
    hit = _GLOBAL_RESULT_CACHE.get(key)
    if not hit:
        return None
    sources, num_results, ts = hit
    if time.time() - ts > _GLOBAL_CACHE_TTL:
        _GLOBAL_RESULT_CACHE.pop(key, None)
        return None
    return sources, num_results

def _cache_put(query: str, sources, num_results):
    _GLOBAL_RESULT_CACHE[_cache_key(query)] = (sources, num_results, time.time())
    # Cap cache size to prevent unbounded growth
    if len(_GLOBAL_RESULT_CACHE) > 200:
        oldest = sorted(_GLOBAL_RESULT_CACHE.items(), key=lambda kv: kv[1][2])[:50]
        for k, _ in oldest:
            _GLOBAL_RESULT_CACHE.pop(k, None)

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
        self._tools = None

        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            try:
                genai.configure(api_key=api_key)
                # Only register tools if the shared fetcher is importable
                if _FETCH_AVAILABLE:
                    self._tools = [self._build_tools()]
                self._gemini = genai.GenerativeModel(
                    model_name=GEMINI_MODEL,
                    system_instruction=SYSTEM_PROMPT,
                    tools=self._tools,
                )
                self._use_gemini = True
                tool_state = "with tools" if self._tools else "no tools"
                print(f"[Phase4] Gemini synthesis ON ({GEMINI_MODEL}) {tool_state}")
            except Exception as e:
                print(f"[Phase4] Gemini init failed: {e}")
        else:
            print("[Phase4] No GEMINI_API_KEY — direct search results only")

    @staticmethod
    def _build_tools():
        """Declare get_document_by_name as a Gemini tool."""
        get_doc_tool = genai.protos.FunctionDeclaration(
            name="get_document_by_name",
            description=(
                "Fetches FULL text of ONE specific document by name (fuzzy match supported). "
                "Use this tool whenever: "
                "(a) the user mentions a specific document name, address, property, or filename "
                "(e.g. '106 madison avenue', 'Andover P&L', 'Northridge appraisal'), OR "
                "(b) the document excerpts in the prompt are empty / don't contain the requested info, OR "
                "(c) the user uses words like 'read', 'open', 'tell me about', 'show me' followed by anything that could be a document or address. "
                "The fuzzy matcher handles spaces vs hyphens vs underscores in filenames. "
                "HARD LIMIT: do NOT call this tool more than 2 times per user message. "
                "After 2 calls, answer from what you have."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "document_name": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description=(
                            "The document name, address, or filename hint from the user's question. "
                            "Pass the most-specific identifying phrase — e.g. for '106 madison avenue pdf' "
                            "pass '106 madison avenue', for 'tell me about the Andover invoice' pass 'Andover invoice'."
                        ),
                    ),
                },
                required=["document_name"],
            ),
        )
        return genai.protos.Tool(function_declarations=[get_doc_tool])

    def _dispatch_tool(self, name: str, args: dict) -> str:
        if name == "get_document_by_name":
            if not _FETCH_AVAILABLE:
                return ("get_document_by_name is currently unavailable. "
                        "Answer using the document excerpts already in the prompt.")
            doc_name = (args or {}).get("document_name", "").strip()
            result = _fetch_doc_by_name(doc_name)
            if not result.get("ok"):
                extras = ""
                if result.get("candidates"):
                    extras = f" Near-matches I do see: {', '.join(result['candidates'])}."
                return (
                    f"get_document_by_name: no exact match for {doc_name!r}.{extras} "
                    f"DO NOT tell the user 'no documents were retrieved' — the "
                    f"DOCUMENT EXCERPTS already in the prompt contain relevant "
                    f"material. Answer from those excerpts now and list the "
                    f"document names that appeared."
                )
            body = result.get("text") or "(empty document)"
            return f"get_document_by_name OK — file: {result['title']}\n\n{body}"
        return f"Unknown tool: {name}"

    def _run_tool_loop(self, prompt: str, max_rounds: int = 3) -> str:
        """Send prompt to Gemini and resolve tool calls until a final answer.

        HARD LIMIT: at most 2 total tool calls per user message. Without this,
        Gemini happily fires get_document_by_name dozens of times when it
        sees relevant filenames in the prompt, downloading each from GCS
        sequentially — which can take 5+ minutes per chat message."""
        chat = self._gemini.start_chat(enable_automatic_function_calling=False)
        msg: object = prompt
        total_tool_calls = 0
        TOTAL_TOOL_CALL_BUDGET = 2
        for round_n in range(max_rounds):
            try:
                resp = chat.send_message(msg)
            except Exception as e:
                return f"Gemini tool-loop error: {e}"
            try:
                parts = resp.candidates[0].content.parts
            except Exception:
                parts = []
            calls, texts = [], []
            for p in parts:
                fc = getattr(p, "function_call", None)
                if fc and getattr(fc, "name", ""):
                    calls.append(fc)
                else:
                    t = getattr(p, "text", "") or ""
                    if t:
                        texts.append(t)
            if not calls:
                return "".join(texts).strip() or (resp.text or "").strip()

            tool_responses = []
            for fc in calls:
                # Hard cap on total fetches per chat message.
                if total_tool_calls >= TOTAL_TOOL_CALL_BUDGET:
                    print(f"[diag] Tool-call budget exhausted ({total_tool_calls}/{TOTAL_TOOL_CALL_BUDGET}). "
                          f"Refusing further calls and forcing answer from existing context.")
                    tool_responses.append(
                        genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name=fc.name,
                                response={"result":
                                    "BUDGET EXHAUSTED. You have already used your "
                                    "document-fetch quota for this question. "
                                    "Answer the user NOW from the document excerpts "
                                    "already in the prompt and any tool results you "
                                    "have so far. Do NOT call this tool again."
                                },
                            )
                        )
                    )
                    continue
                total_tool_calls += 1
                try:
                    args = dict(fc.args) if fc.args else {}
                except Exception:
                    args = {}
                output = self._dispatch_tool(fc.name, args)
                tool_responses.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fc.name,
                            response={"result": output},
                        )
                    )
                )
            msg = tool_responses
        return "(Hit tool-call iteration limit without a final answer.)"

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

        print(f"[diag] Vertex search query={query!r} serving_config={SERVING_CONFIG!r}")

        # Cache check (module-level, cross-session) — repeated identical
        # questions skip the Vertex call entirely.
        cached = _cache_get(query)
        if cached is not None:
            print(f"[diag] Result cache HIT for {query!r}")
            return cached

        # Throttle: never exceed 240/min on the search API
        _throttle_vertex_call()

        # Single quick retry on 429. Used to be 3 retries with 19s backoff,
        # but that just hung the chat for a minute while making the quota
        # situation worse. Better to fail fast and let the user retry.
        results = None
        for attempt, backoff in enumerate([0, 3], start=1):
            if backoff:
                print(f"[diag] Backoff {backoff}s before retry {attempt}")
                time.sleep(backoff)
                _throttle_vertex_call()
            try:
                response = client.search(req)
                results = list(response)
                print(f"[diag] Vertex returned {len(results)} raw results (attempt {attempt})")
                break
            except gapi_exceptions.ResourceExhausted as qe:
                if attempt >= 2:
                    print(f"[diag] Vertex 429 after {attempt} attempts — giving up")
                    raise
                print(f"[diag] Vertex 429 on attempt {attempt} — will retry once")
            except Exception as _e:
                print(f"[diag] Vertex search EXCEPTION: {type(_e).__name__}: {_e}")
                raise

        if results is None:
            results = []

        sources = []
        seen = set()
        # Cap fallback fetches per query — each fetch is a GCS download +
        # text extraction round trip (1-15s per doc). Doing 10 = 2+ min wait.
        # Limit to first 3 docs that need fallback; rest get metadata-only.
        fallback_budget = 3
        for r in results:
            doc = r.document
            title = _safe_struct_get(doc.struct_data, "title", "")
            uri = (_safe_struct_get(doc.struct_data, "source_uri", "")
                   or _safe_struct_get(doc.struct_data, "gcs_uri", "")
                   or _safe_struct_get(doc.struct_data, "uri", ""))

            # Extract content text robustly from EVERY source Vertex provides.
            # For docs imported with content.rawBytes, snippets[] is usually
            # empty but extractive_segments[] / extractive_answers[] DO get
            # populated. We collect from all three.
            text_parts = []
            try:
                derived = doc.derived_struct_data
                if derived:
                    d = dict(derived) if not isinstance(derived, dict) else derived

                    for snip in (d.get("snippets") or []):
                        s = snip.get("snippet") if isinstance(snip, dict) else getattr(snip, "snippet", "")
                        if s:
                            text_parts.append(re.sub(r"<[^>]+>", "", str(s)))

                    for seg in (d.get("extractive_segments") or []):
                        s = seg.get("content") if isinstance(seg, dict) else getattr(seg, "content", "")
                        if s:
                            text_parts.append(str(s))

                    for ans in (d.get("extractive_answers") or []):
                        s = ans.get("content") if isinstance(ans, dict) else getattr(ans, "content", "")
                        if s:
                            text_parts.append(str(s))
            except Exception as e:
                print(f"[Vertex] snippet extract error: {e}")

            snippet_text = "\n".join(text_parts).strip()

            # FALLBACK FETCH — fills in content for top docs that Vertex
            # imported as rawBytes (those don't get auto-snippeted). Capped
            # at 3 per query (~10-30s total). Without this, the chat says
            # 'document excerpts are empty' for everything we imported.
            if (not snippet_text
                    and uri
                    and uri.startswith("gs://")
                    and _FETCH_AVAILABLE
                    and fallback_budget > 0):
                try:
                    fallback_budget -= 1
                    fetched = _fetch_doc_by_name(title or Path(uri).name)
                    if fetched.get("ok") and fetched.get("text"):
                        snippet_text = fetched["text"][:3000]
                        print(f"[diag] Fallback fetch hit for {title!r}: {len(snippet_text)} chars")
                except Exception as fe:
                    print(f"[diag] Fallback fetch failed for {title!r}: {fe}")

            if not title:
                title = _safe_struct_get(doc.derived_struct_data, "title", "")

            label = title or (Path(uri).name if uri else doc.id or "Document")

            if label and label not in seen:
                seen.add(label)
                sources.append({
                    "title": label,
                    "uri": uri,
                    "snippet": snippet_text[:3000],
                })

        # Cache the result for the next 5 min so repeated questions skip Vertex
        _cache_put(query, sources[:10], len(sources))

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
            # ── LOCAL-FIRST PATH ────────────────────────────────────────
            # First try the in-memory filename index. If it finds strong
            # matches, we fetch them directly from GCS and SKIP Vertex
            # entirely — zero search-quota usage.
            sources = []
            num_results = 0
            local_hits_used = False
            if _LOCAL_INDEX_AVAILABLE and _FETCH_AVAILABLE:
                try:
                    idx = _get_local_index()
                    local_hits = idx.find(query, top_n=3)
                    # Score >= 100 means full-substring match — high confidence.
                    strong = [h for h in local_hits if h["score"] >= 100]
                    if strong:
                        local_hits_used = True
                        print(f"[diag] Local index strong-match: {[h['name'] for h in strong]} — SKIPPING Vertex")
                        for h in strong[:3]:
                            try:
                                fetched = _fetch_doc_by_name(h["name"])
                                if fetched.get("ok") and fetched.get("text"):
                                    sources.append({
                                        "title":   fetched["title"],
                                        "uri":     fetched.get("uri", h["uri"]),
                                        "snippet": fetched["text"][:3000],
                                    })
                                    print(f"[diag] Fetched local hit {fetched['title']!r}: {len(fetched['text'])} chars")
                            except Exception as fe:
                                print(f"[diag] Local-hit fetch failed for {h['name']!r}: {fe}")
                        num_results = len(sources)
                        if sources:
                            session.last_search_query = full_query
                            session.last_search_time = now
                            session.cached_sources = sources
                except Exception as ie:
                    print(f"[diag] Local index lookup error: {ie}")

            # ── FALLBACK: Vertex search if local index missed ───────────────
            if not local_hits_used:
                try:
                    sources, num_results = self._vertex_search(full_query)
                    session.last_search_query = full_query
                    session.last_search_time = now
                    session.cached_sources = sources
                except gapi_exceptions.ResourceExhausted as qe:
                    print(f"[Vertex] Quota wall hit: {qe}")
                    session.history.append(ChatMessage(role="user", text=query))
                    msg = (
                        "⚠ Vertex search is currently rate-limited. "
                        "For property/document name queries, the local index "
                        "should have caught this — try rephrasing with the "
                        "specific address or filename."
                    )
                    session.history.append(ChatMessage(role="model", text=msg))
                    session.last_active = time.time()
                    return IntelligenceResponse(
                        answer=msg, sources=[], search_results=0,
                        confidence="none", job_context=session.job_context,
                        suggested_followups=["Try again", "What documents do we have?"],
                    )
                except Exception as e:
                    print(f"[Vertex] {e}")
                    sources, num_results = [], 0

                # AUTO-RESCUE: If Vertex's top results all have empty content
                # (because Vertex's relevance ranking didn't surface the actual
                # match), proactively try a name-based fetch using the user's
                # query as a hint. This catches files like '106-Madison-Avenue-.pdf'
                # where Vertex prioritizes 'madison ave contract' instead.
                try:
                    all_empty = sources and all(not s.get("snippet", "").strip() for s in sources)
                    if all_empty and _FETCH_AVAILABLE:
                        print(f"[diag] Auto-rescue: all top sources have empty snippets; trying name-based fetch with {query!r}")
                        rescue = _fetch_doc_by_name(query)
                        if rescue.get("ok") and rescue.get("text"):
                            rescue_title = rescue["title"]
                            rescue_uri = rescue.get("uri", "")
                            print(f"[diag] Auto-rescue HIT: {rescue_title!r} ({len(rescue['text'])} chars)")
                            rescued = {
                                "title": rescue_title,
                                "uri": rescue_uri,
                                "snippet": rescue["text"][:3000],
                            }
                            existing_titles = {s.get("title") for s in sources}
                            if rescue_title not in existing_titles:
                                sources = [rescued] + sources
                            else:
                                sources = [rescued] + [s for s in sources if s.get("title") != rescue_title]
                            num_results = len(sources)
                            session.cached_sources = sources
                        else:
                            print(f"[diag] Auto-rescue MISS for {query!r}: {rescue.get('error', 'no match')}")
                except Exception as ex:
                    print(f"[diag] Auto-rescue error: {ex}")

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
                if self._tools:
                    answer = self._run_tool_loop(prompt)
                else:
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