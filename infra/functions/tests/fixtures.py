"""
Synthetic FS Manager event + protocol, built in code.

Everything here is invented — names, clubs, scores, the host. The markup mimics
what FS Manager publishes (nested tables, Line1Red/Line2Red rows, the club cell's
one-row flag table, `/bios/` links on names, absolute `http://` links back to the
index, CRLF line endings, numeric entities for non-ASCII letters), the PDFs mimic
the judges-details sheets and the protocol generator's page types. Never put
real competition data in here.

Cast (name, club):
  Category 1 "Testisarja A" (Free Skating only)
    1 Aino MÄKELÄ       KUULA      ← selected in the main scenario (podium)
    2 Örjan VÄÄNÄNEN    PIRUETTI
    3 Sami SIIVONEN     KUULA      ← also in category 2, selected in both
    4 Hanna MALLINEN    LIUKU
    WD Élodie KÄRKKÄINEN PIRUETTI   ← withdrawn, selected
  Category 2 "Testisarja B" (Short Program + Free Skating)
    1 Sami SIIVONEN     KUULA
    2 Ville KOKEILU     LIUKU
    3 Liisa TESTINEN    PIRUETTI
    4 Mikko ESIMERKKI   KUULA
"""
import io
import os
import unicodedata

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

HOST = "results.example.test"
EVENT_DIR = "/results/2627/TEST01/"
BASE = f"https://{HOST}{EVENT_DIR}"
INDEX_URL = BASE + "index.htm"
PROTOCOL_NAME = "protocol_testikilpailu.pdf"

CAT1 = "Testisarja A"
CAT2 = "Testisarja B"

CAT1_SKATERS = [
    # (place, start no, name, club, total, tes, pcs, ded)
    ("1", "3", "Aino MÄKELÄ", "KUULA", "61.20", "31.10", "30.10", "0.00"),
    ("2", "1", "Örjan VÄÄNÄNEN", "PIRUETTI", "55.48", "27.03", "28.45", "0.00"),
    ("3", "4", "Sami SIIVONEN", "KUULA", "50.02", "24.60", "26.42", "1.00"),
    ("4", "2", "Hanna MALLINEN", "LIUKU", "44.91", "21.55", "23.36", "0.00"),
]
CAT1_WD = ("WD", "5", "Élodie KÄRKKÄINEN", "PIRUETTI")

CAT2_SKATERS = [
    ("1", "2", "Sami SIIVONEN", "KUULA", "98.70", "50.20", "48.50", "0.00"),
    ("2", "4", "Ville KOKEILU", "LIUKU", "91.15", "45.05", "46.10", "0.00"),
    ("3", "1", "Liisa TESTINEN", "PIRUETTI", "84.32", "42.12", "42.20", "0.00"),
    ("4", "3", "Mikko ESIMERKKI", "KUULA", "77.01", "37.61", "40.40", "1.00"),
]

OFFICIALS = [("Referee", "Tuomo ESIMERKKINEN"), ("Technical Controller", "Raili MALLIKAS")]

SELECTED = ["Aino MÄKELÄ", "Élodie KÄRKKÄINEN", "Sami SIIVONEN"]
ALL_NAMES = ([s[2] for s in CAT1_SKATERS] + [CAT1_WD[2]] + [s[2] for s in CAT2_SKATERS[1:]])

CRLF = "\r\n"


# ── HTML ──────────────────────────────────────────────────────────────────────

def _esc(text, style):
    if style == "fsm":
        return "".join(c if ord(c) < 0x80 else f"&#x{ord(c):X};" for c in text)
    if style == "nfd":
        return unicodedata.normalize("NFD", text)
    return text


