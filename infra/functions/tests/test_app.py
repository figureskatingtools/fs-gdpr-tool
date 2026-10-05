"""End to end through the HTTP routes: scan → protocol plan → generate, against
the synthetic event served by a fake web."""
import base64
import io
import json
import os
import zipfile

import pikepdf
import pymupdf
import pytest
from pypdf import PdfReader

import fixtures
import fsm_html
import names
from conftest import call

REPL = "Nimi poistettu pyynnöstä"


def _scan():
    status, body = call("scan", {"eventUrl": fixtures.INDEX_URL})
    assert status == 200, body
    return body


def _ids(scan, wanted_names):
    out = []
    for cat in scan["categories"]:
        for sk in cat["skaters"]:
            if any(names.same_name(sk["name"], n) for n in wanted_names):
                out.append(sk["id"])
    return out


def _zip(body):
    return zipfile.ZipFile(io.BytesIO(base64.b64decode(body["zipBase64"])))


def _all_text_pdf(data):
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


@pytest.fixture
def flow(web):
    scan = _scan()
    sel = _ids(scan, fixtures.SELECTED)
    status, plan = call("protocol_plan", {
        "eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel,
        "protocolUrl": scan["suggestedProtocolUrl"]})
    assert status == 200, plan
    status, gen = call("generate", {
        "eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel,
        "protocol": {"url": scan["suggestedProtocolUrl"], "planHash": plan["planHash"], "confirmed": True}})
    assert status == 200, gen
    return scan, sel, plan, gen


# ── scan ──────────────────────────────────────────────────────────────────────

def test_scan_lists_categories_and_skaters(web):
    scan = _scan()
    cats = scan["categories"]
    assert [c["name"] for c in cats] == [fixtures.CAT1, fixtures.CAT2]
    assert [len(c["segments"]) for c in cats] == [1, 2]          # schedule table not absorbed
    cat1 = {s["name"]: s for s in cats[0]["skaters"]}
    assert set(cat1) == {s[2] for s in fixtures.CAT1_SKATERS} | {fixtures.CAT1_WD[2]}
    assert cat1["Élodie KÄRKKÄINEN"]["status"] == "WD"
    assert cat1["Aino MÄKELÄ"]["club"] == "KUULA"
    assert sorted(cat1["Aino MÄKELÄ"]["sources"]) == ["entries", "result"]
    # The skater in two categories is linked both ways.
    sami1 = cat1["Sami SIIVONEN"]
    sami2 = next(s for s in cats[1]["skaters"] if s["name"] == "Sami SIIVONEN")
    assert sami1["alsoIn"] == [sami2["id"]] and sami2["alsoIn"] == [sami1["id"]]
    assert scan["suggestedProtocolUrl"] == fixtures.BASE + fixtures.PROTOCOL_NAME


def test_scan_does_not_crawl_outside_event_dir(web):
    _scan()
    bios = [u for u in web.requests if "/bios/" in u]
    assert bios == []
    outside = [u for u in web.requests
               if fixtures.EVENT_DIR not in u]
    # Only directly referenced assets: style sheet, script, flags.
    assert outside and all(u.endswith((".css", ".js", ".GIF")) for u in outside)
    # The protocol (a form action, not a link) is not fetched by the scan.
    assert not any(u.endswith(fixtures.PROTOCOL_NAME) for u in web.requests)


def test_scan_rejects_hosts_not_on_allow_list(web):
    status, body = call("scan", {"eventUrl": "https://evil.example.org/results/x/index.htm"})
    assert status == 400 and body["error"] == "host_not_allowed"


def test_requests_without_identity_are_rejected(web, monkeypatch):
    monkeypatch.setenv("PROXY_SHARED_SECRET", "s3cret")
    status, _ = call("scan", {"eventUrl": fixtures.INDEX_URL})
    assert status == 401
    status, _ = call("scan", {"eventUrl": fixtures.INDEX_URL}, headers={"x-proxy-secret": "wrong"})
    assert status == 401
    status, _ = call("scan", {"eventUrl": fixtures.INDEX_URL}, headers={"x-proxy-secret": "s3cret"})
    assert status == 200


