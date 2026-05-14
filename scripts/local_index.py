"""
Local filename index — eliminates 95% of Vertex search calls.

Loads ALL filenames in the GCS bucket into memory at startup. When a user
asks about a specific file/property/address, we can match against the local
index and call get_document_by_name DIRECTLY, completely bypassing Vertex.

This is what enterprise RAG systems do at scale: pre-index aggressively,
let the expensive semantic search be a last resort.

Usage:
    from local_index import LocalFileIndex
    idx = LocalFileIndex()
    idx.load()                                  # one-time bucket walk (~10s)
    matches = idx.find("106 madison avenue")    # instant, no API call
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

# Auto-load .env so this module works even when imported from contexts that
# didn't already call load_dotenv (e.g. test scripts, CLI tools).
try:
    from dotenv import load_dotenv
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    for _env in (_REPO_ROOT / ".env", _REPO_ROOT / "Phase3_Bootstrap" / "secrets" / ".env"):
        if _env.exists():
            load_dotenv(_env)
except Exception:
    pass

from google.cloud import storage
from google.oauth2 import service_account

# Default service-account key location — used if env var is empty.
_DEFAULT_SA_KEY = str(
    Path(__file__).resolve().parent.parent / "Phase3_Bootstrap" / "secrets" / "service-account.json"
)


def _normalize(s: str) -> str:
    """Lower, strip ext, collapse separators. '106-Madison-Ave-' = '106 madison ave'.

    Replaces any non-alphanumeric character with a space, then collapses
    whitespace runs. This handles the punctuation that real-world folder
    and file names contain (commas, parentheses, ampersands, slashes,
    apostrophes) so that 'Pampinella, Giacomo - Legal' tokenizes to
    ['pampinella', 'giacomo', 'legal'] -- not ['pampinella,', 'giacomo',
    'legal'] which would prevent token-level matching against query word
    'pampinella'.

    Extension stripping is conservative: only the last segment after the
    final dot is removed, and only if it looks like a real extension (1-5
    alphanumeric chars). This prevents folder names like '1. Profit &
    Expense Tracking (P&L'S) - Unknown' from collapsing to just '1' (which
    Path.stem would do because it cuts at the LAST dot blindly).
    """
    if not s:
        return ""
    base = Path(s).name.lower()
    if "." in base:
        idx = base.rfind(".")
        suffix = base[idx + 1:]
        # Only treat as extension if suffix is 1-5 alphanumeric chars.
        # Catches .pdf, .docx, .xlsx, .jpg, .png, .pptx, .json. Skips
        # ' - Unknown', "'S)", etc.
        if 1 <= len(suffix) <= 5 and suffix.isalnum():
            base = base[:idx]
    cleaned = re.sub(r"[^a-z0-9]+", " ", base)
    return re.sub(r"\s+", " ", cleaned).strip()


# Filler words to strip from queries before matching. These dilute scoring
# without adding signal: "tell me about X" should match the same as just "X".
_QUERY_STOP_WORDS = {
    "tell", "me", "about", "show", "the", "a", "an", "please", "can",
    "you", "i", "want", "to", "see", "give", "what", "is", "are",
    "in", "on", "of", "for", "with", "and", "or", "that", "this",
    "my", "our", "any", "some", "all", "info", "information", "more",
    "file", "document", "docs", "pdf", "docx", "xlsx", "pptx",  # extension words
    "please", "thanks", "hi", "hello",
    "summary", "summarize", "read", "open", "contents", "content",
    "how", "do", "does", "its", "it", "if", "your",
}

# Words that may appear in folder names but are TOO GENERIC to use as
# anchors in pass-3 matching. These are category/document-type words that
# show up in only 1-3 folders by coincidence, but matching on them would
# produce false positives like 'show me all the photos' -> '1620 Old Cedar
# Swamp Advert Photos' or 'water damage claims' -> 'Claims closed 2024'.
# A word in this set is never accepted as a strong anchor in pass 3,
# regardless of how rare it is in the folder set.
_FOLDER_CATEGORY_NOISE_WORDS = {
    # Document type / category
    "photo", "photos", "picture", "pictures", "image", "images",
    "file", "files", "document", "documents", "docs",
    "invoice", "invoices", "receipt", "receipts",
    "permit", "permits", "inspection", "inspections",
    "contract", "contracts", "agreement", "agreements",
    "legal", "finance", "financial", "closing", "deed", "title",
    "appraisal", "appraisals", "estimate", "estimates",
    "claim", "claims", "insurance",
    "email", "emails", "correspondence",
    # Damage / job type
    "water", "fire", "smoke", "mold", "flood", "damage",
    "puffback", "asbestos", "lead", "emergency",
    # Status / state
    "unknown", "closed", "open", "pending", "active",
    "new", "old", "draft", "final", "signed", "completed",
    # Org structure
    "vendor", "vendors", "client", "clients", "customer", "customers",
    "property", "properties", "job", "jobs", "project", "projects",
    # Geography / category modifiers (often appear in folder NAMES but
    # users use them as descriptors, not folder identifiers)
    "vehicles", "funding", "payments", "tracking", "expense", "profit",
    "contracts", "signed",
}


def _strip_filler(norm_q: str) -> str:
    """Remove common stop words. Used to compute a 'core' query for scoring."""
    if not norm_q:
        return norm_q
    words = [w for w in norm_q.split() if w and w not in _QUERY_STOP_WORDS]
    return " ".join(words)


class LocalFileIndex:
    """In-memory filename → GCS URI lookup with fuzzy matching."""

    def __init__(self, bucket_name: Optional[str] = None,
                 prefix: str = "onedrive-mirror/",
                 sa_key: Optional[str] = None,
                 project: Optional[str] = None):
        self.bucket_name = bucket_name or os.getenv("GCS_BUCKET_NAME") or os.getenv("GCS_BUCKET_RAW", "")
        self.prefix      = prefix
        self.project     = project or os.getenv("GCP_PROJECT_ID", "")
        # Fall back to the known service-account path when the env var is empty.
        # Without this fallback, an empty env var causes 'No such file or
        # directory: \'\'' on the very first call.
        env_key = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        self.sa_key      = sa_key or env_key or _DEFAULT_SA_KEY
        self._files: list[tuple[str, str, str]] = []  # (normalized_name, real_name, gs_uri)
        # Set of unique property folder names discovered during the bucket
        # walk. Used by phase4 retrieval to detect when a query references a
        # known property/person folder so it can run a metadata-filtered
        # search (catches docs in the folder whose body text doesn't mention
        # the property name -- e.g. "Pack Out.pdf" inside /Pampinella/).
        self._property_folders: set[str] = set()
        # URI -> doc_type lookup, populated from manifests/doc_type_index.json
        # written by Phase5_oneDrive/onedrive_sync.py. This is Layer 4 in the
        # layered ranking model: the persisted classification result that
        # the ranker prefers over filename heuristics. Empty until the first
        # sync runs with content classification enabled.
        self._doc_type_by_uri: dict[str, str] = {}
        self._loaded = False
        self._loaded_at = 0.0

    def load(self, force: bool = False) -> int:
        """Walk the bucket once. Returns file count."""
        if self._loaded and not force:
            return len(self._files)
        creds = service_account.Credentials.from_service_account_file(
            self.sa_key, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        client = storage.Client(project=self.project, credentials=creds)
        bucket = client.bucket(self.bucket_name)
        files = []
        # Reset folder set on every (re)load so removed folders don't linger.
        property_folders: set[str] = set()
        # Folder discovery walks EVERY path segment of every file, not just
        # a single fixed depth. Real OneDrive layouts nest property/claim
        # folders at varying depths -- e.g. both
        #   onedrive-mirror/Doorloop/27 Manor Drive/files.pdf      (depth 2)
        # and
        #   onedrive-mirror/Claims In Process/Pampinella 2120 6th has REBUILD/Giacomo (Tenant ...)/Closing/file.pdf
        #                                                          (depths 2, 3, 4, 5)
        # both exist. A fixed-depth extractor only sees the first depth-2
        # segment and misses everything else.
        #
        # The distinctiveness filter in detect_property_in_query (Pass 3)
        # uses _FOLDER_CATEGORY_NOISE_WORDS plus a per-corpus rare-word
        # gate to suppress garbage segments like 'files', 'documents',
        # 'Job Documents', 'Outlook Files'. So walking every segment is
        # safe -- the noise is filtered at match time, not at index time.
        #
        # We skip:
        #   - the prefix portion of the path (onedrive-mirror/, etc.)
        #   - the last segment (always the file itself)
        prefix_parts = [p for p in self.prefix.strip("/").split("/") if p]
        prefix_depth = len(prefix_parts)
        t0 = time.time()
        for b in bucket.list_blobs(prefix=self.prefix):
            real_name = Path(b.name).name
            if not real_name:
                continue
            norm = _normalize(real_name)
            if norm:
                files.append((norm, real_name, f"gs://{self.bucket_name}/{b.name}"))
            # Extract every folder segment from the path. Cheap, runs in
            # the same loop -- no extra GCS calls. We walk from after the
            # prefix to before the filename, adding each segment as a
            # candidate folder name.
            parts = b.name.split("/")
            # parts[0:prefix_depth] is the prefix portion; parts[-1] is the
            # filename. Everything in between is a folder.
            for seg in parts[prefix_depth:-1]:
                candidate = seg.strip()
                if candidate and not candidate.startswith("."):
                    property_folders.add(candidate)
        self._files = files
        self._property_folders = property_folders
        self._loaded = True
        self._loaded_at = time.time()
        elapsed = self._loaded_at - t0
        print(f"[LocalFileIndex] Loaded {len(files)} filenames in {elapsed:.1f}s")
        print(f"[LocalFileIndex] Discovered {len(property_folders)} property folders")

        # Best-effort doc_type sidecar load. The file is written by
        # Phase5_oneDrive/onedrive_sync.py at the end of every successful
        # manifest build. Missing or malformed sidecar is non-fatal: the
        # ranker just falls back to filename-based classification (Layer 3)
        # for every file. So a freshly-deployed system with no sync run
        # yet still works -- it just doesn't get the Layer 4 boost.
        try:
            sidecar_blob = bucket.blob("manifests/doc_type_index.json")
            if sidecar_blob.exists():
                import json as _json
                raw = sidecar_blob.download_as_text()
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    # Defensive: only accept str -> str entries. Bad data
                    # (e.g. a stray nested dict from a future schema change)
                    # gets dropped silently rather than poisoning the ranker.
                    self._doc_type_by_uri = {
                        k: v for k, v in parsed.items()
                        if isinstance(k, str) and isinstance(v, str)
                    }
                    print(
                        f"[LocalFileIndex] Loaded doc_type sidecar: "
                        f"{len(self._doc_type_by_uri)} entries"
                    )
                else:
                    print("[LocalFileIndex] doc_type sidecar exists but is not a dict; ignoring")
            else:
                print("[LocalFileIndex] doc_type sidecar not present (filename-only ranking)")
        except Exception as e:
            print(f"[LocalFileIndex] doc_type sidecar load failed: {e}")
            self._doc_type_by_uri = {}

        return len(files)

    def get_doc_type(self, uri: str) -> str:
        """Return the persisted doc_type for a GCS URI, or empty string.

        Used by the query-time ranker to consult Layer 4 (persisted
        classification) before falling back to Layer 3 (filename hint).
        Returns "" rather than None so callers can do truthiness checks
        without needing an explicit None comparison.
        """
        if not uri:
            return ""
        return self._doc_type_by_uri.get(uri, "")

    def find(self, query: str, top_n: int = 5) -> list[dict]:
        """
        Fuzzy-match `query` against indexed filenames.

        Returns ranked list of {"name": str, "uri": str, "score": float}.
        """
        if not self._loaded:
            self.load()
        if not query or not query.strip():
            return []

        norm_q = _normalize(query)
        if not norm_q:
            return []

        # Build a 'core' query without filler words for scoring purposes.
        # 'tell me about 106 madison avenue pdf' → '106 madison avenue'
        # This makes ratios meaningful: 3 of 3 core words is a strong match;
        # 3 of 7 raw words looks weak.
        core_q = _strip_filler(norm_q)
        if not core_q:
            core_q = norm_q  # fallback if user typed only stop words

        q_words = set(norm_q.split())
        core_words = set(core_q.split())
        scored = []

        for norm_name, real_name, uri in self._files:
            score = self._score(norm_q, q_words, core_q, core_words, norm_name)
            if score > 0:
                scored.append({"name": real_name, "uri": uri, "score": score})

        scored.sort(key=lambda x: -x["score"])
        return scored[:top_n]

    # ── Property folder helpers ─────────────────────────────────────────
    def get_property_folders(self) -> set[str]:
        """Return the set of property/person folder names known to the index.

        Used by phase4 retrieval to recognize when a query references a
        known property folder (e.g. "Pampinella", "15-Northridge") so it
        can run a metadata-filtered Vertex search alongside the regular
        text search. Loads the index lazily if not already loaded.
        """
        if not self._loaded:
            self.load()
        return set(self._property_folders)

    def _build_folder_word_freq(self) -> dict:
        """Compute (and cache) a {word: count} frequency map across all
        folder names. Used by detect_property_in_query() to gauge how
        distinctive a query word is. Words appearing in many folders
        (e.g. 'legal', 'files') are weak match anchors. Words appearing
        in one or two folders (e.g. 'pampinella', 'northridge') are
        strong anchors.
        """
        cache = getattr(self, "_folder_word_freq_cache", None)
        if cache is not None:
            return cache
        freq: dict = {}
        for folder in self._property_folders:
            if not folder:
                continue
            for w in _normalize(folder).split():
                if not w:
                    continue
                freq[w] = freq.get(w, 0) + 1
        self._folder_word_freq_cache = freq
        return freq

    def detect_property_in_query(self, query: str) -> Optional[str]:
        """Return the property folder name that best matches the query.

        Designed for the real-world folder naming patterns in this bucket,
        which include things like:
          - 'Pampinella, Giacomo - Legal'
          - '0122-0001EWM - 1117 Aron Pl'
          - 'Vincent Bui - 162 N 7th St'
          - 'Weber, Kathy'
        Users typically only type ONE distinctive token ('pampinella',
        'weber', 'northridge') and expect that to find the folder.

        Matching strategy, in order of priority:
          PASS 1 -- Exact normalized match. Folder normalized equals
                    query normalized. Highest confidence.
          PASS 2 -- Folder name (lowercased) is a substring of the query.
                    Catches users who type the full folder name.
          PASS 3 -- Distinctive query word appears as a token in the folder
                    name. 'distinctive' means the word appears in only a
                    small fraction of total folders (rare -> high signal).
                    This is the path that catches 'pampinella' ->
                    'Pampinella, Giacomo - Legal'.
          PASS 4 -- Token-overlap full match: every distinctive word of
                    the folder is in the query. Conservative fallback for
                    users who type partial folder names.

        Returns the EXACT folder name (preserving original case/punctuation)
        suitable for use in a Vertex AI Search filter, or None if no match.
        """
        if not query or not query.strip():
            return None
        if not self._loaded:
            self.load()
        if not self._property_folders:
            return None

        q_lower = query.lower()
        q_norm = _normalize(query)
        # Filler-stripped form. Used as an alternate target for PASS 1
        # so that command-style queries like 'summarize 27 Manor Drive'
        # still find folder '27 Manor Drive' via exact match. Without
        # this, those queries fall through to PASS 2 substring (which
        # works for short folder names but is less reliable for long
        # messy ones because Re.escape on long strings with embedded
        # noise can sometimes fail the word-boundary check). Stripping
        # filler before equality is strictly more permissive: a query
        # with no filler is unchanged.
        q_norm_stripped = _strip_filler(q_norm)
        q_words = set(q_norm.split())
        # Words from the query that are 'interesting' -- long enough or
        # digit-bearing -- and not common stop words. These are the only
        # ones we'll consider as match anchors in pass 3.
        q_distinctive = {
            w for w in q_words
            if (w.isdigit() or any(c.isdigit() for c in w) or len(w) >= 4)
            and w not in _QUERY_STOP_WORDS
        }

        # PASS 1: exact normalized equality (raw OR filler-stripped).
        # We compare against both q_norm and q_norm_stripped so that:
        #   query 'summarize 27 Manor Drive'
        #   q_norm = 'summarize 27 manor drive'  (no PASS 1 match)
        #   q_norm_stripped = '27 manor drive'   (PASS 1 match)
        # without changing behavior for users who type just '27 manor drive'.
        for folder in self._property_folders:
            if not folder:
                continue
            f_norm = _normalize(folder)
            if f_norm and (f_norm == q_norm or f_norm == q_norm_stripped):
                return folder

        # PASS 2: folder name is a substring of the query
        # (User typed the full folder name verbatim or near-verbatim.)
        # Require word-boundary match so 2-3 char folders like 'IT' or
        # 'WHD' don't accidentally match inside longer query words like
        # 'permits' (contains 'it') or 'whdrawn' (contains 'whd').
        substring_hits: list[str] = []
        for folder in self._property_folders:
            if not folder:
                continue
            f_norm = _normalize(folder)
            if not f_norm:
                continue
            # Build word-boundary regex from the normalized folder name.
            # Escape any regex metachars (shouldn't exist after normalize
            # but cheap defense). Word boundary on both sides ensures
            # 'it' does not match 'permits'.
            pattern = r'(?<![a-z0-9])' + re.escape(f_norm) + r'(?![a-z0-9])'
            if re.search(pattern, q_norm):
                substring_hits.append(folder)
        if substring_hits:
            # Longest match wins -- more specific.
            return max(substring_hits, key=len)

        # PASS 3: distinctive query word appears in folder
        # This is the path that catches 'pampinella' ->
        # 'Pampinella, Giacomo - Legal'.
        if q_distinctive:
            freq = self._build_folder_word_freq()
            total_folders = max(len(self._property_folders), 1)
            # A query word is a STRONG ANCHOR only if it appears in a small
            # ABSOLUTE number of folders. This is much stricter than a
            # percentage threshold because category/damage-type words like
            # 'water', 'fire', 'legal', 'unknown' can each appear in
            # 10-60 folders and would otherwise pass a percentage check.
            # Empirically: in 1352-folder buckets, person/property names
            # appear in 1-3 folders; everything else is generic.
            #   pampinella  -> 1 folder  (very distinctive)
            #   weber       -> 2 folders
            #   northridge  -> 1 folder
            #   legal       -> 8 folders (NOT distinctive enough)
            #   water       -> 25 folders (definitely not distinctive)
            #   unknown     -> 64 folders (placeholder noise)
            # Floor of 3 keeps tiny buckets working; cap of 5 prevents
            # category words from sneaking in on larger buckets.
            max_allowed_freq = max(3, min(5, int(total_folders * 0.005)))

            # Score each folder by (a) how many strong-anchor words match,
            # (b) total distinctive words matched as tiebreak, (c) name
            # length as final tiebreak (longer name = more specific).
            best_score = (0, 0, 0)
            best_folder: Optional[str] = None
            for folder in self._property_folders:
                if not folder:
                    continue
                f_words = set(_normalize(folder).split())
                if not f_words:
                    continue
                # Words in BOTH the query (distinctive) and this folder.
                overlap = q_distinctive & f_words
                if not overlap:
                    continue
                # How many of those are STRONG anchors (rare in folder set
                # AND not a category/noise word)?
                strong = sum(
                    1 for w in overlap
                    if freq.get(w, 0) <= max_allowed_freq
                    and w not in _FOLDER_CATEGORY_NOISE_WORDS
                )
                if strong == 0:
                    # Folder matched only on common words like 'legal' or
                    # 'photos'. Skip -- too many false positives.
                    continue
                score = (strong, len(overlap), len(folder))
                if score > best_score:
                    best_score = score
                    best_folder = folder
                elif score == best_score and best_folder is not None:
                    # Deterministic tiebreak
                    if folder < best_folder:
                        best_folder = folder
            if best_folder:
                return best_folder

        # PASS 4: every distinctive folder word is in the query
        # Conservative fallback. Only fires when the query mentions every
        # distinctive word of the folder (e.g. query '15 northridge appraisal'
        # matching folder '15-Northridge' because {15, northridge} is a
        # subset of the query's word set).
        # Apply noise-word filter so a folder like 'Old Claims' doesn't
        # match query 'water damage claims' on the lone non-noise distinctive
        # word being 'claims'.
        for folder in self._property_folders:
            if not folder:
                continue
            f_words = set(_normalize(folder).split())
            if not f_words:
                continue
            f_distinctive = {w for w in f_words
                             if w.isdigit() or any(c.isdigit() for c in w)
                             or len(w) >= 4}
            if not f_distinctive:
                continue
            # Require at least one non-noise distinctive word so that
            # category-only folders ('Old Claims', 'Outlook Files') can't
            # match queries that just mention those category words.
            non_noise = {w for w in f_distinctive
                         if w not in _FOLDER_CATEGORY_NOISE_WORDS}
            if not non_noise:
                continue
            if f_distinctive.issubset(q_words):
                return folder

        # All four passes missed. Emit a diagnostic so future production
        # failures can be investigated without standalone simulators.
        # Bounded in length so the log line stays readable.
        q_preview = q_norm[:80] + ('...' if len(q_norm) > 80 else '')
        d_preview = ", ".join(sorted(q_distinctive))[:80]
        print(
            f"[LocalFileIndex] detect_property_in_query: NO MATCH "
            f"q_norm={q_preview!r} q_distinctive=[{d_preview}]"
        )
        return None

    def _score(self, norm_q: str, q_words: set,
               core_q: str, core_words: set,
               norm_name: str) -> float:
        if not norm_name:
            return 0.0
        if norm_name == norm_q or norm_name == core_q:
            return 1000.0
        # full substring match wins big (either direction) — try BOTH the
        # raw query AND the filler-stripped core query.
        if norm_q in norm_name or core_q in norm_name:
            # bonus for shorter (more specific) names
            return 100.0 + (50.0 / max(len(norm_name), 1))
        if norm_name in norm_q or norm_name in core_q:
            n_tokens = len(norm_name.split())
            return 100.0 + (10.0 * n_tokens)
        # token-overlap fallback: how many of the query words appear in the name
        n_words = set(norm_name.split())
        if not q_words or not n_words:
            return 0.0
        overlap = q_words & n_words
        if not overlap:
            return 0.0
        # require at least one "distinctive" word (digit-containing or 4+ chars)
        distinctive = [w for w in overlap if w.isdigit() or any(c.isdigit() for c in w) or len(w) >= 4]
        if not distinctive:
            return 0.0

        # STRONG-OVERLAP path: when the CORE query (filler-stripped) and the
        # filename share a high fraction of distinctive words, treat as strong.
        # This catches 'tell me about how to check if your motorcycle is docx'
        # vs. 'How to Check If Your Motorcycle Is Grounded Using a Multimeter.docx'.
        n_distinctive_in_name = [w for w in n_words if w.isdigit() or any(c.isdigit() for c in w) or len(w) >= 4]
        if n_distinctive_in_name:
            core_overlap_distinctive = (core_words & n_words) - _QUERY_STOP_WORDS
            core_distinctive = [w for w in core_overlap_distinctive
                                if w.isdigit() or any(c.isdigit() for c in w) or len(w) >= 4]
            if len(core_distinctive) >= 2:
                # Coverage = how much of the filename's content the core query covers
                name_distinctive_set = set(n_distinctive_in_name)
                covered = name_distinctive_set & set(core_distinctive)
                coverage = len(covered) / max(len(name_distinctive_set), 1)
                # Also: how much of core query is matched
                core_match_ratio = len(covered) / max(len(core_words), 1)

                # Strong if 50%+ filename coverage AND every core word matched
                if coverage >= 0.5 and core_match_ratio >= 0.7:
                    return 100.0 + (5.0 * len(covered)) + (20.0 * core_match_ratio)
                # Or strong if 40%+ coverage AND user typed 3+ specific words
                if coverage >= 0.4 and len(core_distinctive) >= 3:
                    return 100.0 + (5.0 * len(covered))

        ratio = len(overlap) / max(len(q_words), 1)
        return 30.0 * ratio + 5.0 * len(distinctive)


# Singleton — load once per process
_INDEX: Optional[LocalFileIndex] = None

def get_index() -> LocalFileIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = LocalFileIndex()
        try:
            _INDEX.load()
        except Exception as e:
            print(f"[LocalFileIndex] Load failed: {e}")
    return _INDEX


def reload_index() -> dict:
    """Force re-walk of the GCS bucket. Used after OneDrive sync drops new
    files into onedrive-mirror/. Returns {ok, file_count, elapsed_seconds, error?}."""
    global _INDEX
    import time as _time
    t0 = _time.time()
    try:
        if _INDEX is None:
            _INDEX = LocalFileIndex()
        count = _INDEX.load(force=True)
        return {
            "ok":               True,
            "file_count":       count,
            "elapsed_seconds":  round(_time.time() - t0, 2),
        }
    except Exception as e:
        return {
            "ok":               False,
            "error":            str(e),
            "elapsed_seconds":  round(_time.time() - t0, 2),
        }
