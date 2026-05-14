"""Schema-validation + persistence-sink test for structured_summary.

Hits the local Flask API at http://localhost:5000 and verifies:
  - structured_summary on the chat response conforms to the schema contract
  - the JSONL sink at data/structured_summaries/structured_summary_events.jsonl
    receives one row per folder-aware response and zero rows for factual Q&A

Designed to be re-run after backend changes as a regression check.

Usage:
    Make sure the Flask server is running, then from the repo root:
        python scripts/test_structured_summary_schema.py

Exits 0 if all checks pass, non-zero otherwise.
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

API_BASE = "http://localhost:5000"

# Repo root is the parent of this script's directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
PERSIST_FILE = REPO_ROOT / "data" / "structured_summaries" / "structured_summary_events.jsonl"

# The 18 keys every structured_summary must have, per the schema contract
# documented in job_intelligence.py. If this list changes, the schema
# contract has changed -- bump _STRUCTURED_SUMMARY_SCHEMA_VERSION on the
# backend and update this list to match.
REQUIRED_KEYS = {
    "schema_version",
    "response_kind",
    "generated_at",
    "query",
    "folder_name",
    "folder_purpose",
    "checklist_name",
    "file_count_total",
    "file_count_in_dossier",
    "overview",
    "key_facts",
    "timeline",
    "observations",
    "document_inventory",
    "open_items",
    "show_open_items",
    "sources",
    "confidence",
    "structured_fields",
}

# Expected schema version (bumped to 1.1 when structured_fields landed).
EXPECTED_SCHEMA_VERSION = "1.1"

# The 16 keys structured_fields must always have, in any record.
STRUCTURED_FIELDS_KEYS = {
    "property_address", "folder_name", "folder_purpose",
    "appraised_value", "appraisal_effective_date",
    "insurance_carrier", "claim_number",
    "estimate_total", "invoice_total", "inspection_date",
    "contract_status", "insurance_status", "estimate_status",
    "invoice_status", "inspection_status", "photos_status",
}

# Allowed enum values, mirrored from the backend module constants.
VALID_RESPONSE_KINDS = {"folder_summary", "open_items_only", "open_items_unknown"}
VALID_FOLDER_PURPOSES = {"claim_restoration", "property_appraisal", "unknown"}
VALID_CHECKLIST_NAMES = {"claim_default", "property_default", "unknown"}
VALID_OPEN_ITEM_STATUSES = {"found", "needs_review", "not_found"}


def _post(path: str, body: dict) -> dict:
    """POST JSON to the local API, return parsed JSON response. Raises on HTTP error."""
    req = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"\nERROR: cannot reach {API_BASE + path}")
        print(f"       Is the Flask server running? ({e})")
        sys.exit(2)


def _validate_schema(ss: Optional[dict], label: str, expected_kind: str) -> bool:
    """Validate one structured_summary payload. Returns True iff all checks pass."""
    print(f"\n  [{label}]")
    if ss is None:
        print(f"    FAIL: structured_summary is null (expected an object)")
        return False
    if not isinstance(ss, dict):
        print(f"    FAIL: structured_summary is {type(ss).__name__}, expected dict")
        return False

    ok = True

    # 1. Required keys ------------------------------------------------
    missing = REQUIRED_KEYS - set(ss.keys())
    extras = set(ss.keys()) - REQUIRED_KEYS
    if missing:
        print(f"    FAIL: missing required keys: {sorted(missing)}")
        ok = False
    else:
        print(f"    OK   all 18 required keys present")
    if extras:
        # Extras are not failures (forward-compat), just noted.
        print(f"    note: extra keys (forward-compatible): {sorted(extras)}")

    # 2. Provenance ---------------------------------------------------
    if ss.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        print(f"    FAIL: schema_version={ss.get('schema_version')!r}, "
              f"expected {EXPECTED_SCHEMA_VERSION!r}")
        ok = False
    else:
        print(f"    OK   schema_version={EXPECTED_SCHEMA_VERSION!r}")

    if ss.get("response_kind") != expected_kind:
        print(f"    FAIL: response_kind={ss.get('response_kind')!r}, "
              f"expected {expected_kind!r}")
        ok = False
    else:
        print(f"    OK   response_kind={expected_kind!r}")

    gen = ss.get("generated_at", "")
    if not (isinstance(gen, str) and len(gen) >= 19 and gen.endswith("Z")):
        print(f"    FAIL: generated_at={gen!r} (expected ISO-8601 with Z suffix)")
        ok = False
    else:
        print(f"    OK   generated_at={gen!r}")

    # 3. Enum coercion ------------------------------------------------
    fp = ss.get("folder_purpose")
    if fp not in VALID_FOLDER_PURPOSES:
        print(f"    FAIL: folder_purpose={fp!r} not in {VALID_FOLDER_PURPOSES}")
        ok = False
    else:
        print(f"    OK   folder_purpose={fp!r}")

    cn = ss.get("checklist_name")
    if cn not in VALID_CHECKLIST_NAMES:
        print(f"    FAIL: checklist_name={cn!r} not in {VALID_CHECKLIST_NAMES}")
        ok = False
    else:
        print(f"    OK   checklist_name={cn!r}")

    # 4. List shapes --------------------------------------------------
    # key_facts: each must have {label, value, confidence, sources}
    for i, kf in enumerate(ss.get("key_facts") or []):
        if not isinstance(kf, dict):
            print(f"    FAIL: key_facts[{i}] not a dict")
            ok = False
            continue
        required = {"label", "value", "confidence", "sources"}
        missing_kf = required - set(kf.keys())
        if missing_kf:
            print(f"    FAIL: key_facts[{i}] missing {missing_kf}")
            ok = False
    if ss.get("key_facts"):
        print(f"    OK   key_facts[*] shape ({len(ss['key_facts'])} entries)")

    # timeline: each must have {date, event, confidence, sources}
    for i, tl in enumerate(ss.get("timeline") or []):
        if not isinstance(tl, dict):
            print(f"    FAIL: timeline[{i}] not a dict")
            ok = False
            continue
        required = {"date", "event", "confidence", "sources"}
        missing_tl = required - set(tl.keys())
        if missing_tl:
            print(f"    FAIL: timeline[{i}] missing {missing_tl}")
            ok = False
    if ss.get("timeline"):
        print(f"    OK   timeline[*] shape ({len(ss['timeline'])} entries)")

    # document_inventory: each item must have {name, uri, doc_type, bucket}
    inv = ss.get("document_inventory") or {}
    inv_item_count = 0
    for bucket, items in inv.items():
        if not isinstance(items, list):
            print(f"    FAIL: document_inventory[{bucket!r}] not a list")
            ok = False
            continue
        for i, it in enumerate(items):
            inv_item_count += 1
            if not isinstance(it, dict):
                print(f"    FAIL: document_inventory[{bucket!r}][{i}] not a dict")
                ok = False
                continue
            required = {"name", "uri", "doc_type", "bucket"}
            missing_inv = required - set(it.keys())
            if missing_inv:
                print(f"    FAIL: document_inventory[{bucket!r}][{i}] missing {missing_inv}")
                ok = False
            if it.get("bucket") != bucket:
                print(f"    FAIL: document_inventory[{bucket!r}][{i}].bucket "
                      f"is {it.get('bucket')!r}, expected {bucket!r}")
                ok = False
    if inv_item_count:
        print(f"    OK   document_inventory[*] shape "
              f"({inv_item_count} items across {len(inv)} buckets)")

    # open_items: each must have {label, bucket, status, strict_count,
    # total_count, checklist_name}; status in allowed set
    for i, oi in enumerate(ss.get("open_items") or []):
        if not isinstance(oi, dict):
            print(f"    FAIL: open_items[{i}] not a dict")
            ok = False
            continue
        required = {"label", "bucket", "status", "strict_count",
                    "total_count", "checklist_name"}
        missing_oi = required - set(oi.keys())
        if missing_oi:
            print(f"    FAIL: open_items[{i}] missing {missing_oi}")
            ok = False
        if oi.get("status") not in VALID_OPEN_ITEM_STATUSES:
            print(f"    FAIL: open_items[{i}].status={oi.get('status')!r} "
                  f"not in {VALID_OPEN_ITEM_STATUSES}")
            ok = False
    if ss.get("open_items"):
        print(f"    OK   open_items[*] shape ({len(ss['open_items'])} entries)")

    # sources: each must have {title, uri, subfolder}
    for i, s in enumerate(ss.get("sources") or []):
        if not isinstance(s, dict):
            print(f"    FAIL: sources[{i}] not a dict")
            ok = False
            continue
        required = {"title", "uri", "subfolder"}
        missing_s = required - set(s.keys())
        if missing_s:
            print(f"    FAIL: sources[{i}] missing {missing_s}")
            ok = False
    if ss.get("sources"):
        print(f"    OK   sources[*] shape ({len(ss['sources'])} entries)")

    # structured_fields: must have all 16 keys, each a {value, confidence,
    # source_file} triple. value may be None when not extracted.
    sf = ss.get("structured_fields")
    if not isinstance(sf, dict):
        print(f"    FAIL: structured_fields is {type(sf).__name__}, expected dict")
        ok = False
    else:
        sf_missing = STRUCTURED_FIELDS_KEYS - set(sf.keys())
        sf_extras  = set(sf.keys()) - STRUCTURED_FIELDS_KEYS
        if sf_missing:
            print(f"    FAIL: structured_fields missing keys: {sorted(sf_missing)}")
            ok = False
        else:
            print(f"    OK   structured_fields has all 16 keys")
        if sf_extras:
            print(f"    note: structured_fields extra keys: {sorted(sf_extras)}")
        # Every value must be a {value, confidence, source_file} triple
        triple_keys = {"value", "confidence", "source_file"}
        bad_shape = []
        for k, v in sf.items():
            if not isinstance(v, dict) or set(v.keys()) != triple_keys:
                bad_shape.append(k)
        if bad_shape:
            print(f"    FAIL: structured_fields wrong shape on: {bad_shape}")
            ok = False
        else:
            print(f"    OK   structured_fields[*] shape (value, confidence, source_file)")
        # Count extracted vs null for a quick read of how much got pulled
        extracted = sum(1 for v in sf.values()
                        if isinstance(v, dict) and v.get("value") is not None)
        print(f"    note: structured_fields: {extracted}/{len(sf)} extracted")

    return ok


def _count_jsonl_lines() -> int:
    """Return the number of lines currently in the persistence file (0 if absent)."""
    if not PERSIST_FILE.exists():
        return 0
    with open(PERSIST_FILE, "r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _read_last_n_jsonl_records(n: int) -> list:
    """Read and JSON-parse the last n lines of the persistence file."""
    if not PERSIST_FILE.exists():
        return []
    with open(PERSIST_FILE, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    out = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            out.append({"_parse_error": str(e), "_raw": line[:200]})
    return out


def main():
    print("=" * 70)
    print("structured_summary schema + persistence-sink validation")
    print("=" * 70)
    print(f"Persistence file: {PERSIST_FILE}")

    # Take a baseline so we know how many lines existed BEFORE this test run.
    # We compare counts at the end, so re-running this script against an
    # existing file is safe and doesn't require deletion.
    baseline_lines = _count_jsonl_lines()
    print(f"Baseline line count: {baseline_lines}")

    # New session
    print("\nCreating new session...")
    new_resp = _post("/api/chat/new", {})
    session_id = new_resp.get("session_id")
    if not session_id:
        print(f"FAIL: no session_id in /api/chat/new response: {new_resp}")
        sys.exit(1)
    print(f"  session_id: {session_id}")

    overall_ok = True

    # TEST 1: folder summary -----------------------------------------
    print("\n" + "=" * 70)
    print("TEST 1: 'summary on 27 manor drive' -> response_kind=folder_summary")
    print("=" * 70)
    r1 = _post("/api/chat", {
        "query": "summary on 27 manor drive",
        "session_id": session_id,
    })
    if "error" in r1:
        print(f"FAIL: server returned error: {r1['error']}")
        sys.exit(1)
    ok1 = _validate_schema(r1.get("structured_summary"),
                            "summary on 27 manor drive", "folder_summary")
    overall_ok = overall_ok and ok1
    ss = r1.get("structured_summary") or {}
    print(f"\n  Summary preview:")
    print(f"    folder_name:       {ss.get('folder_name')!r}")
    print(f"    file_count_total:  {ss.get('file_count_total')}")
    print(f"    overview (first 80 chars): {(ss.get('overview') or '')[:80]!r}")
    print(f"    key_facts count:   {len(ss.get('key_facts') or [])}")
    sf1 = ss.get("structured_fields") or {}
    extracted1 = [(k, v["value"]) for k, v in sf1.items() if v.get("value") is not None]
    print(f"    structured_fields extracted ({len(extracted1)}/{len(sf1)}):")
    for k, v in extracted1:
        vs = v if not isinstance(v, str) or len(v) <= 50 else v[:50] + "..."
        print(f"      {k:28s} = {vs!r}")

    # Confirm one new JSONL row was written.
    time.sleep(0.1)  # tiny pause to let the disk flush
    after_t1 = _count_jsonl_lines()
    delta_t1 = after_t1 - baseline_lines
    if delta_t1 == 1:
        print(f"  OK   persistence: +1 row written ({baseline_lines} -> {after_t1})")
    else:
        print(f"  FAIL persistence: expected +1 row, got +{delta_t1} "
              f"({baseline_lines} -> {after_t1})")
        overall_ok = False

    # TEST 2: open-items follow-up -----------------------------------
    print("\n" + "=" * 70)
    print("TEST 2: 'what\\'s missing' (follow-up) -> response_kind=open_items_only")
    print("=" * 70)
    r2 = _post("/api/chat", {
        "query": "what's missing",
        "session_id": session_id,
    })
    if "error" in r2:
        print(f"FAIL: server returned error: {r2['error']}")
        sys.exit(1)
    ok2 = _validate_schema(r2.get("structured_summary"),
                            "what's missing", "open_items_only")
    overall_ok = overall_ok and ok2
    ss2 = r2.get("structured_summary") or {}
    if ss2.get("folder_name") != ss.get("folder_name"):
        print(f"\n  FAIL: context not anchored. "
              f"turn 1 folder={ss.get('folder_name')!r}, "
              f"turn 2 folder={ss2.get('folder_name')!r}")
        overall_ok = False
    else:
        print(f"\n  OK   context anchored: folder_name={ss2.get('folder_name')!r}")

    time.sleep(0.1)
    after_t2 = _count_jsonl_lines()
    delta_t2 = after_t2 - after_t1
    if delta_t2 == 1:
        print(f"  OK   persistence: +1 row written ({after_t1} -> {after_t2})")
    else:
        print(f"  FAIL persistence: expected +1 row, got +{delta_t2} "
              f"({after_t1} -> {after_t2})")
        overall_ok = False

    # TEST 3: factual Q&A --------------------------------------------
    print("\n" + "=" * 70)
    print("TEST 3: factual Q&A -> structured_summary null, no JSONL row")
    print("=" * 70)
    r3 = _post("/api/chat", {
        "query": "give me the opinion of value issued for 27 manor drive",
        "session_id": session_id,
    })
    if "error" in r3:
        print(f"FAIL: server returned error: {r3['error']}")
        sys.exit(1)
    ss3 = r3.get("structured_summary")
    print(f"  structured_summary: {ss3!r}")
    if ss3 is None:
        print(f"  OK   structured_summary is null (factual Q&A path unaffected)")
    else:
        print(f"  note: structured_summary populated on factual path; "
              f"response_kind={ss3.get('response_kind')!r}")
    ans = (r3.get("answer") or "").strip()
    if ans:
        print(f"  OK   answer present ({len(ans)} chars): {ans[:100]!r}")
    else:
        print(f"  FAIL: empty answer on factual query")
        overall_ok = False

    time.sleep(0.1)
    after_t3 = _count_jsonl_lines()
    delta_t3 = after_t3 - after_t2
    if delta_t3 == 0:
        print(f"  OK   persistence: 0 new rows ({after_t2} -> {after_t3})")
    else:
        print(f"  FAIL persistence: expected 0 new rows, got +{delta_t3} "
              f"({after_t2} -> {after_t3})")
        overall_ok = False

    # Verify the two written rows have the current schema version.
    print("\n" + "=" * 70)
    print(f"Persistence file: verifying last 2 rows have schema_version {EXPECTED_SCHEMA_VERSION}")
    print("=" * 70)
    last_two = _read_last_n_jsonl_records(2)
    expected_kinds = ["folder_summary", "open_items_only"]
    for i, rec in enumerate(last_two):
        kind = rec.get("response_kind", "(missing)")
        ver  = rec.get("schema_version", "(missing)")
        match = (kind == expected_kinds[i]) and (ver == EXPECTED_SCHEMA_VERSION)
        flag = "OK  " if match else "FAIL"
        print(f"  [{flag}] row {-2+i}: response_kind={kind!r} schema_version={ver!r}")
        if not match:
            overall_ok = False

    # Final report ---------------------------------------------------
    print("\n" + "=" * 70)
    print(f"OVERALL: {'PASS' if overall_ok else 'FAIL'}")
    print("=" * 70)
    print(f"Total rows in persistence file: {after_t3}")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