def test_allowed_user_list(web, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_EMAILS", "someone@example.com")
    status, body = call("scan", {"eventUrl": fixtures.INDEX_URL})
    assert status == 403


# ── protocol plan ─────────────────────────────────────────────────────────────

def test_protocol_plan_lists_pages_and_writes_nothing(web, tmp_root):
    scan = _scan()
    sel = _ids(scan, fixtures.SELECTED)
    before = dict(web.pages)
    status, plan = call("protocol_plan", {
        "eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel,
        "protocolUrl": scan["suggestedProtocolUrl"]})
    assert status == 200
    assert plan["outputPath"] == fixtures.PROTOCOL_NAME
    types = {(p["type"], p["action"]) for p in plan["pages"]}
    assert ("podium", "remove_page") in types
    assert ("results", "redact") in types
    assert ("segment_results", "redact") in types
    assert ("judges_details", "redact") in types
    podiums = [p for p in plan["pages"] if p["type"] == "podium"]
    assert len(podiums) == 2                      # Aino on cat 1's, Sami on cat 2's
    assert all(p["action"] == "redact" for p in plan["pages"] if p["type"] != "podium")
    # Withdrawn skater: only ever on a withdrawn list or her own sheets.
    elodie = sel.index(_ids(scan, ["Élodie KÄRKKÄINEN"])[0])
    elodie_pages = [p for p in plan["pages"] if elodie in p["skaters"]]
    assert elodie_pages and {p["type"] for p in elodie_pages} <= {"withdrawn_list", "results", "segment_results"}
    # Nothing written anywhere: the work dir is gone, the source untouched.
    assert os.listdir(tmp_root) == []
    assert web.pages == before
    assert "zipBase64" not in plan


def test_generate_refuses_unconfirmed_protocol(web, tmp_root):
    scan = _scan()
    sel = _ids(scan, fixtures.SELECTED)
    n = len(web.requests)
    for proto in ({"url": scan["suggestedProtocolUrl"], "planHash": "x", "confirmed": False},
                  {"url": scan["suggestedProtocolUrl"], "confirmed": True},
                  {"url": scan["suggestedProtocolUrl"], "planHash": "x"}):
        status, body = call("generate", {"eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"],
                                         "selection": sel, "protocol": proto})
        assert status == 400 and body["error"] == "protocol_not_confirmed"
        assert "zipBase64" not in body
    # Refused before anything was fetched.
    assert len(web.requests) == n
    assert os.listdir(tmp_root) == []


def test_generate_refuses_a_stale_plan(web):
    scan = _scan()
    sel = _ids(scan, fixtures.SELECTED)
    status, plan = call("protocol_plan", {
        "eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel,
        "protocolUrl": scan["suggestedProtocolUrl"]})
    # A different selection than the one the plan was confirmed for.
    status, body = call("generate", {
        "eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel[:1],
        "protocol": {"url": scan["suggestedProtocolUrl"], "planHash": plan["planHash"], "confirmed": True}})
    assert status == 409 and body["error"] == "protocol_plan_changed"


