"""
Post-generation verification.

1. Residual search: every file of the event *as it will be published* (the
   generated version where one exists, the fetched original otherwise, the
   protocol too) is searched for every removed name, in any token order,
   case/diacritic/entity-insensitively. HTML is searched line by line in both its
   visible text and its raw source (attributes); PDFs page by page with two
   independent extractors (PyMuPDF and pypdf) plus a scan of every non-image,
   non-font object and stream (document info, XMP, outlines, annotations).
2. Invariance: in every changed HTML page the skater tables keep their row
   count and every other skater's row is unchanged cell for cell (placement,
   start number, scores, club); in a redacted row only the name and club cells
   changed. In every changed PDF each page keeps all its words except the ones
   inside a redacted box, and the protocol loses exactly the removed pages.

Results refer to skaters by index only — the caller turns them into
"selected skater N" labels — and never contain a name.
"""
import io
import re

import pymupdf
from pypdf import PdfReader

import fsm_html
import names

_TEXT_EXTS = (".htm", ".html", ".css", ".js", ".txt", ".xml", ".json")
_FONT_KEYS = ("Length1", "Length2", "Length3")
_TAG = re.compile(r"<[^>]*>")


# ── residual search ───────────────────────────────────────────────────────────

def _patterns(skater_names):
    return [names.name_pattern(n) for n in skater_names]


def _search_text(folded, patterns):
    return [(s_i, m.start()) for s_i, p in enumerate(patterns) for m in p.finditer(folded)]


def residual_html(rel, data, patterns):
    try:
        text, _ = fsm_html.decode(data)
    except fsm_html.EncodingError:
        text = data.decode("latin-1")
    hits = []
    lines = text.splitlines()
    for no, line in enumerate(lines, 1):
        found = {s for s, _ in _search_text(names.fold(_TAG.sub(" ", line)), patterns)}
        found |= {s for s, _ in _search_text(names.fold(line), patterns)}
        for s in sorted(found):
            hits.append({"file": rel, "location": f"line {no}", "skater": s})
    # A name broken across lines (rare in hand-edited pages).
    whole = names.fold(_TAG.sub(" ", text))
    for s, _ in _search_text(whole, patterns):
        if not any(h["skater"] == s for h in hits):
            hits.append({"file": rel, "location": "across lines", "skater": s})
    return hits


def _pdf_object_texts(doc):
    """Decoded text of every object and every non-image, non-font stream."""
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=False)
        except Exception:
            continue
        yield xref, obj
        if not doc.xref_is_stream(xref):
            continue
        if "/Image" in obj or any(f"/{k}" in obj for k in _FONT_KEYS) or "/FontFile" in obj:
            continue
        if re.search(r"/Subtype\s*/(Type1C|CIDFontType0C|OpenType)", obj):
            continue
        try:
            raw = doc.xref_stream(xref)
        except Exception:
            continue
        if raw.startswith(b"\xfe\xff"):
            yield xref, raw[2:].decode("utf-16-be", errors="ignore")
        try:
            yield xref, raw.decode("utf-8")
        except UnicodeDecodeError:
            yield xref, raw.decode("latin-1")


def _unescape_pdf_strings(text):
    """Resolve PDF string encodings enough for a name search: hex strings
    (<FEFF…> UTF-16 or plain bytes) and octal escapes in literal strings."""
    def hexrep(m):
        h = re.sub(r"\s", "", m.group(1))
        if len(h) % 2:
            h += "0"
        b = bytes.fromhex(h)
        if b.startswith(b"\xfe\xff"):
            return b[2:].decode("utf-16-be", errors="ignore")
        return b.decode("latin-1")
    text = re.sub(r"<([0-9A-Fa-f\s]{4,})>", hexrep, text)
    return re.sub(r"\\([0-7]{1,3})", lambda m: chr(int(m.group(1), 8)), text)


