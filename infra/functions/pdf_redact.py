"""
True redaction of skater names in PDFs (PyMuPDF).

A name is located glyph by glyph (`rawdict`), so a match inside a longer text
run — the protocol's result sheets print "1 Firstname SURNAME" as one span —
covers only the name's own glyphs. Each hit becomes a redaction annotation;
`apply_redactions` then deletes the covered glyphs from the content stream
(the text layer, not an overlay box), leaving images and table rules alone.
The replacement text is written afterwards at the name's own baseline in the
name's font size (shrunk only if it would run into the next column), and the
skater's club on the same line is removed without a replacement.

Saving is always a full rewrite: `garbage=4` drops every object no longer
referenced (fonts, images of deleted pages) and merges duplicates, never an
incremental update that would keep the old revision in the file.
"""
import io
import re
from dataclasses import dataclass, field

import pymupdf

import names

REPLACEMENT = "Nimi poistettu pyynnöstä"
REPLACEMENT_FONT = "helv"           # Base-14 Helvetica: WinAnsi covers ö/ä
REPLACEMENT_FONT_BOLD = "hebo"
_MIN_FONT_SIZE = 4.0
# Glyph boxes are shrunk vertically before redacting so a box never touches the
# rows above/below (FS Manager row pitch is barely larger than the font size).
_V_INSET = 0.22
_SAME_LINE_TOL = 2.0
_COLUMN_GAP = 2.0

SAVE_OPTIONS = dict(garbage=4, deflate=True, clean=True, incremental=False)


@dataclass
class Hit:
    page: int                 # 0-based
    skater: int               # index into the names list
    rect: tuple               # union box of the name glyphs
    origin: tuple             # baseline start of the first glyph
    size: float
    bold: bool = False
    in_withdrawn: bool = False
    club_rects: list = field(default_factory=list)


def _lines(page):
    """Each text line as a list of glyphs (c, bbox, origin, size, bold)."""
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
    lines = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    chars.append((ch["c"], tuple(ch["bbox"]), tuple(ch["origin"]), span["size"],
                                  bool(span.get("flags", 0) & 16) or "bold" in span.get("font", "").lower()))
            if chars:
                lines.append(chars)
    return lines


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _inset(box):
    h = box[3] - box[1]
    return (box[0], box[1] + h * _V_INSET, box[2], box[3] - h * _V_INSET)


def _withdrawn_y(lines):
    """Baseline of a 'Withdrawn' heading line on the page, if any."""
    for chars in lines:
        text = names.fold("".join(c[0] for c in chars))
        if text in ("withdrawn", "withdrawals", "withdrawn:"):
            return chars[0][2][1]
    return None


def find_hits(doc, skater_names, clubs=None, pages=None):
    """Every occurrence of each name on the given pages (all by default)."""
    clubs = clubs or [""] * len(skater_names)
    patterns = [names.name_pattern(n) for n in skater_names]
    hits = []
    for pno in (range(doc.page_count) if pages is None else pages):
        page = doc[pno]
        lines = _lines(page)
        wd_y = _withdrawn_y(lines)
        for chars in lines:
            folded, idx = names.fold_with_map([c[0] for c in chars])
            for s_i, pat in enumerate(patterns):
                for m in pat.finditer(folded):
                    used = sorted({idx[i] for i in range(m.start(), m.end())})
                    glyphs = [chars[i] for i in used if not chars[i][0].isspace()]
                    if not glyphs:
                        continue
                    rect = _union([g[1] for g in glyphs])
                    hit = Hit(pno, s_i, rect, glyphs[0][2], glyphs[0][3], glyphs[0][4],
                              in_withdrawn=wd_y is not None and glyphs[0][2][1] > wd_y)
                    hit.club_rects = _club_rects(lines, hit, clubs[s_i])
                    hits.append(hit)
    return hits


