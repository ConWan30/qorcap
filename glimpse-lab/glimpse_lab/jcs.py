import hashlib, json
def jcs(o) -> bytes:
    """RFC 8785-compatible for the value types we use (str, int, bool, None, list, dict; no floats)."""
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
def sha(o) -> str:
    return hashlib.sha256(o if isinstance(o, bytes) else jcs(o)).hexdigest()
