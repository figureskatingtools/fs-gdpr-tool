"""
Who is calling: the figureskatingtools-site router proxies `/tools/gdpr/api/*`
here after its Entra login, adding `x-proxy-secret` and `x-forwarded-user-email`
(see PROXY-CONTRACT.md). The secret is checked first — without it no identity
is derived at all. An optional `ALLOWED_USER_EMAILS` (comma-separated) narrows
the tool to the people who handle removal requests.
"""
import hmac
import os


def _header(req, name):
    return req.headers.get(name) or req.headers.get(name.lower())


def proxy_secret_ok(req) -> bool:
    """Enforced only when PROXY_SHARED_SECRET is set (local dev fails open)."""
    expected = os.environ.get("PROXY_SHARED_SECRET")
    if not expected:
        return True
    provided = _header(req, "X-Proxy-Secret") or ""
    return hmac.compare_digest(provided.encode(), expected.encode())


def user_email(req):
    """The acting user's email, or None (→ 401)."""
    if not proxy_secret_ok(req):
        return None
    for name in ("X-MS-CLIENT-PRINCIPAL-NAME", "X-Forwarded-User-Email"):
        value = (_header(req, name) or "").strip()
        if value:
            return value
    return None


def user_allowed(email: str) -> bool:
    raw = os.environ.get("ALLOWED_USER_EMAILS", "")
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return not allowed or email.lower() in allowed
