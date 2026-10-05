"""The backend modules import each other flatly (`import fsm_html`), the way the
Functions host loads them, so the function app's directory has to be on
sys.path for the tests to import them the same way."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import fixtures


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOWED_RESULT_HOSTS", fixtures.HOST)
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    monkeypatch.delenv("ALLOWED_USER_EMAILS", raising=False)
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setenv("FSGDPR_TMP_ROOT", str(root))
    yield


@pytest.fixture
def tmp_root():
    return os.environ["FSGDPR_TMP_ROOT"]


class FakeWeb:
    """Stands in for fetcher.http_get: serves a URL -> bytes map, records every
    request, and can be edited between requests (to simulate a republish)."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.requests = []

    def __call__(self, url, limit=None):
        import fetcher
        import urllib.parse
        if not fetcher._netloc_allowed(urllib.parse.urlsplit(url)):
            raise fetcher.FetchError("host_not_allowed")
        self.requests.append(url)
        # FS Manager links may be http:// — the fake serves both schemes.
        key = url.replace("http://", "https://", 1)
        return self.pages.get(urllib.parse.unquote(key))


@pytest.fixture
def web(monkeypatch):
    import fetcher
    fake = FakeWeb(fixtures.url_map("fsm"))
    monkeypatch.setattr(fetcher, "http_get", fake)
    return fake


def make_request(path, body=None, headers=None, method="POST"):
    import azure.functions as func
    h = {"x-forwarded-user-email": "operator@example.com"}
    h.update(headers or {})
    return func.HttpRequest(method, "/api/" + path, headers=h,
                            body=json.dumps(body or {}).encode("utf-8"))


def call(name, body=None, headers=None):
    import function_app as fa
    fn = getattr(fa, name)._function.get_user_function()
    resp = fn(make_request(name, body, headers))
    return resp.status_code, json.loads(resp.get_body() or b"{}")
