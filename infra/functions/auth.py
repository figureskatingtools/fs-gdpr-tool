"""
Who is calling: the figureskatingtools-site router proxies `/tools/gdpr/api/*`
here after its Entra login, adding `x-proxy-secret` and the signed-in user's
`x-forwarded-user-tid` / `-oid` / `-email` (see PROXY-CONTRACT.md). The secret
is checked first and fails CLOSED — without it no identity is derived at all.
Identity comes only from those router headers; tid and oid must be GUIDs. An
optional `ALLOWED_USER_EMAILS` (comma-separated) narrows the tool to the people
who handle removal requests.
"""
import hmac
import os
import re

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _header(req, name):
    return req.headers.get(name) or req.headers.get(name.lower())


def proxy_secret_ok(req) -> bool:
    """Fails closed: an unset/empty PROXY_SHARED_SECRET rejects every request."""
    expected = os.environ.get("PROXY_SHARED_SECRET") or ""
    if not expected:
        return False
    provided = _header(req, "X-Proxy-Secret") or ""
    return hmac.compare_digest(provided.encode(), expected.encode())


def _is_guid(value) -> bool:
    return bool(_GUID_RE.match((value or "").strip().lower()))


def user_email(req):
    """The acting user's email, or None (→ 401).

    Requires the proxy secret, a GUID `x-forwarded-user-tid` and
    `x-forwarded-user-oid` (a real signed-in principal), and the router's
    `x-forwarded-user-email`. No other header is an identity source.
    """
    if not proxy_secret_ok(req):
        return None
    if not (_is_guid(_header(req, "X-Forwarded-User-Tid")) and _is_guid(_header(req, "X-Forwarded-User-Oid"))):
        return None
    value = (_header(req, "X-Forwarded-User-Email") or "").strip()
    return value or None


def user_allowed(email: str) -> bool:
    raw = os.environ.get("ALLOWED_USER_EMAILS", "")
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return not allowed or email.lower() in allowed
