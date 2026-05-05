"""
Gemini-grounded answer — REFACTORED to skip Vertex answer_query (LLM quota burner)
and now supports function-calling so Gemini can read full documents on demand.

Architecture:
  - Vertex AI Search → SNIPPET-ONLY retrieval (no summary_spec, no answer_query)
  - Gemini Flash    → all synthesis, with two tools available:
        1. search_documents(query)         — the existing snippet retrieval
        2. get_document_by_name(name)      — full-text fetch of a named file
    Gemini decides which tool fits the user's question and calls it. We loop
    until Gemini returns plain text instead of a tool call.

Why function calling instead of always pre-loading snippets:
    Old flow stuffed snippets into the prompt every time. Worked for "what
    permits do I have" but failed for "read the March invoice and tell me
    the total" — snippets miss the exact dollar amount. With tools, Gemini
    can choose to fetch the WHOLE file when a name is mentioned.

Public surface unchanged: web/app.py keeps calling `answer(cfg, query, ...)`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from core import search_client
from core.config import Config
from vertex.search import build_filter
from vertex.document_fetch import get_document_by_name


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

PRIMARY SOURCE: When the prompt contains a DOCUMENT EXCERPTS block, that is
your primary source of truth. Read it first and answer from it. List the
document names that appeared, even when they only partially answer the
question — the user wants to know which files were found.

You have an OPTIONAL tool: get_document_by_name. Only call it when ALL of
the following are true:
  - The user explicitly names an exact or near-exact filename, AND
  - The excerpts already in the prompt do NOT answer the question.
For general topical questions ('summarize claims status', 'what permits do
we have'), answer from the excerpts directly — DO NOT call the tool. If
you call the tool and it returns no match, fall back to the excerpts —
never say 'no documents were retrieved' when excerpts were provided.

There is also a search_documents tool, but the system already runs a search
and puts the results in the prompt. You usually do not need to call it again.

Answer rules:
  - Use ONLY the tool results and the excerpts. Do not invent dollar amounts,
    dates, names, addresses, or document contents.
  - Cite the source filename in parentheses next to specific facts.
  - If a fetched document or the excerpts do not contain the requested info,
    say so plainly and list the document names you DID see.
  - Be direct and concise — no preambles, no 'great question', no caveats."""


@dataclass
class Answer:
    text: str
    citations: list[dict]
    sources: list[dict]
    session: str | None = None


# ─── Vertex snippet retrieval (Path A — unchanged behavior) ──────────────────
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


