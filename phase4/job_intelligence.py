"""
Phase 4B: Job Intelligence Engine — OPTIMIZED ARCHITECTURE
============================================================
Two-stage pipeline (NO QUOTA WASTE):
  1. Vertex AI Search  →  RETRIEVAL ONLY (snippets, no LLM summarization)
  2. Gemini 2.5 Flash  →  ALL synthesis (cheap, fast, better answers)

Key features:
  - NO summary_spec → does not burn discoveryengine.googleapis.com/llm_requests quota
  - Multi-turn conversation with job context tracking
  - 15-minute search result caching → fewer Vertex calls
  - Snippet-based Gemini synthesis → better grounded answers
  - Resilient to quota errors → returns partial results gracefully
  - Photo lookup from GCS photo_index.json (preserved)
  - Large PDF and OneDrive media link support (preserved)

INSTALL:
    pip install google-generativeai google-cloud-discoveryengine google-cloud-storage
"""

import os
import re
import uuid
import json
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import google.generativeai as genai
from google.cloud import discoveryengine_v1 as discoveryengine
from google.oauth2 import service_account
from google.api_core.client_options import ClientOptions

# Shared full-text fetcher. Lets Gemini read whole documents on demand instead
# of guessing from snippets when the user names a specific file.
#
# Belt-and-suspenders: ensure the repo root is on sys.path before importing
# the top-level `vertex` package. Different launchers (simple_web.py,
# scripts/web.py, or running phase4 directly) start with different paths.
try:
    import sys as _sys
    _REPO_ROOT_FOR_FETCH = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT_FOR_FETCH) not in _sys.path:
        _sys.path.insert(0, str(_REPO_ROOT_FOR_FETCH))
    from vertex.document_fetch import get_document_by_name as _fetch_doc_by_name
    _FETCH_AVAILABLE = True
except Exception as _e:
    print(f"[Phase4] document_fetch unavailable: {_e}")
    _FETCH_AVAILABLE = False

# ── Config — read from environment / .env ───────────────────────────────────
import os as _os
from pathlib import Path as _Path

PROJECT_ID   = _os.getenv("GCP_PROJECT_ID",       "commanding-way-380716")
ENGINE_ID    = _os.getenv("VERTEX_ENGINE_ID",      "madison-ave-search-app")
LOCATION     = _os.getenv("GCP_LOCATION",          "global")
# NOTE: gemini-1.5-* and gemini-1.0-* are fully shut down (return 404).
# As of April 2026, gemini-2.5-flash is the recommended production model.
# Plan to migrate to a 3.x-series model before June 2026.
GEMINI_MODEL = _os.getenv("GEMINI_MODEL",          "gemini-2.5-flash")

MAX_RESULTS         = 50    # Vertex page_size — covers more docs per query
MAX_SEGMENTS        = 40    # Default # of doc excerpts sent to Gemini synthesis
MAX_SEGMENTS_LIST   = 50   # When user asks 'list all' — title-only context for many docs
MAX_HISTORY  = 8
SESSION_TTL  = 3600
CACHE_MINUTES = 15   # Reuse Vertex search results within this window per session


def _resolve_sa_key() -> str:
    explicit = _os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if explicit and _Path(explicit).exists():
        return explicit
    bootstrap_key = _Path(__file__).resolve().parent.parent / "Phase3_Bootstrap" / "secrets" / "service-account.json"
    if bootstrap_key.exists():
        return str(bootstrap_key)
    local_key = _Path(__file__).parent / "service-account.json"
    if local_key.exists():
        return str(local_key)
    return "service-account.json"


SA_KEY = _resolve_sa_key()


# ── System prompt for Gemini ─────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a real estate and document intelligence assistant.
You help the portfolio owner get direct, accurate answers about
their properties, deals, financials, legal documents, permits, and investment performance.

PRIMARY SOURCE: The DOCUMENT EXCERPTS block in every prompt is your primary
source of truth. Always read it first and answer from it. List the document
names you saw in the excerpts when relevant — the user wants to know which
files you found, even when you cannot fully answer their question.

