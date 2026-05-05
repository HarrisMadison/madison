"""Quick test of local index matching for various queries."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from local_index import get_index, _normalize

idx = get_index()

QUERIES = [
    "tell me about 106 madison avenue pdf",
    "106 madison avenue",
    "tell me about how to check if your motorcycle is docx",
    "how to check if your motorcycle is grounded",
    "show me the andover P&L",
    "andover p&l",
    "what's in 88 Rugby Drive appraisal",
]

for q in QUERIES:
    print("=" * 70)
    print(f"Query: {q!r}")
    print(f"Normalized: {_normalize(q)!r}")
    hits = idx.find(q, top_n=5)
    for h in hits:
        marker = "  ✓ STRONG" if h['score'] >= 100 else ""
        print(f"  score={h['score']:7.1f}  {h['name']}{marker}")
    print()