def _page(title, caption2, caption3, body, style):
    charset = ""
    if style == "nfd":
        charset = ' <meta http-equiv="Content-Type" content="text/html; charset=utf-8">' + CRLF
    head = (
        "<html>" + CRLF + "<head>" + CRLF
        + f" <title>{title}</title>" + CRLF + charset
        + ' <meta name="generator" content="FS Manager by Swiss Timing, Ltd.">' + CRLF
        + ' <link href="../Styles.css" rel="stylesheet" type="text/css" media="screen">' + CRLF
        + ' <script language="JavaScript" src="/results/jquery.js"></script>' + CRLF
        + "</head>" + CRLF + "<body>" + CRLF
        + '<table width="100%" border="0" cellspacing="0" cellpadding="0">' + CRLF
        + f'<tr><td><a href="http://{HOST}{EVENT_DIR}index.htm"><img src="evt_header.jpg" border="0"></a></td></tr>' + CRLF
        + '<tr class="EmptyLine14"><td>&nbsp;</td></tr>' + CRLF
        + f'<tr class="caption2"><td>{caption2}</td></tr>' + CRLF
        + '<tr class="EmptyLine14"><td> &nbsp; </td></tr>' + CRLF
        + f'<tr class="caption3"><td>{caption3}</td></tr>' + CRLF
        + '<tr class="EmptyLine14"><td> &nbsp; </td></tr>' + CRLF
    )
    tail = "</table>" + CRLF + "</body>" + CRLF + "</html>" + CRLF
    return head + body + tail


def _wrap_table(header_cells, rows, width="70%", head_class="TabHeadRed"):
    out = ("<tr>" + CRLF + "  <td>" + CRLF
           + f'    <table width="{width}" border="0" align="center" cellpadding="0" cellspacing="1" bgcolor="#606060">' + CRLF
           + "    <tr>" + CRLF + "      <td>" + CRLF
           + '        <table width="100%" align="center" border="0" cellspacing="1">' + CRLF
           + f'          <tr class="{head_class}">' + "".join(f"<th>{h}</th>" for h in header_cells) + "</tr>" + CRLF)
    out += "".join(rows)
    out += ("        </table>" + CRLF + "      </td>" + CRLF + "    </tr>" + CRLF
            + "    </table>" + CRLF + "  </td>" + CRLF + "</tr>" + CRLF)
    return out


def _name_cell(name, n, style):
    return (f'<td class="CellLeft"><a href="/bios/isufs{90000 + n}.htm" target="_blank" '
            f'class="disableBiosLink">{_esc(name, style)}</a></td>')


def _club_cell(club, line_class):
    return (f'<td><table><tr class="{line_class}"><td><img src="../flags/{club}.GIF"></td>'
            f"<td></td><td>{club}</td></tr></table></td>")


def _uid(name):
    return sum(ord(c) for c in unicodedata.normalize("NFC", name))


def entries_page(cat_name, skaters, style):
    rows = []
    for i, (_, start, name, club) in enumerate(skaters):
        lc = f"Line{1 + i % 2}Red"
        rows.append(f'          <tr class="{lc}">' + CRLF
                    + f"            <td>{i + 1}</td>" + CRLF
                    + "            " + _name_cell(name, _uid(name), style) + CRLF
                    + "            " + _club_cell(club, lc) + CRLF
                    + "          </tr>" + CRLF)
    body = _wrap_table(["No.", "Name", "Nation"], rows)
    return _page("Testikilpailu", _esc(cat_name, style), "Entries", body, style)


def result_page(cat_name, placed, wd, seg_cols, style):
    rows = []
    allrows = [(p[0], p[2], p[3], p[4], [p[0]] * len(seg_cols)) for p in placed]
    if wd:
        allrows.append((wd[0], wd[2], wd[3], "", ["WD"] + [""] * (len(seg_cols) - 1)))
    for i, (place, name, club, total, segs) in enumerate(allrows):
        lc = f"Line{1 + i % 2}Red"
        rows.append(f'          <tr class="{lc}">' + CRLF
                    + f"            <td>{place}</td>" + CRLF
                    + "            " + _name_cell(name, _uid(name), style) + CRLF
                    + "            " + _club_cell(club, lc) + CRLF
                    + f'            <td align="right">{total}</td>'
                    + "".join(f'<td align="center">{s}</td>' for s in segs) + CRLF
                    + "          </tr>" + CRLF)
    body = _wrap_table(["FPl.", "Name", "Nation", "Points"] + seg_cols, rows)
    return _page("Testikilpailu", _esc(cat_name, style), "Result", body, style)


