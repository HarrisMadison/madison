# PATCH: Named-Document Fetch (Path B)

**Date:** May 2026
**Problem:** When a user typed "read the invoice from ABC Plumbing" or
"summarize the March permit approval", Gemini received only short Vertex
search snippets — never the full document — so it frequently missed the
specific data the user asked for (totals, dates, line items).

**Fix:** Added a second retrieval path. Gemini now has a function-calling
tool — `get_document_by_name` — that resolves a filename against the live
Vertex index, downloads the full file from GCS, extracts all the text, and
hands it back. Gemini decides per-question whether to use snippet search
(general questions) or full-document fetch (named files).

---

## Files changed

| File | What changed |
|---|---|
| `vertex/document_fetch.py` | **NEW.** Shared resolver + GCS fetcher + multi-format text extractor (PDF/DOCX/XLSX/PPTX/TXT). One source of truth, used by every chat path. |
| `vertex/answer.py` | Wired Gemini function-calling. Both `search_documents` and `get_document_by_name` are now exposed as tools. The template-extraction flow (`EXTRACT_PREAMBLE`) still uses the simpler snippet-only path. |
| `phase4/job_intelligence.py` | Same tool wiring on the richer agent path (session memory / pagination / photos). Imports the shared fetcher from `vertex.document_fetch`. |
| `scripts/job_intelligence.py` | Same wiring as phase4 so this duplicate doesn't silently rot. |
| `web/app.py` | Added `GET /api/document?name=...` for direct curl-testing of the fetcher in isolation, bypassing Gemini. |

`.env` was already on `gemini-2.5-flash` — no changes needed there.

---

## Why the long-term shape is right

- **Resolution runs against the LIVE Vertex index.** No hardcoded folder
  listing. As the OneDrive sync ingests new files, they become reachable
  immediately — no code changes in the fetcher.
- **One shared module** (`vertex/document_fetch.py`) is imported by every
  Gemini path. Bugs get fixed in one place, every chat surface benefits.
- **Multi-format extractor** is already in place: PDF, DOCX, XLSX, PPTX,
  TXT/MD/CSV. Plug-in slots make it easy to add more (PNG → OCR, etc.)
  later when you wire in Document AI.
- **Future drafting / comparison / templating** can reuse the fetcher
  directly — `get_document_by_name("appraisal A")` and `("appraisal B")`
  gives Gemini both full bodies in one prompt for side-by-side analysis.

---

## How to test (in order — don't skip steps)

### 1. Direct fetcher test (bypasses Gemini entirely)

Pick a filename you know is indexed. Run the dev server, then:

```powershell
# meta-only check — confirms the resolver finds it and reports a URI
curl "http://localhost:5000/api/document?name=YOUR-FILENAME-HERE&meta=1"

# full text — confirms GCS fetch + text extraction work end-to-end
curl "http://localhost:5000/api/document?name=YOUR-FILENAME-HERE"
```

Expected for `meta=1`:
```json
{
  "ok": true,
  "title": "actual-matched-filename.pdf",
  "uri": "gs://madison-rag-60-rag-raw/...",
  "char_count": 4827,
  "candidates": ["other-near-match-1.pdf", "..."],
  "error": null
}
```

If `ok` is `false`:
- `error` says "No indexed document matches ..." → the file isn't in the
  Vertex index. Run an OneDrive sync.
- `error` says "Matched ... but it has no source URI" → the manifest is
  missing `source_uri` for that doc. Re-run `scripts/index.py`.
- `error` says "GCS object not found" → bucket/prefix mismatch between
  what's in Vertex vs what's in GCS. Check the manifest.

### 2. End-to-end test through the chat UI

Start the web server, open the chat, and try:

> Read [exact filename you tested above] and tell me the total amount.

**Expected:** Gemini calls `get_document_by_name`, the answer cites the
specific dollar amount, and the source panel shows that filename with a
working download chip.

**Wrong behavior to watch for:** vague answer, "I couldn't find...", or
the source panel filled with unrelated documents. That means the model
chose `search_documents` instead of the new tool — which means the user
didn't say a name strongly enough OR the tool description needs more
rule examples. Make the test prompt more explicit ("read the file named X").

### 3. Verify general questions still work

Run a non-named query like:

> What permits do we have on file?

This should still go through `search_documents` (the snippet path) and
produce the same kind of answer it did before. The new tool should NOT
fire here.

### 4. Confirm phase4 path also wired (the dark `/bob` UI)

If you launch with `python scripts/simple_web.py` instead of `scripts/web.py`,
you get the dark-themed chat UI at `http://localhost:5000/bob` (the legacy
name for `/chat`). On first request you should see in the server log:

  `[Phase4] Gemini synthesis ON (gemini-2.5-flash) with tools`

The `with tools` suffix confirms the new `get_document_by_name` tool is
registered. If it says `no tools` instead, the shared fetcher import failed —
check the line above for `[Phase4] document_fetch unavailable: ...`.

The two launchers serve different UIs from the same codebase:
  - `scripts/web.py`        → blue/white SMB Search UI at `/`
                              (uses `vertex/answer.py` for chat)
  - `scripts/simple_web.py` → dark chat UI at `/bob` (and `/chat`)
                              (uses `phase4/job_intelligence.py` for chat)

My patches updated BOTH paths, so `get_document_by_name` works in either UI.

---

## Known limits

- **Scanned PDFs without OCR** still come back empty. The extractor
  detects this and returns a clear error message instead of crashing.
  When Document AI is wired up, point the OCR step at the same fetcher
  to enrich GCS bytes.
- **Non-GCS sources** (https URLs, etc.) are not fetched — only `gs://`
  URIs are pulled. Everything in the OneDrive pipeline lands in GCS, so
  this is fine for your setup.
- **MAX_CHARS = 200,000.** Documents longer than that get truncated with
  a clear "[... truncated ...]" marker. Bump in `vertex/document_fetch.py`
  if you regularly hit it.
- **Tool-loop iteration cap = 6 rounds** in `answer.py` (5 in phase4).
  Prevents runaway loops if Gemini keeps calling tools without producing
  a final answer.

---

## Rollback

```powershell
cd C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve
git diff HEAD             # review the changes
git checkout HEAD -- vertex/answer.py phase4/job_intelligence.py scripts/job_intelligence.py web/app.py
git clean -f vertex/document_fetch.py PATCH_doc_fetch_NOTES.md
```

The pre-patch state is exactly your last commit before this work.