def _vertex_search(cfg: Config, query: str, property_=None, doc_type=None,
                   category=None) -> list[dict]:
    """Snippet-only Vertex search. Returns list of source dicts with snippets."""
    from google.cloud import discoveryengine_v1 as de

    client = search_client(cfg)
    filter_expr = build_filter(property_, doc_type, category)

    content_spec = de.SearchRequest.ContentSearchSpec(
        snippet_spec=de.SearchRequest.ContentSearchSpec.SnippetSpec(
            return_snippet=True,
            max_snippet_count=3,
        ),
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
        # Fail soft — caller will see empty sources and report it
        print(f"[answer] Vertex search failed: {e}")
    return sources


# ─── Gemini tool declarations ────────────────────────────────────────────────
def _tool_declarations():
    """
    Return the two function tools as a `genai.protos.Tool` for the model.

    NOTE: imports inside the function so this module is importable even if
    google-generativeai is missing (e.g., during static analysis).
    """
    import google.generativeai as genai

    search_tool = genai.protos.FunctionDeclaration(
        name="search_documents",
        description=(
            "Run a semantic search across the entire indexed document corpus "
            "(DoorLoop folder, OneDrive content, every source Vertex has "
            "ingested). Returns short snippets from relevant documents. Use "
            "this for general questions, comparisons, or when no specific "
            "filename was mentioned."
        ),
        parameters=genai.protos.Schema(
            type=genai.protos.Type.OBJECT,
            properties={
                "query": genai.protos.Schema(
                    type=genai.protos.Type.STRING,
                    description=(
                        "The search query. Should be a few keywords or a "
                        "short phrase. Avoid pronouns and stop words."
                    ),
                ),
            },
            required=["query"],
        ),
    )

    get_doc_tool = genai.protos.FunctionDeclaration(
        name="get_document_by_name",
        description=(
            "OPTIONAL TOOL — only call when ALL of the following are true: "
            "(a) the user explicitly references a specific file by an exact "
            "or near-exact filename (e.g. 'open Northridge_Appraisal.pdf'), AND "
            "(b) other context already provided does not answer the question. "
            "DO NOT call for general topical questions like 'summarize the "
            "claims status' or 'tell me about X' — answer from the excerpts "
            "already provided. If unsure, do NOT call."
        ),
        parameters=genai.protos.Schema(
            type=genai.protos.Type.OBJECT,
            properties={
                "document_name": genai.protos.Schema(
                    type=genai.protos.Type.STRING,
                    description=(
                        "The filename or partial name of the document, as the "
                        "user referenced it. Example: 'March permit approval', "
                        "'ABC Plumbing invoice', 'Northridge appraisal'."
                    ),
                ),
            },
            required=["document_name"],
        ),
    )

    return genai.protos.Tool(function_declarations=[search_tool, get_doc_tool])


# ─── tool-call dispatcher ────────────────────────────────────────────────────
def _dispatch_tool(cfg: Config, name: str, args: dict,
                   accumulated_sources: list[dict],
                   property_=None, doc_type=None) -> str:
    """
    Execute one tool call from Gemini and return a string the model can read.

    Side-effect: appends any newly seen documents to `accumulated_sources` so
    the UI's source panel reflects everything that informed the answer.
    """
    if name == "search_documents":
        query = (args or {}).get("query", "").strip()
        if not query:
            return "search_documents error: query was empty"
        hits = _vertex_search(cfg, query, property_=property_, doc_type=doc_type)
        # Merge into accumulated sources, dedupe by title
        seen = {s["title"] for s in accumulated_sources}
        for h in hits:
            if h["title"] not in seen:
                accumulated_sources.append(h)
                seen.add(h["title"])
        if not hits:
            return f"search_documents: no matches for query={query!r}"
        # Format compactly for Gemini
        lines = [f"search_documents: found {len(hits)} document(s) for query={query!r}"]
        for i, h in enumerate(hits, 1):
            snip = h.get("snippet") or "(no preview)"
            lines.append(f"\n[{i}] {h['title']}\n{snip}")
        return "\n".join(lines)

    if name == "get_document_by_name":
        doc_name = (args or {}).get("document_name", "").strip()
        result = get_document_by_name(doc_name, cfg=cfg)
        if not result["ok"]:
            extras = ""
            if result.get("candidates"):
                extras = (f" Near-matches I do see: "
                          f"{', '.join(result['candidates'])}.")
            # Direct Gemini back to the snippets it already has so it does
            # NOT respond with 'no documents were retrieved'.
            return (
                f"get_document_by_name: no exact match for {doc_name!r}.{extras} "
                f"DO NOT tell the user 'no documents were retrieved'. Use the "
                f"snippet excerpts that were already provided in the prompt or "
                f"call search_documents with topical keywords. List the "
                f"document names you see in those excerpts in your answer."
            )
        # Add to sources list so UI download chip works
        title = result["title"]
        if title not in {s["title"] for s in accumulated_sources}:
            accumulated_sources.append({
                "reference_id": str(len(accumulated_sources) + 1),
                "title":        title,
                "filename":     title,
                "property":     "",
                "category":     "",
                "doc_type":     "",
                "uri":          result["uri"],
                "snippet":      "",  # full text was loaded; no snippet needed
            })
        body = result["text"] or "(empty document)"
        warn = ""
        if result.get("candidates"):
            warn = (f"\n\n[Note: name was ambiguous. Loaded {title!r}. "
                    f"Other matches: {', '.join(result['candidates'])}.]")
        return f"get_document_by_name OK — file: {title}\n\n{body}{warn}"

    return f"Unknown tool: {name}"


# ─── main entry: tool-using Gemini loop ──────────────────────────────────────
def _gemini_answer_with_tools(cfg: Config, query: str, preamble: str | None,
                              property_=None, doc_type=None,
                              category=None) -> tuple[str, list[dict]]:
    """
    Run a Gemini conversation with both tools available. Returns
    (final_answer_text, sources_list).

    Falls back to plain snippet-stuffing if google-generativeai is missing or
    GEMINI_API_KEY is unset, so the route never 500s for a config gap.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        # No key → fall back to old behavior: search and return list
        sources = _vertex_search(cfg, query, property_=property_,
                                 doc_type=doc_type, category=category)
        if not sources:
            return "No documents matched that query.", []
        names = ", ".join(s["title"] for s in sources[:5])
        return (f"Found {len(sources)} document(s): {names}. "
                f"(Set GEMINI_API_KEY for synthesized answers.)"), sources

    try:
        import google.generativeai as genai
    except ImportError:
        return ("google-generativeai package not installed. "
                "Run: pip install google-generativeai"), []

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").replace("models/", "")
    system_prompt = preamble or DEFAULT_SYSTEM_PROMPT

    try:
        genai.configure(api_key=api_key)
        tools = [_tool_declarations()]
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_prompt,
            tools=tools,
        )
    except Exception as e:
        # Fall back to snippet-only synthesis if model construction fails
        print(f"[answer] Gemini model init failed, falling back: {e}")
        return _gemini_snippet_only(cfg, query, preamble,
                                    property_=property_, doc_type=doc_type,
                                    category=category)

    # Open a chat so we can hand back tool results across turns
    chat = model.start_chat(enable_automatic_function_calling=False)
    accumulated_sources: list[dict] = []

    # Cap iterations so a misbehaving model can't loop forever
    MAX_ROUNDS = 6
    user_message: object = query

    final_text = ""
    for _round in range(MAX_ROUNDS):
        try:
            resp = chat.send_message(user_message)
        except Exception as e:
            return (f"Gemini error: {e}"), accumulated_sources

        # Gather all parts from the response
        try:
            parts = resp.candidates[0].content.parts
        except Exception:
            parts = []

        # Find any function calls in this turn
        function_calls = []
        text_parts = []
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc and getattr(fc, "name", ""):
                function_calls.append(fc)
            else:
                t = getattr(p, "text", "") or ""
                if t:
                    text_parts.append(t)

        if not function_calls:
            # Model produced a final answer
            final_text = "".join(text_parts).strip() or (resp.text or "").strip()
            break

        # Execute every tool call in this turn and feed all results back at once
        tool_responses = []
        for fc in function_calls:
            try:
                args = dict(fc.args) if fc.args else {}
            except Exception:
                args = {}
            tool_output = _dispatch_tool(
                cfg, fc.name, args, accumulated_sources,
                property_=property_, doc_type=doc_type,
            )
            tool_responses.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name,
                        response={"result": tool_output},
                    )
                )
            )
        # Next iteration: send ALL tool responses back together
        user_message = tool_responses
    else:
        # Hit the round cap without a final text
        final_text = ("(Hit tool-call iteration limit. Partial sources may be "
                      "available in the source panel.)")

    if not final_text:
        if accumulated_sources:
            names = ", ".join(s["title"] for s in accumulated_sources[:5])
            final_text = (f"Found {len(accumulated_sources)} document(s): {names}. "
                          f"Gemini returned an empty final response.")
        else:
            final_text = "No documents found and Gemini returned an empty response."

    return final_text, accumulated_sources


def _gemini_snippet_only(cfg: Config, query: str, preamble: str | None,
                         property_=None, doc_type=None,
                         category=None) -> tuple[str, list[dict]]:
    """Fallback path used by the EXTRACT_PREAMBLE template-fill flow and as a
    safety net when tool-using Gemini fails to initialize. This is the OLD
    pre-tool-calling behavior, preserved verbatim."""
    import google.generativeai as genai

    sources = _vertex_search(cfg, query, property_=property_,
                             doc_type=doc_type, category=category)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
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
        text = (resp.text or "").strip() or "Gemini returned an empty response."
        return text, sources
    except Exception as e:
        if not sources:
            return f"No documents found, and Gemini errored: {e}", []
        names = ", ".join(s.get("title") or "doc" for s in sources[:5])
        return (f"Found {len(sources)} document(s): {names}. "
                f"Gemini synthesis error: {e}"), sources


# ─── public entry point ──────────────────────────────────────────────────────
def answer(cfg: Config, query: str, property_=None, doc_type=None,
           category=None, session=None, preamble=None,
           model_version: str = "") -> Answer:
    """
    Entry point used by web/app.py and the drafting pipeline.

    Routing:
      - EXTRACT_PREAMBLE flow (template field extraction) → snippet-only path.
        We do NOT want function-calling for one-line value extraction; it adds
        latency and can confuse the model into citing instead of answering.
      - Everything else → tool-using Gemini loop with both search_documents
        and get_document_by_name available.

    `session` and `model_version` are accepted for backward compatibility but
    ignored (Vertex conversational path was removed earlier to save quota).
    """
    # Template field extraction: keep it simple and fast — no tools.
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

    # Strip snippet field before returning (UI doesn't need it)
    public_sources = [{k: v for k, v in s.items() if k != "snippet"}
                      for s in sources]

    return Answer(text=text, citations=[], sources=public_sources, session=None)
