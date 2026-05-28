"""LangChain-backed answer pipeline — Phase 1 step 3 (retrieval-only).

This module is gated by `config/config.yaml` key `langchain.enabled`. When the
flag is true, `vertex/answer.py:answer()` delegates here instead of running its
own retrieval+synthesis. When false, this module is never imported (the import
in `vertex/answer.py` is lazy).

CURRENT SCOPE (step 3 of [[Infrastructure/17 Implementation Roadmap]] Phase 1):
  - Vertex retrieval is moved to `langchain_google_community.VertexAISearchRetriever`.
  - Synthesis (Gemini tool loop) is still delegated to the legacy
    `vertex.answer._gemini_answer_with_tools` for now. Step 4 will replace
    that with `langchain_google_genai.ChatGoogleGenerativeAI`. Step 5 wraps
    the whole flow in a LangGraph StateGraph.

DESIGN GOAL: zero behavioral change for end users. Retrieval should return
the same documents in the same order for the same query, just routed through
LangChain's wrapper. Verify on the existing test queries before moving on.

PUBLIC SURFACE:
  - answer(cfg, query, ...) -> Answer  (same signature as vertex.answer.answer)

Cross-references:
  - The legacy implementation: `vertex/answer.py`
  - The feature flag dispatcher: `vertex/answer.py:answer()` top-of-function
  - The retriever class docs: langchain_google_community.VertexAISearchRetriever
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import Config
from vertex.search import build_filter


def _doc_to_source(doc: Any, reference_id: str) -> dict:
    """Translate a LangChain Document to the existing source-dict shape.

    The downstream code (Gemini tool dispatcher, UI source panel, drafting
    pipeline) expects dicts with these specific keys. Keep the shape identical
    to `vertex.answer._vertex_search`'s output so nothing downstream needs to
    change.

    LangChain's VertexAISearchRetriever returns Document objects whose
    `metadata` dict mirrors the underlying Vertex `Document.struct_data` /
    `derived_struct_data`, plus a `page_content` field with the snippet text.
    Keys present in metadata vary by what was set during ingestion; we read
    defensively with `.get(...)` everywhere.
    """
    md = doc.metadata or {}

    # URI extraction mirrors vertex.search._extract_uri: try several known
    # locations because Vertex stores it in different places depending on
    # how the doc was ingested.
    uri = (
        md.get("source")           # LangChain convention
        or md.get("source_uri")    # our manifest convention
        or md.get("link")          # derived_struct_data convention
        or md.get("uri")
        or ""
    )

    # Title: prefer the explicit title/filename, else derive from URI.
    title = (
        md.get("title")
        or md.get("filename")
        or (Path(uri).name if uri else "")
        or md.get("id")
        or "Document"
    )

    snippet = (doc.page_content or "").strip()

    return {
        "reference_id": reference_id,
        "title":        title,
        "filename":     md.get("filename", title),
        "property":     str(md.get("property", "")),
        "category":     str(md.get("category", "")),
        "doc_type":     str(md.get("doc_type", "")),
        "uri":          uri,
        "snippet":      snippet,
    }


def _langchain_vertex_search(
    cfg: Config,
    query: str,
    property_=None,
    doc_type=None,
    category=None,
    person_name=None,
    address=None,
    project_id=None,
) -> list[dict]:
    """Vertex AI Search via LangChain. Returns same list-of-dicts shape as
    `vertex.answer._vertex_search` so callers don't need to change.

    The retriever is constructed per-call to keep credential/config handling
    centralized in `core.config`. If construction needs to be cached for
    perf reasons later, do it via an LRU on the args that actually matter
    (project_id, location, data_store_id, filter, query).
    """
    # Lazy import: don't pay the LangChain import cost when the feature
    # flag is off and this module is never reached.
    from langchain_google_community import VertexAISearchRetriever

    filter_expr = build_filter(
        property_, doc_type, category,
        person_name=person_name, address=address, project_id=project_id,
    )

    # VertexAISearchRetriever wraps the same Discovery Engine API the legacy
    # code calls directly. Parameters intentionally mirror the legacy
    # `_vertex_search` call shape:
    #   - project_id, location, data_store_id      -> identify the corpus
    #   - filter                                   -> our existing build_filter() string
    #   - max_documents=10                         -> matches legacy page_size=10
    #   - get_extractive_answers=False             -> snippet-only, no LLM summary
    #     (legacy uses snippet_spec.return_snippet=True with no answer_query)
    #   - max_extractive_segment_count=3           -> matches legacy max_snippet_count=3
    try:
        retriever = VertexAISearchRetriever(
            project_id=cfg.project_id,
            location_id=cfg.location,
            data_store_id=cfg.data_store_id,
            filter=filter_expr,
            max_documents=10,
            get_extractive_answers=False,
            max_extractive_segment_count=3,
            engine_data_type=0,  # 0 = unstructured (matches our content_config)
        )
    except Exception as e:
        # Fail soft to match legacy `_vertex_search` behavior — callers get
        # an empty list and the chat path still functions, just without
        # retrieved context.
        print(f"[answer_langchain] VertexAISearchRetriever init failed: {e}")
        return []

    try:
        docs = retriever.invoke(query)
    except Exception as e:
        print(f"[answer_langchain] retriever.invoke failed: {e}")
        return []

    # Dedupe by title (same logic as legacy _vertex_search).
    sources: list[dict] = []
    seen_titles: set[str] = set()
    for i, d in enumerate(docs, 1):
        src = _doc_to_source(d, reference_id=str(i))
        if src["title"] in seen_titles:
            continue
        seen_titles.add(src["title"])
        sources.append(src)

    return sources


def answer(
    cfg: Config,
    query: str,
    property_=None,
    doc_type=None,
    category=None,
    session=None,
    preamble=None,
    model_version: str = "",
):
    """LangChain-routed entry point. Same signature as `vertex.answer.answer`.

    Step 3 scope (retrieval-only): swap in `_langchain_vertex_search` for the
    Vertex call inside the legacy synthesis path. The Gemini synthesis itself
    is still the legacy tool-loop code in `vertex.answer` for now.

    We do this by temporarily substituting the legacy retrieval helper.
    Steps 4-5 will remove that indirection entirely.
    """
    # Import the legacy module here so steps 4-5 can swap pieces of it out
    # one at a time without circular imports at module load.
    from vertex import answer as legacy_answer
    from vertex.answer import (
        EXTRACT_PREAMBLE,
        Answer,
        _gemini_snippet_only,
        _gemini_answer_with_tools,
    )

    # Wire: replace the legacy _vertex_search with our LangChain version for
    # the duration of this call. Use a try/finally so we always restore even
    # if synthesis raises. We do NOT call legacy_answer.answer() (that would
    # re-enter the feature-flag dispatcher and infinite-loop); instead we
    # replicate its inner routing logic here.
    original_vertex_search = legacy_answer._vertex_search
    legacy_answer._vertex_search = _langchain_vertex_search
    try:
        if preamble == EXTRACT_PREAMBLE:
            text, sources = _gemini_snippet_only(
                cfg, query, preamble,
                property_=property_, doc_type=doc_type, category=category,
            )
        else:
            text, sources = _gemini_answer_with_tools(
                cfg, query, preamble,
                property_=property_, doc_type=doc_type, category=category,
            )

        public_sources = [
            {k: v for k, v in s.items() if k != "snippet"}
            for s in sources
        ]
        return Answer(text=text, citations=[], sources=public_sources, session=None)
    finally:
        legacy_answer._vertex_search = original_vertex_search