def segment_page(cat_name, seg_name, placed, wd, style):
    rows = []
    allrows = list(placed) + ([(wd[0], wd[1], wd[2], wd[3], "", "", "", "")] if wd else [])
    for i, (place, start, name, club, total, tes, pcs, ded) in enumerate(allrows):
        lc = f"Line{1 + i % 2}Blue"
        rows.append(f'          <tr class="{lc}">' + CRLF
                    + f'            <td align="center">{place}</td>' + CRLF
                    + "            " + _name_cell(name, _uid(name), style) + CRLF
                    + f"            <td>{club}</td>" + CRLF
                    + f'            <td align="right">{total}</td> <td align="right">{tes}</td> <td>&nbsp;</td>'
                    + f'<td align="right">{pcs}</td> <td align="right">{ded}</td> <td align="center">#{start}</td>' + CRLF
                    + "          </tr>" + CRLF)
    body = _wrap_table(["&nbsp; Pl. &nbsp;", "Name", "Nation", "TSS<br/>=", "TES<br/>+", "&nbsp;",
                        "PCS<br/>+", "Ded.<br/>-", "StN."], rows, width="95%", head_class="TabHeadBlue")
    officials = []
    for i, (role, person) in enumerate(OFFICIALS):
        officials.append(f'          <tr class="Line{1 + i % 2}White"><td class="CellLeft">{role}</td>'
                         f'<td class="CellLeft">{_esc(person, style)}</td></tr>' + CRLF)
    body += '<tr class="EmptyLine22"><td> &nbsp; </td></tr>' + CRLF
    body += (("<tr>" + CRLF + "  <td>" + CRLF
              + '    <table width="70%" border="0" align="center" cellpadding="0" cellspacing="1" bgcolor="#606060">' + CRLF
              + "    <tr>" + CRLF + "      <td>" + CRLF
              + '        <table width="100%" align="center" border="0" cellspacing="1">' + CRLF
              + '          <tr class="TabHeadWhite"><td>Function</td><td> </td></tr>' + CRLF)
             + "".join(officials)
             + ("        </table>" + CRLF + "      </td>" + CRLF + "    </tr>" + CRLF
                + "    </table>" + CRLF + "  </td>" + CRLF + "</tr>" + CRLF))
    return _page("Testikilpailu", _esc(cat_name, style) + " - " + seg_name, "Judges Scores", body, style)


def panel_page(cat_name, seg_name, style):
    rows = []
    for i, (role, person) in enumerate(OFFICIALS):
        rows.append(f'          <tr class="Line{1 + i % 2}White"><td class="CellLeft">{role}</td>'
                    f'<td class="CellLeft">{_esc(person, style)}</td></tr>' + CRLF)
    body = _wrap_table(["Function", "Official"], rows, head_class="TabHeadWhite")
    return _page("Testikilpailu", _esc(cat_name, style) + " - " + seg_name, "Panel of Judges", body, style)


SEGMENTS = {
    1: [("SEG001", "Free Skating", "FSKXTEST-CATA----FNL-000100--_JudgesDetailsperSkater.pdf")],
    2: [("SEG002", "Short Program", "FSKXTEST-CATB----QUAL000100--_JudgesDetailsperSkater.pdf"),
        ("SEG003", "Free Skating", "FSKXTEST-CATB----FNL-000100--_JudgesDetailsperSkater.pdf")],
}