def test_generate_refuses_when_event_changed(web):
    scan = _scan()
    sel = _ids(scan, fixtures.SELECTED[:1])
    web.pages[fixtures.BASE + "CAT002RS.htm"] += b"\r\n"
    status, body = call("generate", {"eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel})
    assert status == 409 and body["error"] == "event_changed"


def test_unknown_selection(web):
    scan = _scan()
    status, body = call("generate", {"eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"],
                                     "selection": ["c9-s9"]})
    assert status == 400 and body["error"] == "invalid_selection"


# ── generate ──────────────────────────────────────────────────────────────────

def test_zip_contains_only_changed_files(flow):
    scan, sel, plan, gen = flow
    z = _zip(gen)
    names_in_zip = set(z.namelist())
    assert names_in_zip == {
        "gdpr-manifest.json",
        "CAT001EN.htm", "CAT001RS.htm", "SEG001.htm",
        "CAT002EN.htm", "CAT002RS.htm", "SEG002.htm", "SEG003.htm",
        "FSKXTEST-CATA----FNL-000100--_JudgesDetailsperSkater.pdf",
        "FSKXTEST-CATB----QUAL000100--_JudgesDetailsperSkater.pdf",
        "FSKXTEST-CATB----FNL-000100--_JudgesDetailsperSkater.pdf",
        fixtures.PROTOCOL_NAME,
    }
    # Index and panel pages are never touched.
    assert "index.htm" not in names_in_zip
    assert not any(n.endswith("OF.htm") for n in names_in_zip)


def test_removed_names_are_gone_from_every_output(flow, web):
    scan, sel, plan, gen = flow
    z = _zip(gen)
    patterns = [names.name_pattern(n) for n in fixtures.SELECTED]
    for member in z.namelist():
        data = z.read(member)
        if member.endswith(".pdf"):
            texts = [_all_text_pdf(data)]
            texts += [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        else:
            text, _ = fsm_html.decode(data) if member.endswith(".htm") else (data.decode("utf-8"), None)
            texts = [text]
        for t in texts:
            folded = names.fold(t)
            for n, p in zip(fixtures.SELECTED, patterns):
                assert not p.search(folded), f"{member} still contains a selected name"
    assert gen["ok"] is True
    v = gen["manifest"]["verification"]
    assert v["remainingOccurrences"] == [] and v["skatersNotFound"] == []


def test_other_skaters_unchanged(flow, web):
    scan, sel, plan, gen = flow
    z = _zip(gen)
    for member in z.namelist():
        if not member.endswith(".htm"):
            continue
        old = web.pages[fixtures.BASE + member]
        old_text, _ = fsm_html.decode(old)
        new_text, _ = fsm_html.decode(z.read(member))
        old_rows = fsm_html.skater_rows(old_text)
        new_rows = fsm_html.skater_rows(new_text)
        assert len(old_rows) == len(new_rows)
        for a, b in zip(old_rows, new_rows):
            if any(names.same_name(a.name, n) for n in fixtures.SELECTED):
                assert b.name == REPL
                assert b.club == ""
                # Placement, start number and scores kept.
                keep = [i for i in range(len(a.texts)) if i not in (a.name_col, a.club_col)]
                assert [a.texts[i] for i in keep] == [b.texts[i] for i in keep]
            else:
                assert a.texts == b.texts
    for check in gen["manifest"]["verification"]["htmlPages"] + gen["manifest"]["verification"]["pdfFiles"]:
        assert check["ok"], check


def test_html_structure_and_encoding_preserved(flow, web):
    scan, sel, plan, gen = flow
    z = _zip(gen)
    data = z.read("CAT001RS.htm")
    old = web.pages[fixtures.BASE + "CAT001RS.htm"]
    assert all(b < 0x80 for b in data)                       # still pure ASCII
    assert b"Nimi poistettu pyynn&#xF6;st&#xE4;" in data      # FS Manager's entity style
    assert data.count(b"\r\n") == old.count(b"\r\n")          # CRLF kept, no lines added
    assert data.count(b"<tr") == old.count(b"<tr")
    assert data.count(b"<td") == old.count(b"<td")
    # Bios links and flags of the selected skaters are gone, others' remain.
    aino_bio = f"isufs{90000 + fixtures._uid('Aino MÄKELÄ')}".encode()
    assert aino_bio in old and aino_bio not in data
    hanna_bio = f"isufs{90000 + fixtures._uid('Hanna MALLINEN')}".encode()
    assert hanna_bio in data
    assert old.count(b"flags/") - data.count(b"flags/") == 3  # Aino, Sami, Élodie
    # Minimal diff: identical except inside the edited cells.
    import difflib
    changed_lines = [l for l in difflib.ndiff(old.decode().splitlines(), data.decode().splitlines())
                     if l.startswith(("- ", "+ "))]
    assert len(changed_lines) == 2 * 3 * 2                     # 3 rows × (name + club line) × (-/+)


def test_judges_pdfs_are_truly_redacted(flow):
    scan, sel, plan, gen = flow
    z = _zip(gen)
    data = z.read("FSKXTEST-CATA----FNL-000100--_JudgesDetailsperSkater.pdf")
    # Full rewrite, no incremental update.
    assert data.count(b"%%EOF") == 1
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert "/Prev" not in pdf.trailer
        # No redaction/overlay annotations left behind.
        assert all("/Annots" not in page.obj for page in pdf.pages)
    for page in PdfReader(io.BytesIO(data)).pages:
        t = names.fold(page.extract_text() or "")
        assert not names.find_in_text(t, "Aino MÄKELÄ") and not names.find_in_text(t, "Sami SIIVONEN")
    text = _all_text_pdf(data)
    assert text.count(REPL) == 2                             # Aino + Sami (Élodie never skated)
    assert "Hanna MALLINEN" in text and "44.91" in text
    # Replacement in the name's position on Aino's page.
    doc = pymupdf.open(stream=data, filetype="pdf")
    rect = doc[0].search_for(REPL)[0]
    assert abs(rect.x0 - 62) < 1.5
    # Text removed, not covered: nothing is painted over the name's area.
    assert not [d for d in doc[0].get_drawings() if d["rect"].intersects(rect) and d.get("fill")]
    doc.close()


def test_protocol_podium_pages_and_images_removed(flow, web):
    scan, sel, plan, gen = flow
    z = _zip(gen)
    old = web.pages[fixtures.BASE + fixtures.PROTOCOL_NAME]
    new = z.read(fixtures.PROTOCOL_NAME)
    old_doc = pymupdf.open(stream=old, filetype="pdf")
    new_doc = pymupdf.open(stream=new, filetype="pdf")
    assert new_doc.page_count == old_doc.page_count - 2
    assert "P O D I U M" not in "".join(p.get_text() for p in new_doc)
    old_images = sum(len(p.get_images()) for p in old_doc)
    assert old_images == 2
    with pikepdf.open(io.BytesIO(new)) as pdf:
        images = [o for o in pdf.objects if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == "/Image"]
        assert images == []                                   # orphaned podium photos dropped
        assert "/Prev" not in pdf.trailer
    assert new.count(b"%%EOF") == 1
    manifest = gen["manifest"]
    assert manifest["protocol"]["removedPages"] == [p["page"] for p in plan["pages"] if p["action"] == "remove_page"]
    assert manifest["verification"]["pdfFiles"][-1]["removedPagesImagesGone"] is True
    old_doc.close()
    new_doc.close()


def test_protocol_keeps_name_and_other_content(flow, web):
    scan, sel, plan, gen = flow
    new = _zip(gen).read(fixtures.PROTOCOL_NAME)
    text = _all_text_pdf(new)
    for other in ("Örjan VÄÄNÄNEN", "Hanna MALLINEN", "Ville KOKEILU", "Mikko ESIMERKKI"):
        assert names.find_in_text(names.fold(text), other)
    # Rank kept in front of the replacement, on the same baseline.
    doc = pymupdf.open(stream=new, filetype="pdf")
    page = next(p for p in doc if "Pl. Name" in p.get_text() and "Total Score" in p.get_text())
    words = page.get_text("words")
    nimi = next(w for w in words if w[4] == "Nimi")
    assert any(w[4] == "1" and abs(w[3] - nimi[3]) < 1.5 and w[2] <= nimi[0] for w in words)
    doc.close()
    assert "61.20" in text and "98.70" in text                 # selected skaters' scores kept


def test_manifest_has_no_names(flow):
    scan, sel, plan, gen = flow
    raw = _zip(gen).read("gdpr-manifest.json").decode("utf-8")
    folded = names.fold(raw)
    for n in fixtures.ALL_NAMES:
        assert not names.find_in_text(folded, n)
        surname = names.tokens(n)[-1]
        assert surname not in folded
    manifest = json.loads(raw)
    assert [s["label"] for s in manifest["skaters"]] == [f"selected skater {i}" for i in range(1, len(sel) + 1)]
    assert {s["category"] for s in manifest["skaters"]} == {fixtures.CAT1, fixtures.CAT2}
    assert any("republished" in n for n in manifest["notes"])


def test_work_directory_is_deleted(flow, tmp_root):
    assert os.listdir(tmp_root) == []


def test_work_directory_deleted_on_error(web, tmp_root, monkeypatch):
    import engine

    def boom(*a, **k):
        raise RuntimeError("Aino MÄKELÄ")              # a message that must not be logged

    scan = _scan()
    monkeypatch.setattr(engine, "generate", boom)
    status, body = call("generate", {"eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"],
                                     "selection": _ids(scan, fixtures.SELECTED[:1])})
    assert status == 500 and body == {"error": "internal_error"}
    assert os.listdir(tmp_root) == []


def test_errors_never_log_names(web, caplog, monkeypatch):
    import engine

    def boom(*a, **k):
        raise RuntimeError("Aino MÄKELÄ")

    scan = _scan()
    monkeypatch.setattr(engine, "generate", boom)
    with caplog.at_level("DEBUG"):
        call("generate", {"eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"],
                          "selection": _ids(scan, fixtures.SELECTED[:1])})
    assert "MÄKELÄ" not in caplog.text and fixtures.HOST not in caplog.text


