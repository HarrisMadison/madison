"""
Quick health probe. Tests Flask /api/status with a 5-sec timeout to see
if the server is actually answering or hung. Run while simple_web.py
is running in the other window.

    python scripts/probe_flask.py
"""
import urllib.request
import urllib.error
import json
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

URL_BASE = "http://localhost:5000"

def probe(path, method="GET", body=None, timeout=5):
    print(f"\n{'-' * 60}")
    print(f" {method} {URL_BASE}{path}  (timeout={timeout}s)")
    print(f"{'-' * 60}")
    t0 = time.time()
    try:
        req = urllib.request.Request(URL_BASE + path, method=method)
        req.add_header("Content-Type", "application/json")
        data = body.encode("utf-8") if body else None
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            elapsed = time.time() - t0
            body_text = resp.read().decode("utf-8", errors="replace")
            print(f"  Status: {resp.status} OK")
            print(f"  Time:   {elapsed:.2f}s")
            try:
                obj = json.loads(body_text)
                print(f"  Body keys: {list(obj.keys())[:8]}")
                if "vertex_ok" in obj:
                    print(f"  vertex_ok: {obj['vertex_ok']}")
                if "vertex_error" in obj and obj.get("vertex_error"):
                    print(f"  vertex_error: {obj['vertex_error'][:120]}")
                if "_cached" in obj:
                    print(f"  _cached: {obj['_cached']} (age {obj.get('_cache_age_seconds')}s)")
            except Exception:
                print(f"  Body (non-JSON): {body_text[:200]}")
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"  HTTP error: {e.code} after {elapsed:.2f}s")
        print(f"  Body: {body_text[:300]}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  FAILED after {elapsed:.2f}s: {type(e).__name__}: {e}")

print("=" * 60)
print(" FLASK HEALTH PROBE")
print("=" * 60)

# 1. Trivial GET — should return instantly
probe("/api/status", timeout=5)

# 2. Trivial POST — creates a session, NO Vertex call, should also be instant
probe("/api/chat/new", method="POST", body="{}", timeout=5)

# 3. Real chat — tests Vertex. Only do this if 1 and 2 worked.
print("\n" + "=" * 60)
print(" Now the real test: a chat call. Will time out at 30s.")
print(" If this hangs, Vertex is the problem. If 1 & 2 already")
print(" hung, Flask itself is the problem.")
print("=" * 60)
probe("/api/chat", method="POST",
      body='{"query":"andover","session_id":""}',
      timeout=30)

print("\nDone.")
