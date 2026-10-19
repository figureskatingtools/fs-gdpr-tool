"""The proxy auth contract (PROXY-CONTRACT.md): the shared secret fails closed,
the identity comes only from the router's x-forwarded-user-* headers (tid and
oid must be GUIDs), and ALLOWED_USER_EMAILS still narrows who may use it."""
import base64
import json

import pytest

import auth
from conftest import TEST_EMAIL, TEST_OID, TEST_SECRET, TEST_TID, call, make_request

ROUTES = ("scan", "protocol_plan", "generate")


@pytest.mark.parametrize("configured", [None, ""], ids=["unset", "empty"])
def test_secret_fails_closed_when_not_configured(configured, monkeypatch):
    if configured is None:
        monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    else:
        monkeypatch.setenv("PROXY_SHARED_SECRET", configured)
    assert auth.proxy_secret_ok(make_request("scan")) is False
    assert auth.proxy_secret_ok(make_request("scan", headers={"x-proxy-secret": ""})) is False
    assert auth.user_email(make_request("scan")) is None
    for route in ROUTES:
        status, body = call(route, {})
        assert (status, body) == (401, {"error": "unauthorized"}), route


@pytest.mark.parametrize("secret", [None, "", "wrong", TEST_SECRET + "x"])
def test_wrong_or_missing_secret(secret):
    assert auth.user_email(make_request("scan", headers={"x-proxy-secret": secret})) is None
    for route in ROUTES:
        assert call(route, {}, headers={"x-proxy-secret": secret})[0] == 401, route


def test_secret_is_compared_in_constant_time(monkeypatch):
    seen = []
    real = auth.hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    assert auth.proxy_secret_ok(make_request("scan")) is True
    assert seen == [(TEST_SECRET.encode(), TEST_SECRET.encode())]


def test_identity_from_router_headers():
    assert auth.user_email(make_request("scan")) == TEST_EMAIL
    upper = make_request("scan", headers={"x-forwarded-user-tid": f" {TEST_TID.upper()} ",
                                          "x-forwarded-user-oid": TEST_OID.upper()})
    assert auth.user_email(upper) == TEST_EMAIL


@pytest.mark.parametrize("override", [
    {"x-forwarded-user-tid": None},
    {"x-forwarded-user-oid": None},
    {"x-forwarded-user-tid": None, "x-forwarded-user-oid": None},
    {"x-forwarded-user-tid": "not-a-guid"},
    {"x-forwarded-user-oid": "1234"},
    {"x-forwarded-user-oid": TEST_OID + "0"},
    {"x-forwarded-user-tid": TEST_EMAIL},
    {"x-forwarded-user-email": None},
    {"x-forwarded-user-email": "  "},
], ids=lambda o: ",".join(f"{k.split('-')[-1]}={v!r}" for k, v in o.items()))
def test_missing_or_invalid_identity_headers_are_401(override):
    assert auth.user_email(make_request("scan", headers=override)) is None
    for route in ROUTES:
        assert call(route, {}, headers=override)[0] == 401, route


def test_legacy_identity_sources_are_ignored():
    """X-MS-CLIENT-PRINCIPAL-NAME used to win over the router's e-mail; now it
    is ignored, and neither it nor a principal blob stands in for tid/oid."""
    principal = base64.b64encode(json.dumps({"userDetails": "swa@example.com"}).encode()).decode()
    req = make_request("scan", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "direct@example.com",
                                        "x-ms-client-principal": principal})
    assert auth.user_email(req) == TEST_EMAIL
    no_router_identity = {"x-forwarded-user-tid": None, "x-forwarded-user-oid": None,
                          "x-forwarded-user-email": None,
                          "X-MS-CLIENT-PRINCIPAL-NAME": "direct@example.com",
                          "x-ms-client-principal": principal}
    assert auth.user_email(make_request("scan", headers=no_router_identity)) is None


def test_allowed_user_emails_still_applies(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_EMAILS", "Someone@Example.com, other@example.com")
    assert auth.user_allowed("someone@example.com")
    assert not auth.user_allowed(TEST_EMAIL)
    assert call("scan", {})[0] == 403
    # The allow-list is checked against the router's header only.
    status, _ = call("scan", {}, headers={"X-MS-CLIENT-PRINCIPAL-NAME": "someone@example.com"})
    assert status == 403


def test_health_needs_no_secret(monkeypatch):
    import function_app as fa
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    fn = fa.health._function.get_user_function()
    resp = fn(make_request("health", method="GET", headers={k: None for k in (
        "x-proxy-secret", "x-forwarded-user-tid", "x-forwarded-user-oid", "x-forwarded-user-email")}))
    assert resp.status_code == 200
