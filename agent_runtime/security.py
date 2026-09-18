"""Canonical argument hashing and persistence-safe redaction."""
import hashlib, json, re
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE = {"apikey", "token", "accesstoken", "refreshtoken", "password", "passwd", "secret", "clientsecret", "authorization", "proxyauthorization", "cookie", "setcookie"}

def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

def arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(arguments).encode()).hexdigest()

def _sensitive_key(key: object) -> bool:
    name = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return name in _SENSITIVE or name.endswith(("apikey", "token", "password", "passwd", "secret", "authorization"))

def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): REDACTED if _sensitive_key(k) else redact_sensitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(v) for v in value]
    return value
