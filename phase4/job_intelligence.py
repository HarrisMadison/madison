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
    """Pull snippet text from derived_struct_data without triggering LLM quota."""
    snippet_text = ""
    try:
        if not hasattr(doc, "derived_struct_data") or doc.derived_struct_data is None:
            return ""
        derived = doc.derived_struct_data
        try:
            derived_dict = dict(derived)
        except Exception:
            derived_dict = {}

        snippets = derived_dict.get("snippets", [])
        if snippets:
            parts = []
            for s in snippets[:3]:
                try:
                    if isinstance(s, dict):
                        text = s.get("snippet", "") or s.get("content", "")
                    else:
                        text = getattr(s, "snippet", "") or getattr(s, "content", "")
                    if text:
                        text = re.sub(r"<[^>]+>", "", str(text))
                        parts.append(text.strip())
                except Exception:
                    continue
            snippet_text = " ... ".join(parts)
    except Exception:
        pass
    return snippet_text[:600]


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

        self._gemini = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT,
        )
        print(f"[Phase4] Gemini synthesis ON ({GEMINI_MODEL})")

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

        def _make_req(q, flt=""):
            kw = dict(
                serving_config=self._serving_config,
                query=q,
                page_size=MAX_RESULTS,
                content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                    snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                        return_snippet=True,
                        max_snippet_count=3,
                    ),
                    # NOTE: NO summary_spec → does not invoke discoveryengine LLM
                ),
            )
            if flt:
                kw["filter"] = flt
            return discoveryengine.SearchRequest(**kw)

        try:
            response = self._search_client.search(
                _make_req(search_query, 'NOT document_type: ANY("scanned_document")'))
            results = list(response)
            if not results:
                response = self._search_client.search(_make_req(search_query))
                results = list(response)
        except Exception as e:
            err_msg = str(e)
            print(f"[Vertex] Search error: {err_msg[:200]}")
            try:
                response = self._search_client.search(_make_req(search_query))
                results = list(response)
            except Exception as e2:
                print(f"[Vertex] Retry error: {str(e2)[:200]}")
                return [], [], {}

        excerpts = []
        media_links = []
        source_uris = {}

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

            if not snippet_content:
                snippet_content = title or source_label

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

        history = []
        if session and session.history:
            recent = session.history[-(MAX_HISTORY * 2):]
            for msg in recent:
                history.append({"role": msg.role, "parts": [msg.text]})

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
            f"DOCUMENT EXCERPTS:\n{context_block}\n\n"
            f"{'─' * 40}{job_hint}\n"
            f"{media_hint}\n\n"
            f"User's question: {query}"
        )

        try:
            chat = self._gemini.start_chat(history=history)
            response = chat.send_message(prompt)
            return response.text
        except Exception as e:
            print(f"[Gemini] Synthesis error: {e}")
            if excerpts:
                doc_list = ", ".join(e["source"] for e in excerpts[:5])
                return (f"I found {len(excerpts)} relevant document(s): {doc_list}. "
                        "Gemini hit an error generating the full answer — please try again.")
            return ("I couldn't find relevant documents for that question. "
                    "Try a more specific query (address, permit number, or document name).")

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
