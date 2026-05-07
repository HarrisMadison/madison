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
    """Lower, strip ext, collapse separators. '106-Madison-Ave-' = '106 madison ave'."""
    if not s:
        return ""
    base = Path(s).name.lower()
    stem = Path(base).stem
    return re.sub(r"[\s_\-\.]+", " ", stem).strip()


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
        # Layout the metadata extractor expects:
        #   <mirror_prefix>/<properties_folder>/<property>/<category>/...
        # mirror_prefix is what self.prefix already targets (e.g.
        # "onedrive-mirror/"). The next segment is the properties container
        # ("Properties" or "Doorloop" depending on deployment). The segment
        # AFTER that is the property/person folder name we want.
        prefix_depth = len([p for p in self.prefix.strip("/").split("/") if p])
        property_segment_idx = prefix_depth + 1  # skip mirror_prefix + properties container
        t0 = time.time()
        for b in bucket.list_blobs(prefix=self.prefix):
            real_name = Path(b.name).name
            if not real_name:
                continue
            norm = _normalize(real_name)
            if norm:
                files.append((norm, real_name, f"gs://{self.bucket_name}/{b.name}"))
            # Extract the property folder name from the path. Cheap, runs
            # in the same loop -- no extra GCS calls.
            parts = b.name.split("/")
            if len(parts) > property_segment_idx + 1:
                # +1 because we need at least one more segment AFTER the
                # property folder (the file or a subfolder), confirming
                # the property segment is a real folder, not the file itself.
                candidate = parts[property_segment_idx].strip()
                # Skip empty strings and obvious non-folder noise.
                if candidate and not candidate.startswith("."):
                    property_folders.add(candidate)
        self._files = files
        self._property_folders = property_folders
        self._loaded = True
        self._loaded_at = time.time()
        elapsed = self._loaded_at - t0
        print(f"[LocalFileIndex] Loaded {len(files)} filenames in {elapsed:.1f}s")
        print(f"[LocalFileIndex] Discovered {len(property_folders)} property folders")
        return len(files)

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

    def detect_property_in_query(self, query: str) -> Optional[str]:
        """Return the property folder name that best matches the query.

        Matching strategy:
          1. Exact-substring match (case-insensitive) on either the raw or
             normalized form of the folder name. Wins outright.
          2. Token-overlap fallback: every distinctive word in the folder
             name must appear in the query. Catches folder names with
             punctuation/casing differences (e.g. "15-Northridge" matched
             by "15 northridge").

        Returns the EXACT folder name (preserving original case/punctuation)
        suitable for use in a Vertex AI Search filter, or None if no match.
        Returning the exact folder name matters because Vertex's
        `property: ANY("...")` filter is exact-match.
        """
        if not query or not query.strip():
            return None
        if not self._loaded:
            self.load()
        if not self._property_folders:
            return None

        q_lower = query.lower()
        q_norm = _normalize(query)
        q_words = set(q_norm.split())

        # Pass 1: substring match. Prefer the longest match so
        # "15-Northridge" beats "15" if both happen to be folder names.
        substring_hits: list[str] = []
        for folder in self._property_folders:
            if not folder:
                continue
            f_lower = folder.lower()
            f_norm = _normalize(folder)
            # Direct substring (handles "pampinella" matching "Pampinella"
            # and "15 northridge" matching "15-Northridge" via norm).
            if f_lower in q_lower or (f_norm and f_norm in q_norm):
                substring_hits.append(folder)
        if substring_hits:
            # Longest match wins -- more specific.
            return max(substring_hits, key=len)

        # Pass 2: token-overlap fallback. Require ALL distinctive words of
        # the folder name to appear in the query. Avoids false positives
        # like matching folder "15-Northridge" on a query that only says
        # "northridge" (we want the user to be specific).
        for folder in self._property_folders:
            if not folder:
                continue
            f_norm = _normalize(folder)
            f_words = set(f_norm.split())
            if not f_words:
                continue
            # Distinctive = digit-bearing or 4+ chars. Skips "st", "of".
            f_distinctive = {w for w in f_words
                             if w.isdigit() or any(c.isdigit() for c in w)
                             or len(w) >= 4}
            if not f_distinctive:
                # Folder name is all short/common words -- skip. Substring
                # pass would have caught it if appropriate.
                continue
            if f_distinctive.issubset(q_words):
                return folder
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
