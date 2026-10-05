"""
HTTP routes of the GDPR removal tool.

Stateless by design: each request fetches what it needs into a temporary
directory, answers, and deletes the directory (workspace.py). The three steps
the UI walks through are tied together only by values it sends back:

  POST /api/scan           {eventUrl}
       -> categories + skaters, `snapshot` (hash of every fetched file)
  POST /api/protocol_plan  {eventUrl, snapshot, selection, protocolUrl}
       -> the protocol pages a selection touches, `planHash`; writes nothing
  POST /api/generate       {eventUrl, snapshot, selection,
                            protocol?: {url, planHash, confirmed: true}}
       -> verification report + the ZIP (base64)

`generate` refetches the event and refuses (409) when its snapshot differs
from the one the selection was made on, and redacts a protocol only for the
very plan the user confirmed.

Privacy: URLs and names travel only in request/response bodies. Nothing is
logged except exception *types* — a message could quote page text.
"""
import base64
import json
import logging
import posixpath
import urllib.parse

import azure.functions as func

import auth
import engine
import fetcher
import fsm_html
import package
import protocol
import workspace

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

MAX_BODY = 256 * 1024

_FETCH_STATUS = {
    "invalid_url": 400, "host_not_allowed": 400, "not_a_pdf": 400, "invalid_path": 400,
    "event_not_found": 404, "protocol_not_found": 404,
    "file_too_large": 413, "too_many_files": 413, "event_too_large": 413,
    "upstream_error": 502, "upstream_unreachable": 502,
}


def _json(obj, status=200):
    return func.HttpResponse(json.dumps(obj, ensure_ascii=False), status_code=status,
                             mimetype="application/json", charset="utf-8",
                             headers={"Cache-Control": "no-store"})


def _error(code, status):
    return _json({"error": code}, status)


def _body(req):
    raw = req.get_body() or b""
    if len(raw) > MAX_BODY:
        raise ValueError("body_too_large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise ValueError("invalid_json")
    if not isinstance(data, dict):
        raise ValueError("invalid_json")
    return data


def _guard(handler):
    """Auth + body parsing + error mapping shared by every POST route."""
    def wrapped(req: func.HttpRequest) -> func.HttpResponse:
        email = auth.user_email(req)
        if not email:
            return _error("unauthorized", 401)
        if not auth.user_allowed(email):
            return _error("forbidden", 403)
        try:
            body = _body(req)
        except ValueError as e:
            return _error(str(e), 400)
        try:
            return handler(body)
        except fetcher.FetchError as e:
            return _error(e.code, _FETCH_STATUS.get(e.code, 502))
        except fsm_html.EncodingError:
            return _error("unsupported_encoding", 422)
        except engine.SelectionError:
            return _error("invalid_selection", 400)
        except protocol.PlanMismatch:
            return _error("protocol_plan_changed", 409)
        except package.NameLeak:
            return _error("manifest_check_failed", 500)
        except Exception as e:  # noqa: BLE001 — never echo or log page content
            logging.error("request failed: %s", type(e).__name__)
            return _error("internal_error", 500)
    wrapped.__name__ = handler.__name__
    return wrapped


def _load_event(body, wd):
    url = (body.get("eventUrl") or "").strip()
    if not url:
        raise fetcher.FetchError("invalid_url")
    event = fetcher.fetch_event(url, wd)
    cats = engine.build_catalog(event)
    return event, cats


class SnapshotMismatch(Exception):
    pass


def _check_snapshot(body, event):
    if body.get("snapshot") != event.snapshot():
        raise SnapshotMismatch()


def _selection(body, cats):
    ids = body.get("selection")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise engine.SelectionError("invalid")
    return [i for i in dict.fromkeys(ids)], engine.resolve_selection(cats, ids)


def _plan_for_selection(plan_obj, name_idx):
    """Map plan entries' unique-name indexes back to selection indexes."""
    out = []
    for e in plan_obj["pages"]:
        out.append({**e, "skaters": [n for n, ni in enumerate(name_idx) if ni in e["skaters"]]})
    return out


# ── routes ────────────────────────────────────────────────────────────────────

@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok"})


@app.route(route="scan", methods=["POST"])
@_guard
def scan(body):
    with workspace.workspace() as wd:
        event, cats = _load_event(body, wd)
        if not cats:
            return _error("no_categories", 422)
        index_text, _ = fsm_html.decode(event.read(event.index_name))
        link = fsm_html.protocol_link(index_text)
        suggested = urllib.parse.urljoin(event.index_url, link) if link else None
        return _json({
            "eventUrl": event.index_url,
            "baseUrl": event.base_url,
            "snapshot": event.snapshot(),
            "categories": engine.catalog_json(cats),
            "suggestedProtocolUrl": suggested,
            "stats": {
                "files": len(event.files),
                "html": sum(1 for f in event.files if f.lower().endswith((".htm", ".html"))),
                "pdf": sum(1 for f in event.files if f.lower().endswith(".pdf")),
                "sharedAssets": len(event.shared),
                "missingLinks": len(event.missing),
            },
        })


@app.route(route="protocol_plan", methods=["POST"])
@_guard
def protocol_plan(body):
    with workspace.workspace() as wd:
        event, cats = _load_event(body, wd)
        try:
            _check_snapshot(body, event)
        except SnapshotMismatch:
            return _error("event_changed", 409)
        ids, selection = _selection(body, cats)
        uniq, clubs, name_idx = engine.unique_names(selection)
        url = (body.get("protocolUrl") or "").strip()
        data, _ = fetcher.fetch_protocol(url, wd)
        plan_obj = protocol.plan(data, uniq, clubs)
        return _json({
            "protocolUrl": url,
            "outputPath": fetcher.protocol_output_path(event.base_url, url),
            "pageCount": plan_obj["pageCount"],
            "pages": _plan_for_selection(plan_obj, name_idx),
            "planHash": protocol.plan_hash(data, plan_obj, ids),
        })


@app.route(route="generate", methods=["POST"])
@_guard
def generate(body):
    proto = body.get("protocol")
    if proto is not None:
        if not isinstance(proto, dict) or proto.get("confirmed") is not True or not proto.get("planHash"):
            return _error("protocol_not_confirmed", 400)
    with workspace.workspace() as wd:
        event, cats = _load_event(body, wd)
        try:
            _check_snapshot(body, event)
        except SnapshotMismatch:
            return _error("event_changed", 409)
        ids, selection = _selection(body, cats)
        uniq, clubs, name_idx = engine.unique_names(selection)

        protocol_result = None
        if proto is not None:
            url = (proto.get("url") or "").strip()
            data, _ = fetcher.fetch_protocol(url, wd)
            new, info = protocol.apply(data, uniq, clubs, ids, proto["planHash"])
            rel = fetcher.protocol_output_path(event.base_url, url)
            out = wd / "protocol" / "redacted.pdf"
            out.write_bytes(new)
            protocol_result = (rel, data, new, info, name_idx)

        changed, report = engine.generate(event, cats, selection, protocol_result)
        for rel, data in changed.items():
            target = wd / "generated" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        manifest = package.build_manifest(event.index_url, engine.labels(selection), report)
        zip_bytes = package.build_zip(changed, manifest, uniq)
        event_dir = posixpath.basename(urllib.parse.urlsplit(event.base_url).path.rstrip("/")) or "event"
        return _json({
            "ok": report["ok"],
            "manifest": manifest,
            "zipName": f"gdpr-removal-{event_dir}.zip",
            "zipBase64": base64.b64encode(zip_bytes).decode("ascii"),
        })