def residual_pdf(rel, data, patterns):
    hits = []
    seen = set()

    def add(location, s):
        if (location, s) not in seen:
            seen.add((location, s))
            hits.append({"file": rel, "location": location, "skater": s})

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for page in doc:
            for s, _ in _search_text(names.fold(page.get_text(), unescape=False), patterns):
                add(f"page {page.number + 1}", s)
        for xref, text in _pdf_object_texts(doc):
            folded = names.fold(_unescape_pdf_strings(text), unescape=False)
            for s, _ in _search_text(folded, patterns):
                add(f"object {xref}", s)
        for key, value in (doc.metadata or {}).items():
            for s, _ in _search_text(names.fold(value or "", unescape=False), patterns):
                add(f"metadata {key}", s)
    finally:
        doc.close()
    try:
        reader = PdfReader(io.BytesIO(data))
        for i, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            for s, _ in _search_text(names.fold(text, unescape=False), patterns):
                add(f"page {i}", s)
    except Exception:
        pass
    return hits


def residual_search(files, skater_names):
    """files: {relative path: bytes} of the final published state."""
    patterns = _patterns(skater_names)
    hits = []
    for rel in sorted(files):
        data = files[rel]
        low = rel.lower()
        if low.endswith(".pdf"):
            hits += residual_pdf(rel, data, patterns)
        elif low.endswith(_TEXT_EXTS):
            hits += residual_html(rel, data, patterns)
    return hits


# ── invariance ────────────────────────────────────────────────────────────────

def html_invariance(rel, old, new):
    """Compare skater tables of a page before and after redaction."""
    old_text, _ = fsm_html.decode(old)
    new_text, _ = fsm_html.decode(new)
    old_rows = fsm_html.skater_rows(old_text)
    new_rows = fsm_html.skater_rows(new_text)
    problems = []
    if len(old_rows) != len(new_rows):
        problems.append(f"row count changed {len(old_rows)} -> {len(new_rows)}")
    redacted = 0
    for i, (a, b) in enumerate(zip(old_rows, new_rows), 1):
        if a.texts == b.texts:
            continue
        if b.name != fsm_html.REPLACEMENT:
            problems.append(f"row {i}: changed but not a redacted row")
            continue
        redacted += 1
        skip = {a.name_col, a.club_col}
        if len(a.texts) != len(b.texts):
            problems.append(f"row {i}: cell count changed")
            continue
        for c, (x, y) in enumerate(zip(a.texts, b.texts)):
            if c in skip:
                continue
            if x != y:
                problems.append(f"row {i}: column {c + 1} changed")
        if a.club_col >= 0 and b.club:
            problems.append(f"row {i}: club still present")
    return {"file": rel, "rows": len(old_rows), "rowsAfter": len(new_rows),
            "redactedRows": redacted, "ok": not problems, "problems": problems}


def _intersects(box, rects, tol=0.5):
    x0, y0, x1, y1 = box
    for r in rects:
        if x0 < r[2] - tol and x1 > r[0] + tol and y0 < r[3] - tol and y1 > r[1] + tol:
            return True
    return False


def _words(page):
    return [(tuple(w[:4]), w[4]) for w in page.get_text("words")]


def pdf_invariance(rel, old, new, hits, written, page_map=None, removed=()):
    """Every word of every kept page survives except those in redacted boxes.

    hits:    pdf_redact.Hit list (old page numbers)
    written: {new page number: [replacement boxes]}
    page_map: old page number -> new page number (identity when None)
    removed: old page numbers deliberately removed"""
    problems = []
    redacted_by_page = {}
    for h in hits:
        redacted_by_page.setdefault(h.page, []).extend([h.rect] + list(h.club_rects))
    a = pymupdf.open(stream=old, filetype="pdf")
    b = pymupdf.open(stream=new, filetype="pdf")
    try:
        expected_pages = a.page_count - len(removed)
        if b.page_count != expected_pages:
            problems.append(f"page count {b.page_count}, expected {expected_pages}")
        for old_no in range(a.page_count):
            if old_no in removed:
                continue
            new_no = page_map.get(old_no, old_no) if page_map else old_no
            if new_no >= b.page_count:
                break
            rects = redacted_by_page.get(old_no, [])
            before = sorted(t for box, t in _words(a[old_no]) if not _intersects(box, rects))
            boxes = written.get(new_no, [])
            after = sorted(t for box, t in _words(b[new_no]) if not _intersects(box, boxes))
            if before != after:
                missing = len([t for t in before if t not in after])
                extra = len([t for t in after if t not in before])
                problems.append(f"page {new_no + 1}: other content changed "
                                f"({missing} words missing, {extra} added)")
    finally:
        a.close()
        b.close()
    return {"file": rel, "ok": not problems, "problems": problems}
