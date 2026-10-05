"""Unit tests: name matching, FS Manager HTML handling, PDF redaction details,
verification and the fetcher's boundaries."""
import http.server
import io
import threading
import unicodedata

import pymupdf
import pytest

import fetcher
import fixtures
import fsm_html
import names
import pdf_redact
import verify


# ── names ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", [
    "Aino MÄKELÄ", "MÄKELÄ Aino", "aino mäkelä", "Mäkelä, Aino",
    "Aino M&#xC4;KEL&#xC4;", "Aino M&Auml;KEL&Auml;",
    unicodedata.normalize("NFD", "Aino MÄKELÄ"),
    "Aino MA¨KELA¨",                 # spacing diaeresis as some PDFs extract it
    "Aino MÄKELÄ",
])
def test_name_variants_match(variant):
    assert names.same_name(variant, "Aino MÄKELÄ")
    assert names.find_in_text(names.fold(f"x {variant} y"), "Aino MÄKELÄ")


def test_name_matching_is_whole_token():
    assert not names.same_name("Aino MÄKELÄINEN", "Aino MÄKELÄ")
    assert not names.find_in_text(names.fold("Aino MÄKELÄINEN"), "Aino MÄKELÄ")
    assert not names.find_in_text(names.fold("Aino"), "Aino MÄKELÄ")


def test_name_across_line_break():
    assert names.find_in_text(names.fold("ÉLODIE\nKÄRKKÄINEN", unescape=False), "Élodie KÄRKKÄINEN")


# ── HTML ──────────────────────────────────────────────────────────────────────

def _event(style="fsm"):
    return fixtures.build_event(style)[0]


@pytest.mark.parametrize("style,codec", [("fsm", "utf-8"), ("cp1252", "cp1252"), ("nfd", "utf-8")])
def test_decode_round_trips(style, codec):
    for rel, data in _event(style).items():
        if rel.endswith(".htm"):
            text, got = fsm_html.decode(data)
            assert text.encode(got) == data
            if any(b >= 0x80 for b in data):
                assert got == codec


def test_skater_rows_skip_officials_and_club_tables():
    text, _ = fsm_html.decode(_event()["SEG001.htm"])
    rows = fsm_html.skater_rows(text)
    assert [r.name for r in rows] == [s[2] for s in fixtures.CAT1_SKATERS] + [fixtures.CAT1_WD[2]]
    assert rows[-1].placement == "WD"
    text, _ = fsm_html.decode(_event()["CAT001RS.htm"])
    rows = fsm_html.skater_rows(text)
    assert rows[0].club == "KUULA" and rows[0].texts[3] == "61.20"


def test_redact_page_changes_only_target_cells():
    old = _event()["CAT002RS.htm"]
    new, matched = fsm_html.redact_page(old, ["SIIVONEN Sami"])
    assert matched == [(0, 0)]
    text_old, _ = fsm_html.decode(old)
    rows = fsm_html.skater_rows(text_old)
    target = rows[0]
    name_td, club_td = target.cells[target.name_col], target.cells[target.club_col]
    # Everything before the name cell and after the club cell is byte-identical.
    assert new[:name_td.open_end] == old[:name_td.open_end]
    assert new[-(len(old) - club_td.close_start):] == old[club_td.close_start:]


def test_redact_page_no_match_returns_original():
    old = _event()["CAT002RS.htm"]
    new, matched = fsm_html.redact_page(old, ["Nobody HERE"])
    assert new is old and matched == []


def test_ascii_page_gets_entities():
    old = _event()["SEG001.htm"]
    new, _ = fsm_html.redact_page(old, ["Élodie KÄRKKÄINEN"])
    assert all(b < 0x80 for b in new)
    assert b">Nimi poistettu pyynn&#xF6;st&#xE4;</td>" in new


def test_index_parsing():
    text, _ = fsm_html.decode(_event()["index.htm"])
    cats = fsm_html.parse_index(text)
    assert [(c.number, c.name, c.entries, c.result) for c in cats] == [
        (1, fixtures.CAT1, "CAT001EN.htm", "CAT001RS.htm"),
        (2, fixtures.CAT2, "CAT002EN.htm", "CAT002RS.htm")]
    assert [s["page"] for s in cats[1].segments] == ["SEG002.htm", "SEG003.htm"]
    assert cats[1].segments[0]["pdf"].endswith("_JudgesDetailsperSkater.pdf")
    assert fsm_html.protocol_link(text) == fixtures.PROTOCOL_NAME


# ── PDF ───────────────────────────────────────────────────────────────────────

def test_pdf_redaction_keeps_other_words():
    old = fixtures.judges_details_pdf(fixtures.CAT2, "Free Skating", fixtures.CAT2_SKATERS)
    new, hits, written = pdf_redact.redact_pdf(old, ["Ville KOKEILU"], ["LIUKU"])
    assert len(hits) == 1 and hits[0].club_rects
    check = verify.pdf_invariance("x.pdf", old, new, hits, written)
    assert check["ok"], check
    doc = pymupdf.open(stream=new, filetype="pdf")
    page = doc[1]
    words = [w[4] for w in page.get_text("words")]
    assert "KOKEILU" not in words and "LIUKU" not in words
    assert "91.15" in words and "45.05" in words and "Nimi" in words
    doc.close()