def index_page(style):
    rows = []
    for num, name in ((1, CAT1), (2, CAT2)):
        rows.append('          <tr class="Line1Red">' + CRLF
                    + f'            <td class="CellLeft">{_esc(name, style)} </td>' + CRLF
                    + "            <td></td>" + CRLF
                    + f'            <td class="CellLeft">' + CRLF + f"              <a href=CAT{num:03d}EN.htm> Entries </a>" + CRLF + "            </td>" + CRLF
                    + f'            <td class="CellLeft">' + CRLF + f"              <a href=CAT{num:03d}RS.htm> Result </a>" + CRLF + "            </td>" + CRLF
                    + "            <td>&nbsp;</td>" + CRLF
                    + "          </tr>" + CRLF)
        for seg, seg_name, pdf in SEGMENTS[num]:
            rows.append('          <tr class="Line1Red">' + CRLF
                        + '            <td class="CellLeft" valign="top"></td>' + CRLF
                        + f'            <td class="CellLeft">{seg_name}</td>' + CRLF
                        + f'            <td class="CellLeft">' + CRLF + f"              <a href={seg}OF.htm>Panel of Judges</a>" + CRLF + "            </td>" + CRLF
                        + f'            <td class="CellLeft">' + CRLF + f"              <a href={seg}.htm> Starting Order / Result Details </a>" + CRLF + "            </td>" + CRLF
                        + f"            <td>" + CRLF + f'              <a href={pdf} target=&quot;_blank&quot;> Judges Scores &nbsp;(pdf)</a>' + CRLF + "            </td>" + CRLF
                        + "          </tr>" + CRLF)
    cats = _wrap_table(["Category", "Segment", "&nbsp;", "&nbsp;", "Reports"], rows, width="80%", head_class="TabHeadWhite")
    # A time schedule further down links the segment pages again.
    sched = []
    for i, (seg, seg_name, _) in enumerate(SEGMENTS[1] + SEGMENTS[2]):
        sched.append(f'          <tr class="Line{1 + i % 2}Red"><td>11.09.2026</td><td>{10 + i}:00</td>'
                     f'<td class="CellLeft"><a href={seg}.htm>{seg_name}</a></td></tr>' + CRLF)
    sched_table = _wrap_table(["Date", "Time", "Segment"], sched, width="80%", head_class="TabHeadWhite")
    proto = ('<tr class="caption3">' + CRLF
             + f'  <td><form action="{PROTOCOL_NAME}" method="link"><input type="submit" value="Download Event protocol"></form></td>' + CRLF
             + "</tr>" + CRLF)
    footer = ('<tr><td><a href="https://www.isu-skating.com/">ISU</a> '
              '<a href="http://www.st-sportservice.com">Swiss Timing</a></td></tr>' + CRLF)
    return _page("Testikilpailu", "Testikilpailu", "11.09.2026 - 13.09.2026", proto + cats + sched_table + footer, style)


def _encode(text, style):
    if style == "fsm":
        return text.encode("ascii")
    if style == "cp1252":
        return text.encode("cp1252")
    return text.encode("utf-8")


# ── PDFs ──────────────────────────────────────────────────────────────────────

def _fonts():
    import reportlab
    fdir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    if "Vera" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Vera", os.path.join(fdir, "Vera.ttf")))
        pdfmetrics.registerFont(TTFont("VeraBd", os.path.join(fdir, "VeraBd.ttf")))


def _fsm_sheet_header(c, title, heading):
    w, h = A4
    c.setFont("VeraBd", 9)
    c.drawString(23, h - 71, title)
    c.drawString(23, h - 89, heading)
    c.setFont("Vera", 7)
    c.drawString(23, h - 740 - 20, "printed:   11.09.2026 17.25")
    c.drawString(546, h - 741 - 20, "Page 1 / 1")