def test_selecting_one_category_only_reports_the_other(web):
    """Sami selected only in category 1: his category 2 rows stay, and
    verification says where."""
    scan = _scan()
    sami_cat1 = next(s["id"] for s in scan["categories"][0]["skaters"] if s["name"] == "Sami SIIVONEN")
    status, gen = call("generate", {"eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"],
                                    "selection": [sami_cat1]})
    assert status == 200
    assert gen["ok"] is False
    files = {h["file"] for h in gen["manifest"]["verification"]["remainingOccurrences"]}
    assert {"CAT002EN.htm", "CAT002RS.htm", "SEG002.htm", "SEG003.htm"} <= files
    assert all(h["skater"] == "selected skater 1" for h in gen["manifest"]["verification"]["remainingOccurrences"])


def test_withdrawn_only_keeps_podiums(web):
    scan = _scan()
    sel = _ids(scan, ["Élodie KÄRKKÄINEN"])
    status, plan = call("protocol_plan", {
        "eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel,
        "protocolUrl": scan["suggestedProtocolUrl"]})
    assert status == 200
    assert plan["pages"] and all(p["action"] == "redact" for p in plan["pages"])
    assert {p["type"] for p in plan["pages"]} == {"withdrawn_list"}
    status, gen = call("generate", {
        "eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel,
        "protocol": {"url": scan["suggestedProtocolUrl"], "planHash": plan["planHash"], "confirmed": True}})
    assert status == 200 and gen["ok"], gen["manifest"]["verification"]
    doc = pymupdf.open(stream=_zip(gen).read(fixtures.PROTOCOL_NAME), filetype="pdf")
    assert doc.page_count == plan["pageCount"]
    doc.close()


@pytest.mark.parametrize("style", ["cp1252", "nfd"])
def test_other_encodings(style, monkeypatch):
    import fetcher
    from conftest import FakeWeb
    fake = FakeWeb(fixtures.url_map(style))
    monkeypatch.setattr(fetcher, "http_get", fake)
    scan = _scan()
    sel = _ids(scan, fixtures.SELECTED)
    assert len(sel) == 4
    status, gen = call("generate", {"eventUrl": fixtures.INDEX_URL, "snapshot": scan["snapshot"], "selection": sel})
    assert status == 200 and gen["ok"], gen["manifest"]["verification"]
    z = _zip(gen)
    data = z.read("CAT001RS.htm")
    old = fake.pages[fixtures.BASE + "CAT001RS.htm"]
    if style == "cp1252":
        assert REPL.encode("cp1252") in data
        assert "Örjan VÄÄNÄNEN".encode("cp1252") in data           # untouched raw bytes
    else:
        assert REPL.encode("utf-8") in data
        import unicodedata
        assert unicodedata.normalize("NFD", "Örjan VÄÄNÄNEN").encode() in data
    assert data.count(b"\r\n") == old.count(b"\r\n")