def test_pdf_redaction_uncompressed_streams_hold_no_name():
    old = fixtures.judges_details_pdf(fixtures.CAT1, "Free Skating", fixtures.CAT1_SKATERS, compress=False)
    new, hits, _ = pdf_redact.redact_pdf(old, ["Hanna MALLINEN"], ["LIUKU"])
    assert hits
    assert not verify.residual_pdf("x.pdf", new, [names.name_pattern("Hanna MALLINEN")])


def test_pdf_no_match_returns_none():
    old = fixtures.judges_details_pdf(fixtures.CAT1, "Free Skating", fixtures.CAT1_SKATERS)
    new, hits, _ = pdf_redact.redact_pdf(old, ["Nobody HERE"])
    assert new is None and hits == []


# ── verification ──────────────────────────────────────────────────────────────

def test_residual_finds_name_in_attribute_and_reversed():
    page = b'<html><img alt="M&#xC4;KEL&#xC4; Aino" src="x.jpg"></html>\r\n'
    hits = verify.residual_search({"a.htm": page}, ["Aino MÄKELÄ"])
    assert hits == [{"file": "a.htm", "location": "line 1", "skater": 0}]


def test_residual_finds_name_in_pdf_metadata():
    doc = pymupdf.open()
    doc.new_page()
    doc.set_metadata({"author": "Örjan Väänänen"})
    data = doc.tobytes()
    hits = verify.residual_search({"m.pdf": data}, ["Örjan VÄÄNÄNEN"])
    assert hits and all(h["skater"] == 0 for h in hits)


def test_html_invariance_detects_changed_score():
    old = _event()["CAT001RS.htm"]
    new, _ = fsm_html.redact_page(old, ["Aino MÄKELÄ"])
    assert verify.html_invariance("p", old, new)["ok"]
    tampered = new.replace(b"55.48", b"55.49")
    check = verify.html_invariance("p", old, tampered)
    assert not check["ok"] and "row 2" in check["problems"][0]


# ── fetcher ───────────────────────────────────────────────────────────────────

def test_link_classification():
    base = fixtures.BASE
    page = base + "index.htm"
    assert fetcher.classify_link(base, page, "CAT001RS.htm") == ("dir", "CAT001RS.htm")
    assert fetcher.classify_link(base, page, f"http://{fixtures.HOST}{fixtures.EVENT_DIR}SEG001.htm") == ("dir", "SEG001.htm")
    assert fetcher.classify_link(base, page, "../Styles.css")[0] == "shared"
    assert fetcher.classify_link(base, page, "https://other.example/x.htm") == (None, None)
    assert fetcher.classify_link(base, page, "mailto:x@example.com") == (None, None)
    assert fetcher.classify_link(base, page, "sub/../../x.htm")[0] == "shared"


def test_event_location():
    assert fetcher.event_location(fixtures.BASE)[2] == "index.htm"
    assert fetcher.event_location(fixtures.BASE[:-1])[0] == fixtures.BASE
    with pytest.raises(fetcher.FetchError):
        fetcher.event_location("ftp://results.example.test/x/")
    with pytest.raises(fetcher.FetchError):
        fetcher.event_location("https://user:pw@results.example.test/x/")


class _Handler(http.server.BaseHTTPRequestHandler):
    pages = {}
    seen = []

    def do_GET(self):
        self.seen.append(self.path)
        if self.path == "/results/2627/TEST01/redirect.htm":
            self.send_response(302)
            self.send_header("Location", "http://evil.example.org/steal")
            self.end_headers()
            return
        data = self.pages.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    files, shared = fixtures.build_event("fsm")
    pages = {fixtures.EVENT_DIR + rel: data for rel, data in files.items()}
    pages.update(shared)
    pages["/bios/isufs90001.htm"] = b"bio"
    _Handler.pages, _Handler.seen = pages, []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host = f"127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("ALLOWED_RESULT_HOSTS", host)
    yield host, _Handler
    srv.shutdown()


def test_fetch_event_over_http(server, tmp_path):
    host, handler = server
    event = fetcher.fetch_event(f"http://{host}{fixtures.EVENT_DIR}index.htm", tmp_path)
    served = {p[len(fixtures.EVENT_DIR):]: d for p, d in handler.pages.items()
              if p.startswith(fixtures.EVENT_DIR)}
    expected = {rel for rel in served if rel != fixtures.PROTOCOL_NAME}
    assert set(event.files) == expected
    assert all(event.read(rel) == served[rel] for rel in expected)
    assert not any(p.startswith("/bios/") for p in handler.seen)
    assert {u.split(host, 1)[1] for u in event.shared} == {
        "/results/2627/Styles.css", "/results/jquery.js",
        "/results/2627/flags/KUULA.GIF", "/results/2627/flags/PIRUETTI.GIF", "/results/2627/flags/LIUKU.GIF"}


def test_redirect_off_allow_list_is_refused(server):
    host, _ = server
    with pytest.raises(fetcher.FetchError) as e:
        fetcher.http_get(f"http://{host}{fixtures.EVENT_DIR}redirect.htm")
    assert e.value.code == "host_not_allowed"


def test_not_allowed_host_never_contacted(server):
    host, handler = server
    with pytest.raises(fetcher.FetchError):
        fetcher.http_get("http://localhost:1/x.htm")
    assert handler.seen == []


def test_protocol_output_path():
    assert fetcher.protocol_output_path(fixtures.BASE, fixtures.BASE + "p.pdf") == "p.pdf"
    assert fetcher.protocol_output_path(fixtures.BASE, f"https://{fixtures.HOST}/other/p2.pdf") == "p2.pdf"