def judges_details_pdf(cat_name, seg_name, placed, compress=True):
    """One page per skater, laid out like FS Manager's judges details."""
    _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=1 if compress else 0)
    c.setTitle("JudgesDetailsperSkater")
    w, h = A4
    for rank, start, name, club, total, tes, pcs, ded in placed:
        _fsm_sheet_header(c, "JUDGES DETAILS PER SKATER", f"{cat_name}  {seg_name}")
        c.setFont("VeraBd", 7)
        for x, t in ((39, "Rank"), (62, "Name"), (267, "Nation"), (325, "Starting Number"),
                     (384, "Total Segment Score"), (460, "Total Element Score"), (530, "Deductions")):
            c.drawString(x, h - 119, t)
        y = h - 140
        c.drawRightString(56, y, rank)
        c.drawString(62, y, name)
        c.drawString(267, y, club)
        c.drawRightString(352, y, start)
        c.drawRightString(410, y, total)
        c.drawRightString(458, y, tes)
        c.drawRightString(520, y, pcs)
        c.drawRightString(570, y, ded)
        c.setFont("Vera", 7)
        for i, (el, bv, goe) in enumerate((("2A", "3.30", "0.66"), ("3S", "4.30", "-0.43"), ("CCoSp4", "3.50", "0.70"))):
            yy = h - 172 - i * 8
            c.drawString(35, yy, str(i + 1))
            c.drawString(45, yy, el)
            c.drawRightString(225, yy, bv)
            c.drawRightString(255, yy, goe)
        c.showPage()
    c.save()
    return buf.getvalue()


def _jpeg(seed):
    from PIL import Image
    img = Image.new("RGB", (64, 48), (seed * 40 % 255, 120, 200 - seed * 30 % 200))
    for x in range(64):
        img.putpixel((x, x % 48), (255, seed * 17 % 255, 0))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return out.getvalue()


def protocol_pdf():
    """A small protocol in the protocol generator's page order:
    cover, per category: podium, results (with a Withdrawn list), per segment
    segment results, panel of judges, judges details per skater; last page."""
    from reportlab.lib.utils import ImageReader
    _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    c.setFont("VeraBd", 24)
    c.drawString(60, h - 300, "Testikilpailu")
    c.setFont("Vera", 10)
    c.drawString(60, h - 330, "O F F I C I A L   P R O T O C O L")
    c.showPage()

    def podium(cat_name, top3, seed):
        c.setFont("Vera", 9)
        c.drawCentredString(w / 2, h - 90, "P O D I U M")
        c.setFont("VeraBd", 14)
        c.drawCentredString(w / 2, h - 110, cat_name)
        c.drawImage(ImageReader(io.BytesIO(_jpeg(seed))), 100, h - 520, 395, 380)
        for (x, y), (name, club), place in zip(((300, 600), (130, 640), (470, 660)), top3, ("1", "2", "3")):
            c.setFont("VeraBd", 11)
            c.drawCentredString(x, h - y + 30, place)
            c.drawCentredString(x, h - y, name)
            c.setFont("Vera", 9)
            c.drawCentredString(x, h - y - 14, club)
        c.showPage()

    def results(cat_name, heading, placed, wd, segment):
        _fsm_sheet_header(c, "Testikilpailu", heading)
        c.setFont("VeraBd", 8)
        c.drawString(58, h - 117, "Pl. Name")
        c.drawString(461, h - 117, "Nation")
        c.drawString(500, h - 117, "Total Segment Score" if segment else "Total Score")
        c.setFont("Vera", 8)
        c.drawString(500, h - 60, "RESULT")
        y = h - 140
        for p in placed:
            c.drawString(66, y, f"{p[0]} {p[2]}")
            c.drawString(461, y, p[3])
            c.drawRightString(560, y, p[4])
            y -= 11
        if wd:
            c.drawString(26, y, "Withdrawn")
            y -= 11
            c.drawString(75, y, wd[2])
            c.drawString(461, y, wd[3])
            c.drawString(523, y, "WD")
        c.setFont("VeraBd", 8)
        c.drawString(46, h - 301, "Ms. Raili MALLIKAS, FIN")
        c.showPage()

    def panel(heading):
        _fsm_sheet_header(c, "PANEL OF JUDGES", heading)
        c.setFont("Vera", 8)
        for i, (role, person) in enumerate(OFFICIALS):
            c.drawString(46, h - 140 - i * 11, f"{role}  {person}")
        c.showPage()

    def judges(heading, placed):
        for rank, start, name, club, total, *_ in placed:
            _fsm_sheet_header(c, "JUDGES DETAILS PER SKATER", heading)
            c.setFont("VeraBd", 7)
            c.drawString(39, h - 119, "Rank")
            c.drawString(62, h - 119, "Name")
            c.drawString(52, h - 140, rank)
            c.drawString(62, h - 140, name)
            c.drawString(267, h - 140, club)
            c.drawRightString(410, h - 140, total)
            c.showPage()

    podium(CAT1, [(s[2], s[3]) for s in CAT1_SKATERS[:3]], 1)
    results(CAT1, CAT1, CAT1_SKATERS, CAT1_WD, False)
    results(CAT1, f"{CAT1}  Free Skating", CAT1_SKATERS, CAT1_WD, True)
    panel(f"{CAT1}  Free Skating")
    judges(f"{CAT1}  Free Skating", CAT1_SKATERS)

    podium(CAT2, [(s[2], s[3]) for s in CAT2_SKATERS[:3]], 2)
    results(CAT2, CAT2, CAT2_SKATERS, None, False)
    for seg in ("Short Program", "Free Skating"):
        results(CAT2, f"{CAT2}  {seg}", CAT2_SKATERS, None, True)
        panel(f"{CAT2}  {seg}")
        judges(f"{CAT2}  {seg}", CAT2_SKATERS)

    c.setFont("Vera", 12)
    c.drawString(60, h - 400, "T H A N K   Y O U")
    c.showPage()
    c.save()
    return buf.getvalue()