def _club_rects(lines, hit, club):
    """Boxes of the skater's club printed on the same baseline right of the
    name (the Nation/Club column)."""
    key = names.fold(club) if club else ""
    if not key:
        return []
    pat = re.compile(r"(?<!\w)" + re.escape(key) + r"(?!\w)")
    out = []
    y = hit.origin[1]
    for chars in lines:
        folded, idx = names.fold_with_map([c[0] for c in chars])
        for m in pat.finditer(folded):
            glyphs = [chars[i] for i in sorted({idx[j] for j in range(m.start(), m.end())})
                      if not chars[i][0].isspace()]
            if not glyphs or abs(glyphs[0][2][1] - y) > _SAME_LINE_TOL:
                continue
            if glyphs[0][1][0] < hit.rect[2] - 0.5:
                continue
            out.append(_union([g[1] for g in glyphs]))
    return sorted(out)[:1]


def _next_text_x(lines, hit):
    """Left edge of the nearest text right of the name on its baseline — the
    space the replacement may use."""
    y = hit.origin[1]
    xs = [c[1][0] for chars in lines for c in chars
          if not c[0].isspace() and abs(c[2][1] - y) <= _SAME_LINE_TOL and c[1][0] >= hit.rect[2] + 0.5]
    return min(xs) if xs else None


def redact(doc, hits):
    """Apply true redaction for `hits` (grouped per page) and draw the
    replacement text. Returns {page: [replacement boxes]} for verification."""
    by_page = {}
    for h in hits:
        by_page.setdefault(h.page, []).append(h)
    written = {}
    for pno, page_hits in by_page.items():
        page = doc[pno]
        lines = _lines(page)
        plans = []
        for h in page_hits:
            nxt = _next_text_x(lines, h)
            limit = (nxt - _COLUMN_GAP) if nxt else (page.rect.width - 18)
            for r in h.club_rects:
                if r[0] > h.rect[0] and (nxt is None or r[0] <= nxt + 0.5):
                    # The club is the next column: the replacement may use the
                    # space up to whatever follows the club.
                    after = [c[1][0] for chars in lines for c in chars
                             if not c[0].isspace() and abs(c[2][1] - h.origin[1]) <= _SAME_LINE_TOL
                             and c[1][0] >= r[2] + 0.5]
                    limit = (min(after) - _COLUMN_GAP) if after else limit
            plans.append((h, limit))
            page.add_redact_annot(pymupdf.Rect(_inset(h.rect)))
            for r in h.club_rects:
                page.add_redact_annot(pymupdf.Rect(_inset(r)))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        # The applied annotations leave an empty /Annots array behind.
        if not list(page.annots()) and doc.xref_get_key(page.xref, "Annots")[0] != "null":
            doc.xref_set_key(page.xref, "Annots", "null")
        boxes = []
        for h, limit in plans:
            font = REPLACEMENT_FONT_BOLD if h.bold else REPLACEMENT_FONT
            size = h.size
            avail = max(limit - h.origin[0], 10)
            width = pymupdf.get_text_length(REPLACEMENT, fontname=font, fontsize=size)
            if width > avail:
                size = max(_MIN_FONT_SIZE, size * avail / width)
                width = pymupdf.get_text_length(REPLACEMENT, fontname=font, fontsize=size)
            page.insert_text(pymupdf.Point(h.origin), REPLACEMENT,
                             fontname=font, fontsize=size, color=(0, 0, 0))
            boxes.append((h.origin[0], h.origin[1] - size, h.origin[0] + width, h.origin[1] + size * 0.3))
        written[pno] = boxes
    return written


def save_bytes(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf, **SAVE_OPTIONS)
    return buf.getvalue()


def redact_pdf(data: bytes, skater_names, clubs=None):
    """Redact every occurrence of the names in a judges-scores PDF. Returns
    (new_bytes or None when nothing matched, hits, written_boxes)."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        hits = find_hits(doc, skater_names, clubs)
        if not hits:
            return None, [], {}
        written = redact(doc, hits)
        return save_bytes(doc), hits, written
    finally:
        doc.close()
