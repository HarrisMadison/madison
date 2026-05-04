"""
Gemini-grounded answer — REFACTORED to skip Vertex answer_query (LLM quota burner).

Architecture:
  - Vertex AI Search → SNIPPET-ONLY retrieval (no summary_spec, no answer_query)
  - Gemini Flash    → all synthesis (cheap, fast)

This mirrors phase4/job_intelligence.py and saves discoveryengine LLM quota.
The public function signature is unchanged so web/app.py keeps working.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from core import search_client
from core.config import Config
from vertex.search import build_filter


# Preamble for template field extraction — forces short, value-only responses.
EXTRACT_PREAMBLE = """You are a precise data extraction assistant. Your job is to find and return ONLY the specific value requested.

STRICT RULES:
- Return ONLY the value (a number, name, date, dollar amount, or short phrase)
- Maximum 2 sentences. Prefer 1 sentence or less.
- Do NOT include explanations, context, disclaimers, caveats, or legal text
- Do NOT reproduce paragraphs from source documents
- Do NOT say "according to" or "the document states" — just give the value
- If the exact value is not found, return exactly: NOT FOUND
- For dollar amounts, return just the number: $185,000
- For dates, return just the date: June 22, 2022
- For names, return just the name: Shearwater Way LLC
- For EINs/IDs, return just the number: 82-1566754

Examples of GOOD responses:
- "$425.00"
- "82-1566754"
- "15 Northridge Dr, Coram, NY 11727"
- "Loan Funder LLC"
- "NOT FOUND"
"""

DEFAULT_SYSTEM_PROMPT = """You are a real estate and document intelligence assistant.
Answer using ONLY the document excerpts provided. Never invent addresses, dollar
amounts, dates, entity names, or lender details. If the excerpts do not answer
the question, say what you DID find and what is missing. Be direct and concise.
Cite the source filename in parentheses next to any specific fact."""


@dataclass
class Answer:
    text: str
    citations: list[dict]
    sources: list[dict]
    session: str | None = None


def _extract_snippets(doc) -> str:
    """Pull snippet text from derived_struct_data without triggering Vertex LLM."""
    try:
        if not getattr(doc, "derived_struct_data", None):
            return ""
        derived = dict(doc.derived_struct_data)
        snippets = derived.get("snippets", []) or []
        parts = []
        for s in snippets[:3]:
            if isinstance(s, dict):
                txt = s.get("snippet") or s.get("content") or ""
            else:
                txt = getattr(s, "snippet", "") or getattr(s, "content", "")
            if txt:
                txt = re.sub(r"<[^>]+>", "", str(txt)).strip()
                if txt:
                    parts.append(txt)
        return " ... ".join(parts)[:600]
    except Exception:
        return ""


def _extract_uri(doc) -> str:
    if getattr(doc, "content", None):
        u = getattr(doc.content, "uri", "") or ""
        if u:
            return u
    if getattr(doc, "derived_struct_data", None):
        d = dict(doc.derived_struct_data)
        for k in ("link", "uri", "source_uri"):
            if d.get(k):
                return d[k]
    if getattr(doc, "struct_data", None):
        s = dict(doc.struct_data)
        if s.get("source_uri"):
            return s["source_uri"]
    return ""


def _gemini_synthesize(query: str, sources: list[dict], preamble: str | None) -> str:
    """Call Gemini directly via google-generativeai. Falls back gracefully."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        # No key → return a deterministic listing instead of an error
        if not sources:
            return "No documents matched that query."
        names = ", ".join(s.get("title") or s.get("filename") or "doc" for s in sources[:5])
        return f"Found {len(sources)} document(s): {names}. (Set GEMINI_API_KEY for synthesized answers.)"

    try:
        import google.generativeai as genai
    except ImportError:
        return "google-generativeai package not installed. Run: pip install google-generativeai"

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").replace("models/", "")

    ctx_parts = []
    for i, s in enumerate(sources[:10], 1):
        label = s.get("title") or s.get("filename") or f"source-{i}"
        body = s.get("snippet") or "(no preview)"
        ctx_parts.append(f"[SOURCE {i} — {label}]\n{body}")
    context_block = "\n\n────\n\n".join(ctx_parts) if ctx_parts else "(no documents retrieved)"

    system_prompt = preamble or DEFAULT_SYSTEM_PROMPT
    user_prompt = (
        f"DOCUMENT EXCERPTS:\n{context_block}\n\n"
        f"────────────────\n"
        f"User's question: {query}"
    )

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name=model_name, system_instruction=system_prompt)
        resp = model.generate_content(user_prompt)
        return (resp.text or "").strip() or "Gemini returned an empty response."
    except Exception as e:
        if not sources:
            return f"No documents found, and Gemini errored: {e}"
        names = ", ".join(s.get("title") or "doc" for s in sources[:5])
        return f"Found {len(sources)} document(s): {names}. Gemini synthesis error: {e}"


def answer(cfg: Config, query: str, property_=None, doc_type=None,
           category=None, session=None, preamble=None,
           model_version: str = "") -> Answer:
    """
    Snippet-only Vertex search + Gemini synthesis. NO answer_query, NO summary_spec.
    `session` and `model_version` are accepted for backwards compatibility but ignored.
    """
    from google.cloud import discoveryengine_v1 as de

    client = search_client(cfg)
    filter_expr = build_filter(property_, doc_type, category)

    content_spec = de.SearchRequest.ContentSearchSpec(
        snippet_spec=de.SearchRequest.ContentSearchSpec.SnippetSpec(
            return_snippet=True,
            max_snippet_count=3,
        ),
        # NO summary_spec — does not invoke Vertex's LLM (saves quota)
    )

    req = de.SearchRequest(
        serving_config=cfg.search_serving_config,
        query=query,
        filter=filter_expr,
        page_size=10,
        content_search_spec=content_spec,
    )

    sources: list[dict] = []
    seen_titles: set[str] = set()

    try:
        resp = client.search(request=req)
        for r in resp.results:
            doc = r.document
            sd = dict(doc.struct_data) if doc.struct_data else {}
            if not sd and doc.derived_struct_data:
                sd = dict(doc.derived_struct_data)

            uri = _extract_uri(doc)
            title = (
                sd.get("title")
                or sd.get("filename")
                or (Path(uri).name if uri else "")
                or doc.id
                or "Document"
            )
            if title in seen_titles:
                continue
            seen_titles.add(title)

            sources.append({
                "reference_id": str(len(sources) + 1),
                "title":        title,
                "filename":     sd.get("filename", title),
                "property":     str(sd.get("property", "")),
                "category":     str(sd.get("category", "")),
                "doc_type":     str(sd.get("doc_type", "")),
                "uri":          uri,
                "snippet":      _extract_snippets(doc),
            })
    except Exception as e:
        # Vertex search failed → return a clean Answer instead of crashing the route
        return Answer(
            text=f"Document search failed: {e}",
            citations=[],
            sources=[],
            session=None,
        )

    text = _gemini_synthesize(query, sources, preamble)

    # Strip snippet field from sources before returning (UI doesn't need it)
    public_sources = [{k: v for k, v in s.items() if k != "snippet"} for s in sources]

    return Answer(text=text, citations=[], sources=public_sources, session=None)