# ── the event ─────────────────────────────────────────────────────────────────

def build_event(style="fsm"):
    """{relative path: bytes} of a whole event directory, plus shared assets
    under their absolute paths (keys starting with '/')."""
    files = {}
    cat1_entries = [(None, s[1], s[2], s[3]) for s in CAT1_SKATERS] + [(None, CAT1_WD[1], CAT1_WD[2], CAT1_WD[3])]
    cat2_entries = [(None, s[1], s[2], s[3]) for s in CAT2_SKATERS]
    files["index.htm"] = _encode(index_page(style), style)
    files["CAT001EN.htm"] = _encode(entries_page(CAT1, cat1_entries, style), style)
    files["CAT001RS.htm"] = _encode(result_page(CAT1, CAT1_SKATERS, CAT1_WD, ["FS"], style), style)
    files["CAT002EN.htm"] = _encode(entries_page(CAT2, cat2_entries, style), style)
    files["CAT002RS.htm"] = _encode(result_page(CAT2, CAT2_SKATERS, None, ["SP", "FS"], style), style)
    for num, cat, placed, wd in ((1, CAT1, CAT1_SKATERS, CAT1_WD), (2, CAT2, CAT2_SKATERS, None)):
        for seg, seg_name, pdf in SEGMENTS[num]:
            files[f"{seg}.htm"] = _encode(segment_page(cat, seg_name, placed, wd, style), style)
            files[f"{seg}OF.htm"] = _encode(panel_page(cat, seg_name, style), style)
            files[pdf] = judges_details_pdf(cat, seg_name, placed)
    files["evt_header.jpg"] = _jpeg(7)
    files[PROTOCOL_NAME] = protocol_pdf()
    shared = {
        "/results/2627/Styles.css": b"body { font-family: Arial; }\r\n",
        "/results/jquery.js": b"/* jquery stand-in */\r\n",
    }
    for club in ("KUULA", "PIRUETTI", "LIUKU"):
        shared[f"/results/2627/flags/{club}.GIF"] = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
    return files, shared


def url_map(style="fsm"):
    """URL -> bytes for the fake HTTP layer."""
    files, shared = build_event(style)
    out = {BASE + rel: data for rel, data in files.items()}
    for path, data in shared.items():
        out[f"https://{HOST}{path}"] = data
    # A skater bio outside the event dir: must never be fetched.
    out[f"https://{HOST}/bios/isufs90001.htm"] = b"<html>bio</html>"
    return out
