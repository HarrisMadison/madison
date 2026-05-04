"""Gemini-grounded answer with session support, preamble control, and concise extraction mode."""
from __future__ import annotations

from dataclasses import dataclass

from core import conversational_client
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

Examples of BAD responses (never do this):
- "The Employer Identification Number (EIN) from the IRS notice is 82-1566754. This EIN will identify the business accounts, tax returns..."
- "According to the appraisal report, the property at 15 Northridge..."
"""


@dataclass
class Answer:
    text: str
    citations: list[dict]
    sources: list[dict]
    session: str | None = None


def answer(cfg: Config, query: str, property_=None, doc_type=None,
           category=None, session=None, preamble=None,
           model_version: str = "gemini-2.0-flash-001/answer_gen/v1") -> Answer:
    from google.cloud import discoveryengine_v1 as de

    client = conversational_client(cfg)
    filter_expr = build_filter(property_, doc_type, category)

    # Build the answer generation spec with optional preamble.
    gen_spec = de.AnswerQueryRequest.AnswerGenerationSpec(
        ignore_adversarial_query=True,
        include_citations=True,
        model_spec=de.AnswerQueryRequest.AnswerGenerationSpec.ModelSpec(
            model_version=model_version,
        ),
    )

    # Add preamble if provided (used for concise extraction in templates).
    if preamble:
        gen_spec.prompt_spec = de.AnswerQueryRequest.AnswerGenerationSpec.PromptSpec(
            preamble=preamble,
        )

    req = de.AnswerQueryRequest(
        serving_config=cfg.search_serving_config,
        query=de.Query(text=query),
        session=session or None,
        search_spec=de.AnswerQueryRequest.SearchSpec(
            search_params=de.AnswerQueryRequest.SearchSpec.SearchParams(
                max_return_results=10,
                filter=filter_expr,
            ),
        ),
        answer_generation_spec=gen_spec,
    )

    resp = client.answer_query(request=req)
    a = resp.answer
    text = a.answer_text or ""
    session_name = resp.session.name if resp.session else None

    citations = []
    for c in a.citations:
        snippet = text[c.start_index:c.end_index]
        citations.append({
            "snippet": snippet,
            "reference_ids": [s.reference_id for s in c.sources],
        })

    sources = []
    for ref in a.references:
        di = ref.unstructured_document_info
        if di:
            sd = dict(di.struct_data) if di.struct_data else {}
            uri = getattr(di, 'uri', '') or ''
            if not uri:
                uri = sd.get('source_uri', '') or sd.get('link', '')
            sources.append({
                "reference_id": ref.reference_id,
                "title": di.title or sd.get("filename", ""),
                "property": sd.get("property", ""),
                "category": sd.get("category", ""),
                "doc_type": sd.get("doc_type", ""),
                "uri": uri,
            })

    return Answer(text=text, citations=citations, sources=sources, session=session_name)