TOOLS: You may have an optional tool called get_document_by_name. Only call
it when the user names an EXACT filename AND the excerpts do not already
answer the question. For general topical questions ("summarize the claims
status", "what's going on with X"), answer from the excerpts directly —
DO NOT call the tool. If a tool call returns no match, fall back to the
excerpts — never tell the user 'no documents were retrieved' when the
excerpts block contains documents.

RULES:
1. Answer using ONLY the document excerpts provided. Never invent addresses,
   dollar amounts, dates, entity names, or lender details.
2. For financial figures: state the number, cite the source document, and note
   if the figure may be partial.
3. For property questions, structure answers as:
   - Property, Appraisal / Purchase Price, Key Dates, Financial Summary, Open Items
4. If documents do not clearly answer the question, say exactly what you DID find
   and what is missing. The owner can then pull the right document.
5. Use conversation history to track which property is in focus so the owner
   does not have to repeat the address on every follow-up.
6. Be direct and concise. No preambles like "Great question" or "Certainly".
7. If you detect conflicting figures across documents, flag it explicitly.
8. When a photo card appears in results for a photo request, always reply:
   "Photos are available for this property - click the photo link below to view them in OneDrive.
   Note: I cannot display photos directly in this chat."
   If no photo card is present say: "No photos are indexed for this property yet."
9. Keep answers under 300 words unless a detailed breakdown is explicitly requested.
10. For portfolio-wide questions, summarize what the indexed documents show.

DOMAIN CONTEXT:
This is a real estate investment portfolio operating on Long Island NY.
Properties are acquired, renovated, and sold or held for income.
Key document types: Appraisals, Closing packages, Title reports, P+L statements,
Permits, Inspection reports, Flood disclosures, Loan letters, Invoices, Insurance.
LLC entities include: Bobbomatic LLC, Flip It LLC, Shearwater Way LLC, Lama Drive LLC."""


# ── Data classes ────────────────────────────────────────────────────────────
@dataclass
class ChatMessage:
    role: str
    text: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class CachedSearch:
    query: str
    excerpts: list
    media_links: list
    source_uris: dict
    cached_at: float


@dataclass
class ChatSession:
    session_id:   str
    history:      list = field(default_factory=list)
    job_context:  Optional[str] = None
    created_at:   float = field(default_factory=time.time)
    last_active:  float = field(default_factory=time.time)
    last_search:  Optional[CachedSearch] = None
    page_offset:  int = 0   # how many items already shown for current cached search


@dataclass
class IntelligenceResponse:
    answer:          str
    sources:         list
    search_results:  int
    confidence:      str
    job_context:     Optional[str]
    suggested_followups: list
    media_links:     list = field(default_factory=list)
    source_uris:     dict = field(default_factory=dict)


# ── Helpers ─────────────────────────────────────────────────────────────────
def _load_creds():
    key_path = Path(SA_KEY)
    if not key_path.exists():
        key_path = Path(__file__).parent / SA_KEY
    return service_account.Credentials.from_service_account_file(
        str(key_path),
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )


def _extract_job_context(text: str) -> Optional[str]:
    STOPWORDS = {"tell","show","me","about","the","of","for","on","get",
                 "find","what","is","are","give","summary","photos","photo",
                 "docs","documents","please","do","we","have","any"}
    m = re.search(
        r"\b(\d{1,5})\s+([A-Za-z][a-zA-Z\.\'']+(?:\s+[A-Za-z][a-zA-Z\.\'']+){0,4})\b",
        text
    )
    if m:
        num = m.group(1)
        words = m.group(2).split()
        while words and words[-1].lower() in STOPWORDS:
            words.pop()
        if words:
            return f"{num} " + " ".join(w.title() for w in words)
    job_id = re.search(r"\bJOB[-_]\d{4}[-_]\d{2,4}\b", text, re.IGNORECASE)
    if job_id:
        return job_id.group(0).upper()
    return None


def _score_confidence(excerpt_count: int, has_direct_hit: bool) -> str:
    if excerpt_count == 0:
        return "none"
    if excerpt_count >= 5 and has_direct_hit:
        return "high"
    if excerpt_count >= 2:
        return "medium"
    return "low"


def _suggest_followups(query: str, job_context: Optional[str]) -> list:
    q = query.lower()
    suggestions = []

    if any(w in q for w in ["invoice", "payment", "owe", "paid", "balance"]):
        suggestions += ["Are there any other open invoices?", "What's the total project cost?"]
    elif any(w in q for w in ["permit", "inspection"]):
        suggestions += ["When does the permit expire?", "Are there any failed inspections?"]
    elif any(w in q for w in ["status", "progress", "stage"]):
        suggestions += ["What's still open on this job?", "Any insurance issues?"]
    elif any(w in q for w in ["claim", "insurance", "adjuster"]):
        suggestions += ["Has the adjuster responded?", "What's the approved scope amount?"]
    elif any(w in q for w in ["estimate", "scope", "xactimate"]):
        suggestions += ["Has the insurer approved the scope?", "Any change orders?"]
    elif any(w in q for w in ["apprais", "value", "comparable"]):
        suggestions += ["What was the comparable sales used?", "What's the site value?"]
    elif any(w in q for w in ["loan", "draw", "lender"]):
        suggestions += ["What's the current loan balance?", "Are there any other draws?"]

    if job_context and "job" not in q:
        suggestions.append(f"What documents do we have for {job_context}?")

    return suggestions[:3]


def _safe_struct_to_dict(struct):
    """Convert proto struct_data to a Python dict safely."""
    if struct is None:
        return {}
    try:
        return dict(struct)
    except Exception:
        try:
            result = {}
            for k in struct:
                v = struct[k]
                result[k] = v
            return result
        except Exception:
            return {}


def _extract_snippets_from_doc(doc) -> str:
    """
    Pull snippet text from derived_struct_data without triggering LLM quota.
    Reads all three content fields Vertex may populate (snippets, extractive
    segments, extractive answers) because different doc types put their text
    under different fields. Short single-page invoices and .docx tables, for
    example, only show up under extractive_segments.
    """
    parts = []
    try:
        if not hasattr(doc, "derived_struct_data") or doc.derived_struct_data is None:
            return ""
        derived = doc.derived_struct_data
        try:
            d = dict(derived)
        except Exception:
            d = {}

        # 1) snippets[] -- query-aware short excerpts (HTML-tagged)
        for s in (d.get("snippets") or []):
            try:
                txt = s.get("snippet", "") if isinstance(s, dict) else getattr(s, "snippet", "")
                if txt:
                    parts.append(re.sub(r"<[^>]+>", "", str(txt)).strip())
            except Exception:
                continue

        # 2) extractive_segments[] -- larger chunks (~2-3 paragraphs each)
        for seg in (d.get("extractive_segments") or []):
            try:
                txt = seg.get("content", "") if isinstance(seg, dict) else getattr(seg, "content", "")
                if txt:
                    parts.append(str(txt).strip())
            except Exception:
                continue

        # 3) extractive_answers[] -- pinpoint answers extracted from doc
        for ans in (d.get("extractive_answers") or []):
            try:
                txt = ans.get("content", "") if isinstance(ans, dict) else getattr(ans, "content", "")
                if txt:
                    parts.append(str(txt).strip())
            except Exception:
                continue
    except Exception as e:
        print(f"[Vertex] snippet extract error: {e}")

    snippet_text = "\n".join(parts).strip()
    # 3000-char window per doc -- big enough for short invoices & contract pages
    return snippet_text[:3000]


# ── Core engine ─────────────────────────────────────────────────────────────
class JobIntelligence:
    """
    Two-stage RAG with optimized quota usage:
      Vertex AI Search (snippets only) → Gemini Flash (synthesis)
    """

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            genai.configure(api_key=api_key)
        else:
            creds = _load_creds()
            genai.configure(credentials=creds)

        # Build the function-calling tool list. If the fetcher import failed
        # we register only the search tool, so Gemini still works.
        self._tools = [self._build_tools()] if _FETCH_AVAILABLE else None

        self._gemini = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT,
            tools=self._tools,
        )
        tool_state = "with tools" if self._tools else "no tools"
        print(f"[Phase4] Gemini synthesis ON ({GEMINI_MODEL}) {tool_state}")

        self._search_client = discoveryengine.SearchServiceClient(
            credentials=_load_creds(),
            client_options=ClientOptions(
                api_endpoint=f"{LOCATION}-discoveryengine.googleapis.com"
                if LOCATION != "global"
                else "discoveryengine.googleapis.com"
            )
        )
        self._serving_config = (
            f"projects/{PROJECT_ID}/locations/{LOCATION}/"
            f"collections/default_collection/engines/{ENGINE_ID}/"
            f"servingConfigs/default_search"
        )
        print(f"[Phase4] Vertex AI Search engine: {ENGINE_ID} (project: {PROJECT_ID})")
        print(f"[Phase4] Architecture: RETRIEVAL-ONLY (no LLM summary, saves quota)")

        self._sessions = {}

    # ── Tool declarations for Gemini function calling ───────────────────
    @staticmethod
    def _build_tools():
        """Declare get_document_by_name (and search_documents as a future hook).

        Currently only get_document_by_name is wired through the agent loop —
        snippet retrieval still happens automatically via retrieve() before
        Gemini runs. We expose the search tool slot for future expansion when
        we move retrieve() behind function calling too.
        """
        get_doc_tool = genai.protos.FunctionDeclaration(
            name="get_document_by_name",
            description=(
                "OPTIONAL TOOL — only call this when ALL of the following are true: "
                "(a) the user explicitly references a specific file by an exact "
                "or near-exact filename (e.g. 'open Northridge_Appraisal.pdf', "
                "'read the file 15-Northridge-final-invoice'), AND "
                "(b) the snippet excerpts in the prompt do NOT already answer "
                "the question. "
                "DO NOT call this for general topical questions like "
                "'summarize the claims status', 'what permits do we have', "
                "'tell me about X' — the snippet excerpts in the prompt cover "
                "those, and you should answer from them directly. "
                "If you are unsure, do NOT call this tool — answer from the "
                "document excerpts already in the prompt instead."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "document_name": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description=(
                            "The filename or partial name as the user "
                            "referenced it."
                        ),
                    ),
                },
                required=["document_name"],
            ),
        )
        return genai.protos.Tool(function_declarations=[get_doc_tool])

    def _dispatch_tool(self, name: str, args: dict) -> str:
        """Run a single tool call and return a plain string Gemini can read.

        IMPORTANT: When get_document_by_name fails (no match), we DO NOT tell
        Gemini 'no documents exist'. We tell it to fall back to the snippet
        excerpts already in the prompt. This preserves pre-tool behavior —
        the snippets are still the primary answer source; the tool is an
        OPTIONAL upgrade for when an exact filename is given.
        """
        if name == "get_document_by_name":
            if not _FETCH_AVAILABLE:
                return ("get_document_by_name is currently unavailable. "
                        "Answer the user's question using the document "
                        "excerpts already provided in the prompt.")
            doc_name = (args or {}).get("document_name", "").strip()
            result = _fetch_doc_by_name(doc_name)
            if not result.get("ok"):
                extras = ""
                if result.get("candidates"):
                    extras = (f" Near-matches I do see: "
                              f"{', '.join(result['candidates'])}.")
                # Explicitly direct Gemini back to the snippet excerpts so it
                # does not interpret the failure as 'no docs at all'.
                return (
                    f"get_document_by_name: no exact match for "
                    f"{doc_name!r}.{extras} "
                    f"DO NOT tell the user 'no documents were retrieved' — "
                    f"that is wrong. The DOCUMENT EXCERPTS already provided "
                    f"in the original prompt contain relevant material. "
                    f"Answer the user's question from those excerpts now, "
                    f"and list which document names appeared in them."
                )
            body = result.get("text") or "(empty document)"
            return f"get_document_by_name OK — file: {result['title']}\n\n{body}"
        return f"Unknown tool: {name}"

    def new_session(self) -> str:
        sid = str(uuid.uuid4())
        self._sessions[sid] = ChatSession(session_id=sid)
        self._cleanup_old_sessions()
        return sid

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        sess = self._sessions.get(session_id)
        if sess:
            if time.time() - sess.last_active > SESSION_TTL:
                del self._sessions[session_id]
                return None
        return sess

    def _cleanup_old_sessions(self):
        now = time.time()
        expired = [sid for sid, s in self._sessions.items()
                   if now - s.last_active > SESSION_TTL]
        for sid in expired:
            del self._sessions[sid]

    def retrieve(self, query: str, job_context: Optional[str] = None) -> tuple:
        """
        Vertex AI Search SNIPPET ONLY (no summary_spec → no LLM quota burn).
        Returns (excerpts, media_links, source_uris).
        """
        search_query = f"{job_context} {query}" if job_context else query

        def _make_req(q, flt="", with_summary: bool = False):
            """Build a search request.

            with_summary controls whether we ask Vertex for an LLM-generated
            summary. We default to False because that summary call burns
            `discoveryengine.googleapis.com/llm_requests` quota AND that
            quota is currently throttled (429s on every retry). Gemini does
            the synthesis from snippets anyway — we don't need Vertex's.
            """
            content_spec_kwargs = dict(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True,
                    max_snippet_count=5,
                ),
                # Ask for extractive segments + answers too. Different doc
                # types only return content under one of these. We read
                # whichever fields actually have text.
                extractive_content_spec=discoveryengine.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
                    max_extractive_segment_count=3,
                    max_extractive_answer_count=3,
                ),
            )
            if with_summary:
                content_spec_kwargs["summary_spec"] = (
                    discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(
                        summary_result_count=10,
                        include_citations=True,
                    )
                )
            kw = dict(
                serving_config=self._serving_config,
                query=q,
                page_size=MAX_RESULTS,
                content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                    **content_spec_kwargs
                ),
            )
            if flt:
                kw["filter"] = flt
            return discoveryengine.SearchRequest(**kw)

        vertex_summary = ""
        try:
            response = self._search_client.search(
                _make_req(search_query, 'NOT document_type: ANY("scanned_document")'))
            # CRITICAL: materialize results BEFORE reading summary, otherwise
            # response.summary.summary_text comes back as empty string.
            results = list(response)
            if not results:
                response = self._search_client.search(_make_req(search_query))
                results = list(response)
            try:
                vertex_summary = (response.summary.summary_text or "").strip()
            except Exception:
                vertex_summary = ""
        except Exception as e:
            err_msg = str(e)
            print(f"[Vertex] Search error: {err_msg[:200]}")
            try:
                response = self._search_client.search(_make_req(search_query))
                results = list(response)
                try:
                    vertex_summary = (response.summary.summary_text or "").strip()
                except Exception:
                    vertex_summary = ""
            except Exception as e2:
                print(f"[Vertex] Retry error: {str(e2)[:200]}")
                return [], [], {}

        excerpts = []
        media_links = []
        source_uris = {}

        # Inject Vertex's own summary as a synthetic first excerpt so Gemini
        # has cross-document context even when individual snippets are weak.
        if vertex_summary:
            excerpts.append({
                "source":  "[Vertex Search summary]",
                "content": vertex_summary,
            })

        for result in results:
            doc = result.document
            struct = _safe_struct_to_dict(doc.struct_data)

            source_uri   = struct.get("source_uri", "")
            doc_type     = struct.get("document_type", "")
            onedrive_url = struct.get("onedrive_url", "")

            if doc_type == "photo_index":
                prop_name = struct.get("property", struct.get("title", "Property"))
                photo_count = struct.get("photo_count", 0)
                if onedrive_url:
                    media_links.append({
                        "type":     "photos",
                        "property": prop_name,
                        "count":    photo_count,
                        "url":      onedrive_url,
                    })
                excerpts.append({
                    "source":  f"{prop_name} (photo index)",
                    "content": f"{photo_count} photos available for {prop_name} in OneDrive.",
                })
                continue

            if doc_type == "large_pdf_pointer":
                title = struct.get("title", "Document")
                size_mb = struct.get("size_mb", 0)
                gcs_uri = struct.get("gcs_uri", "")
                media_links.append({
                    "type":    "document",
                    "title":   title,
                    "size_mb": size_mb,
                    "url":     gcs_uri,
                })
                excerpts.append({
                    "source":  title,
                    "content": struct.get("summary", f"Large PDF: {title} ({size_mb:.1f} MB)"),
                })
                continue

            title = struct.get("title", "")
            source_label = source_uri.split("/")[-1] if source_uri else (title or doc.id or "Unknown")
            snippet_content = _extract_snippets_from_doc(doc)

            # DIAGNOSTIC: log what we got per doc so we can see why answers fail.
            # Remove this once root cause is understood.
            print(f"[diag] doc='{source_label[:60]}' snippet_len={len(snippet_content)} preview={snippet_content[:120]!r}")

            # If Vertex returned no extractable text, mark it explicitly so
            # Gemini knows the doc was found but has no body content (rather
            # than receiving the filename as if it were content).
            if not snippet_content:
                snippet_content = "[No text content available for this document — likely scanned PDF or image-based content. Document exists but cannot be read without OCR.]"

            if source_label and source_label not in ("Unknown", ""):
                excerpts.append({
                    "source":     source_label,
                    "source_uri": source_uri,
                    "content":    snippet_content,
                })
                if source_uri:
                    source_uris[source_label] = source_uri

        seen_urls = set()
        unique_media = []
        for m in media_links:
            if m.get("url") and m["url"] not in seen_urls:
                seen_urls.add(m["url"])
                unique_media.append(m)

        return excerpts, unique_media, source_uris

    def _photo_lookup(self, address: str) -> list:
        if not address:
            return []

        def _norm(s):
            s = s.lower().strip()
            s = re.sub(
                r"\b(drive|dr|avenue|ave|road|rd|street|st|blvd|boulevard|"
                r"lane|ln|court|ct|place|pl|west|east|north|south|w|e|n|s)\b",
                "", s)
            return re.sub(r"[^a-z0-9]+", " ", s).strip()

        def _match(key, query):
            kn, qn = _norm(key), _norm(query)
            if kn == qn:
                return True
            q_tok = set(qn.split())
            k_tok = set(kn.split())
            if q_tok and q_tok.issubset(k_tok):
                return True
            nums = re.findall(r"\d+", query)
            if nums and any(n in kn for n in nums):
                words = [w for w in qn.split() if not w.isdigit() and len(w) > 2]
                if any(w in kn for w in words):
                    return True
            return False

        bucket_name = (os.getenv("GCS_BUCKET_NAME") or
                       os.getenv("GCS_BUCKET_RAW") or
                       os.getenv("GCS_RAW_BUCKET", ""))
        if not bucket_name:
            return []

        try:
            from google.cloud import storage as _gcs
            from google.oauth2 import service_account as _sa
            creds = _sa.Credentials.from_service_account_file(
                str(SA_KEY),
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            gcs_client = _gcs.Client(credentials=creds)
            bucket = gcs_client.bucket(bucket_name)
            blob = bucket.blob("manifests/photo_index.json")
            if not blob.exists():
                return []
            photo_index = json.loads(blob.download_as_text())
        except Exception as e:
            print(f"[Photo] GCS error: {e}")
            return []

        results = []
        for prop_name, data in photo_index.items():
            if _match(prop_name, address):
                results.append({
                    "type":  "photo",
                    "title": data.get("title") or f"{prop_name} - Photos",
                    "url":   data.get("url", ""),
                    "count": data.get("count", 0),
                })
        return results

    def synthesize(
        self,
        query:       str,
        excerpts:    list,
        session:     Optional[ChatSession] = None,
        media_links: list = None,
        page_offset: int = 0,
    ) -> str:
        if excerpts:
            ctx_parts = []
            for i, exc in enumerate(excerpts[:MAX_SEGMENTS], 1):
                content = exc.get("content", "") or "(no preview available)"
                ctx_parts.append(f"[SOURCE {i} — {exc['source']}]\n{content}")
            context_block = "\n\n─────\n\n".join(ctx_parts)
        else:
            context_block = "(No relevant documents retrieved for this query.)"

        history_block = ""
        if session and session.history:
            recent = session.history[-(MAX_HISTORY * 2):]
            for msg in recent:
                speaker = "User" if msg.role == "user" else "Assistant"
                history_block += f"{speaker}: {msg.text}\n"
        if not history_block:
            history_block = "(no prior conversation)"

        job_hint = (
            f"\n[Current job in focus: {session.job_context}]"
            if session and session.job_context else ""
        )
        media_hint = ""
        if media_links:
            for m in media_links:
                if m.get("type") in ("photo", "photos"):
                    cnt = m.get('count', 0)
                    name = m.get('title') or m.get('property', '')
                    media_hint += f"\n[PHOTO_CARD_PRESENT: {cnt} photos available for {name}]"

        prompt = (
            f"Conversation so far:\n{history_block}\n"
            f"DOCUMENT EXCERPTS:\n{context_block}\n\n"
            f"{'─' * 40}{job_hint}\n"
            f"{media_hint}\n\n"
            f"User's question: {query}"
        )

        # If tools are wired, run a tool-aware loop so Gemini can call
        # get_document_by_name when the user names a specific file. Otherwise
        # fall back to single-shot generate_content.
        try:
            if self._tools:
                return self._run_tool_loop(prompt, excerpts)
            resp = self._gemini.generate_content(prompt)
            answer = (resp.text or "").strip()
            if not answer:
                if excerpts:
                    doc_list = ", ".join(e["source"] for e in excerpts[:5])
                    return f"Found {len(excerpts)} relevant document(s): {doc_list}. Gemini returned an empty response."
                return "No documents found and Gemini returned an empty response."
            return answer
        except Exception as e:
            err_str = str(e)
            print(f"[Gemini] Synthesis error: {err_str}")
            if excerpts:
                doc_list = ", ".join(e["source"] for e in excerpts[:5])
                return (f"Found {len(excerpts)} relevant document(s): {doc_list}. "
                        f"Gemini error during synthesis: {err_str[:300]}")
            return ("I couldn't find relevant documents for that question. "
                    "Try a more specific query (address, permit number, or document name).")

    def _run_tool_loop(self, prompt: str, excerpts: list, max_rounds: int = 5) -> str:
        """Run a chat with tool-call iteration. Returns the final answer text."""
        chat = self._gemini.start_chat(enable_automatic_function_calling=False)
        msg: object = prompt
        for _ in range(max_rounds):
            try:
                resp = chat.send_message(msg)
            except Exception as e:
                # Surface the error directly so it's visible in the UI
                if excerpts:
                    doc_list = ", ".join(e2["source"] for e2 in excerpts[:5])
                    return (f"Found {len(excerpts)} relevant document(s): "
                            f"{doc_list}. Gemini tool-loop error: {e}")
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

    def chat(self, query: str, session_id: Optional[str] = None) -> IntelligenceResponse:
        session = None
        if session_id:
            session = self.get_session(session_id)
        if session is None:
            sid = self.new_session()
            session = self._sessions[sid]

        # Detect "show more" / "next page" requests — serve next slice from cache
        q_lower = query.lower().strip()
        is_more_request = any(p in q_lower for p in [
            "show more", "show me more", "more results", "more documents",
            "next page", "next batch", "see more", "continue", "and more",
        ]) and len(q_lower) < 60   # only short queries — don't false-trigger
        served_from_pagination = False

        if is_more_request and session.last_search and session.last_search.excerpts:
            print(f"[Pagination] 'Show more' detected — advancing offset from {session.page_offset}")
            cache = session.last_search
            excerpts    = cache.excerpts
            media_links = cache.media_links
            source_uris = cache.source_uris
            # Advance the offset based on what was last shown
            cached_query_lower = cache.query.lower()
            cached_is_list = any(p in cached_query_lower for p in [
                "list all", "list every", "all properties", "all documents",
                "all jobs", "show me all", "show all", "every property",
                "every job", "portfolio", "what properties", "which properties",
                "what documents do we have"
            ])
            page_size = MAX_SEGMENTS_LIST if cached_is_list else MAX_SEGMENTS
            session.page_offset = session.page_offset + page_size
            if session.page_offset >= len(excerpts):
                # No more results
                answer_override = (
                    f"No more documents to show — you have already seen all "
                    f"{len(excerpts)} matching documents from the previous query."
                )
                session.history.append(ChatMessage(role="user",  text=query))
                session.history.append(ChatMessage(role="model", text=answer_override))
                session.last_active = time.time()
                return IntelligenceResponse(
                    answer=answer_override,
                    sources=list({e["source"] for e in excerpts}),
                    search_results=len(excerpts),
                    confidence="high",
                    job_context=session.job_context,
                    suggested_followups=[],
                    media_links=media_links,
                    source_uris=source_uris,
                )
            served_from_pagination = True
        else:
            detected = _extract_job_context(query)
            if detected:
                session.job_context = detected

            search_key = f"{session.job_context} {query}" if session.job_context else query

            excerpts, media_links, source_uris = [], [], {}
            now = time.time()
            cache = session.last_search
            cache_age_ok = (
                cache is not None
                and cache.query == search_key
                and (now - cache.cached_at) < (CACHE_MINUTES * 60)
            )

            if cache_age_ok:
                print(f"[Cache] Reusing Vertex results from {int(now - cache.cached_at)}s ago")
                excerpts    = cache.excerpts
                media_links = cache.media_links
                source_uris = cache.source_uris
            else:
                excerpts, media_links, source_uris = self.retrieve(
                    query, job_context=session.job_context)
                session.last_search = CachedSearch(
                    query=search_key,
                    excerpts=excerpts,
                    media_links=media_links,
                    source_uris=source_uris,
                    cached_at=now,
                )
                # Fresh search → reset pagination
                session.page_offset = 0

            _photo_words = ("photo", "photos", "picture", "pictures",
                            "image", "images", "show me")
            if any(w in query.lower() for w in _photo_words):
                extra = self._photo_lookup(session.job_context or query)
                seen = {m["url"] for m in media_links}
                media_links = media_links + [m for m in extra if m["url"] not in seen]

        answer = self.synthesize(
            query, excerpts, session,
            media_links=media_links,
            page_offset=session.page_offset,
        )

        session.history.append(ChatMessage(role="user",  text=query))
        session.history.append(ChatMessage(role="model", text=answer))
        session.last_active = time.time()

        has_direct = any(
            any(word in (exc.get("content") or "").lower()
                for word in query.lower().split()[:4])
            for exc in excerpts
        )
        confidence = _score_confidence(len(excerpts), has_direct)
        followups  = _suggest_followups(query, session.job_context)

        # Add "Show more" suggestion if results were truncated
        q_lower_for_limit = (
            session.last_search.query.lower() if session.last_search else query.lower()
        )
        cached_is_list = any(p in q_lower_for_limit for p in [
            "list all", "list every", "all properties", "all documents",
            "all jobs", "show me all", "show all", "every property",
            "every job", "portfolio", "what properties", "which properties",
            "what documents do we have"
        ])
        page_size_used = MAX_SEGMENTS_LIST if cached_is_list else MAX_SEGMENTS
        shown_so_far = session.page_offset + page_size_used
        if shown_so_far < len(excerpts):
            followups = [f"Show more results ({len(excerpts) - shown_so_far} remaining)"] + followups
            followups = followups[:3]

        sources    = list({exc["source"] for exc in excerpts})

        return IntelligenceResponse(
            answer=answer,
            sources=sources,
            search_results=len(excerpts),
            confidence=confidence,
            job_context=session.job_context,
            suggested_followups=followups,
            media_links=media_links,
            source_uris=source_uris,
        )

    def clear_session(self, session_id: str):
        session = self.get_session(session_id)
        if session:
            session.history.clear()
            session.job_context = None
            session.last_search = None


_intelligence: Optional[JobIntelligence] = None


def get_intelligence() -> JobIntelligence:
    global _intelligence
    if _intelligence is None:
        _intelligence = JobIntelligence()
    return _intelligence
