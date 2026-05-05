"""Direct test: can the fetcher actually read the motorcycle file?"""
import sys, os
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from vertex.document_fetch import get_document_by_name

# Try various phrasings the user might use
QUERIES = [
    "How to Check If Your Motorcycle Is Grounded Using a Multimeter.docx",
    "How to Check If Your Motorcycle Is Grounded Using a Multimeter",
    "motorcycle grounded multimeter",
    "motorcycle multimeter",
    "how to check motorcycle grounded",
    "tell me about how to check if your motorcycle is docx",
]

for q in QUERIES:
    print("=" * 70)
    print(f"Query: {q!r}")
    result = get_document_by_name(q)
    print(f"  ok:    {result.get('ok')}")
    print(f"  title: {result.get('title')}")
    if result.get('error'):
        print(f"  error: {result['error']}")
    text = result.get('text') or ""
    print(f"  text:  {len(text)} chars")
    if text:
        print(f"  preview: {text[:300]!r}")
    print()
