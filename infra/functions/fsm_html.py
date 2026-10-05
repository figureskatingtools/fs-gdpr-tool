"""
FS Manager HTML pages: decoding, table parsing and byte-preserving edits.

FS Manager writes plain HTML 4 pages: ASCII with numeric entities for anything
else (`&#xF6;`), CRLF line endings, unquoted attribute values in places and
nested tables (the club cell holds a one-row table with the flag image). Older
or hand-edited sites may carry raw windows-1252 or UTF-8 bytes instead.

Nothing here re-serialises a page through a DOM. A page is decoded with a codec
that round-trips its bytes exactly, the table/row/cell boundaries are located
with a small tag scanner, and edits are spliced into the original text at those
offsets — every byte outside an edited cell stays identical.

Page kinds, by file name:
  CATnnnEN.htm  entries           CATnnnRS.htm  category result
  SEGnnn.htm    starting order / detailed classification (segment)
  SEGnnnOF.htm  panel of judges   index.htm     event index
Only entries, result and segment pages carry skaters.
"""
import html
import re
from dataclasses import dataclass, field

import names

REPLACEMENT = "Nimi poistettu pyynnöstä"

PAGE_ENTRIES = "entries"
PAGE_RESULT = "result"
PAGE_SEGMENT = "segment"
PAGE_PANEL = "panel"
PAGE_INDEX = "index"

_RE_CAT = re.compile(r"(?i)^CAT(\d+)(EN|RS)\.html?$")
_RE_SEG = re.compile(r"(?i)^SEG(\d+)(OF)?\.html?$")
_RE_CHARSET = re.compile(rb"(?i)<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9_-]+)")
_RE_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)\b[^>]*>")
_RE_ATTR_CLASS = re.compile(r"""(?i)\bclass\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_RE_IMG = re.compile(r"(?i)<img\b[^>]*>")
_RE_ANY_TAG = re.compile(r"<[^>]*>")
_RE_HREF = re.compile(r"""(?i)\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_STRUCT_TAGS = {"table", "tr", "td", "th"}


def page_kind(filename: str):
    """(kind, number) for an FS Manager file name, or (None, None)."""
    base = filename.rsplit("/", 1)[-1]
    m = _RE_CAT.match(base)
    if m:
        return (PAGE_ENTRIES if m.group(2).upper() == "EN" else PAGE_RESULT), int(m.group(1))
    m = _RE_SEG.match(base)
    if m:
        return (PAGE_PANEL if m.group(2) else PAGE_SEGMENT), int(m.group(1))
    if base.lower() in ("index.htm", "index.html"):
        return PAGE_INDEX, None
    return None, None


# ── decoding ──────────────────────────────────────────────────────────────────

class EncodingError(ValueError):
    pass


def decode(data: bytes):
    """Return (text, codec) such that text.encode(codec) == data."""
    candidates = []
    m = _RE_CHARSET.search(data[:4096])
    if m:
        candidates.append(m.group(1).decode("ascii").lower())
    candidates += ["utf-8", "cp1252", "latin-1"]
    for codec in candidates:
        try:
            text = data.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.encode(codec) == data:
            return text, codec
    raise EncodingError("page bytes do not round-trip through any known codec")


def encode_replacement(text: str, original: bytes, codec: str) -> str:
    """The replacement as it has to appear in this page's source: numeric
    entities in FS Manager's own style when the page is pure ASCII, the raw
    letters otherwise (the page's codec can hold them)."""
    if all(b < 0x80 for b in original):
        return "".join(c if ord(c) < 0x80 else f"&#x{ord(c):X};" for c in text)
    try:
        text.encode(codec)
        return text
    except UnicodeEncodeError:
        return text.encode(codec, errors="xmlcharrefreplace").decode(codec)


# ── tag scanner ───────────────────────────────────────────────────────────────

@dataclass
class Node:
    tag: str
    start: int            # '<' of the open tag
    open_end: int         # just after the open tag
    attrs: str
    parent: "Node" = None
    close_start: int = -1  # '<' of the close tag (or end of parent content)
    end: int = -1          # just after the close tag
    children: list = field(default_factory=list)

    @property
    def css_class(self) -> str:
        m = _RE_ATTR_CLASS.search(self.attrs)
        return (m.group(1) or m.group(2) or m.group(3) or "") if m else ""

    def kids(self, tag):
        return [c for c in self.children if c.tag == tag]


def parse_structure(text: str):
    """Scan table/tr/td/th tags into a tree. Tolerates missing close tags the way
    browsers do: closing an element closes everything opened inside it."""
    root = Node("#root", 0, 0, "")
    stack = [root]
    for m in _RE_TAG.finditer(text):
        closing, tag = m.group(1) == "/", m.group(2).lower()
        if tag not in _STRUCT_TAGS:
            continue
        if not closing:
            # An unclosed td/th/tr is implicitly closed by a sibling-level tag.
            if tag in ("td", "th"):
                _close_until(stack, ("td", "th"), m.start(), stop_at=("tr", "table"))
            elif tag == "tr":
                _close_until(stack, ("tr",), m.start(), stop_at=("table",))
            node = Node(tag, m.start(), m.end(), m.group(0), parent=stack[-1])
            stack[-1].children.append(node)
            stack.append(node)
        else:
            if any(n.tag == tag for n in stack[1:]):
                while stack[-1].tag != tag:
                    n = stack.pop()
                    n.close_start = n.end = m.start()
                n = stack.pop()
                n.close_start, n.end = m.start(), m.end()
    while len(stack) > 1:
        n = stack.pop()
        n.close_start = n.end = len(text)
    return root


def _close_until(stack, tags, pos, stop_at):
    for i in range(len(stack) - 1, 0, -1):
        if stack[i].tag in stop_at:
            return
        if stack[i].tag in tags:
            while len(stack) > i:
                n = stack.pop()
                n.close_start = n.end = pos
            return


def _walk(node):
    yield node
    for c in node.children:
        yield from _walk(c)


def inner(text: str, node: Node) -> str:
    return text[node.open_end:node.close_start]


def cell_text(text: str, node: Node) -> str:
    """Visible text of a cell: tags dropped, entities resolved, spaces collapsed."""
    raw = _RE_ANY_TAG.sub(" ", inner(text, node))
    return re.sub(r"\s+", " ", html.unescape(raw).replace(" ", " ")).strip()


# ── skater tables ─────────────────────────────────────────────────────────────

@dataclass
class Row:
    node: Node
    cells: list            # direct td nodes
    texts: list            # visible text per cell
    name_col: int
    club_col: int          # -1 when the table has no club/nation column

    @property
    def name(self) -> str:
        return self.texts[self.name_col] if self.name_col < len(self.texts) else ""

    @property
    def club(self) -> str:
        if 0 <= self.club_col < len(self.texts):
            return self.texts[self.club_col]
        return ""

    @property
    def placement(self) -> str:
        return self.texts[0] if self.texts and self.name_col > 0 else ""


def _rows(table: Node):
    """Direct rows of a table, looking through the tbody-less nesting FS
    Manager never uses but browsers would accept."""
    return table.kids("tr")


def _header_cols(text, table):
    """Column indexes of Name and Club/Nation from a table's header row."""
    for tr in _rows(table):
        heads = tr.kids("th")
        if not heads:
            continue
        labels = [names.fold(cell_text(text, th)).rstrip(".:") for th in heads]
        if "name" not in labels:
            return None
        name_col = labels.index("name")
        club_col = next((i for i, lab in enumerate(labels)
                         if lab in ("nation", "club", "nat", "country")), -1)
        return name_col, club_col
    return None


def skater_rows(text: str, root: Node = None):
    """Every skater row of a page: rows of tables whose header has a 'Name'
    column. The officials table on segment pages has no such header and the
    club cell's own one-row table has no header at all, so neither matches."""
    root = root or parse_structure(text)
    out = []
    for table in (n for n in _walk(root) if n.tag == "table"):
        cols = _header_cols(text, table)
        if not cols:
            continue
        name_col, club_col = cols
        for tr in _rows(table):
            if tr.kids("th"):
                continue
            cells = tr.kids("td")
            if len(cells) <= name_col:
                continue
            texts = [cell_text(text, td) for td in cells]
            if not texts[name_col]:
                continue
            out.append(Row(tr, cells, texts, name_col, club_col))
    return out


# ── index page ────────────────────────────────────────────────────────────────

@dataclass
class IndexCategory:
    number: int
    name: str
    entries: str = ""        # relative file names as linked
    result: str = ""
    segments: list = field(default_factory=list)   # [{name, page, panel, pdf}]


def _hrefs(fragment: str):
    return [next(g for g in m.groups() if g is not None) for m in _RE_HREF.finditer(fragment)]


def parse_index(text: str):
    """Categories in index order with the files linked for each. A category row
    links CATnnnEN/CATnnnRS; the rows after it, until the next category row,
    are its segments (SEGnnnOF, SEGnnn and the judges-scores PDF)."""
    root = parse_structure(text)
    cats, current, current_table = [], None, None
    for tr in (n for n in _walk(root) if n.tag == "tr"):
        if tr.kids("tr") or any(c.kids("table") for c in tr.kids("td")):
            continue                      # layout rows wrapping whole tables
        frag = inner(text, tr)
        links = [h.strip().strip('"\'') for h in _hrefs(frag)]
        base = [h.rsplit("/", 1)[-1] for h in links]
        cat_links = [(h, b) for h, b in zip(links, base) if _RE_CAT.match(b)]
        if cat_links:
            num = int(_RE_CAT.match(cat_links[0][1]).group(1))
            cells = tr.kids("td")
            label = cell_text(text, cells[0]) if cells else f"Category {num}"
            current = IndexCategory(num, label or f"Category {num}")
            current_table = tr.parent
            for h, b in cat_links:
                if _RE_CAT.match(b).group(2).upper() == "EN":
                    current.entries = h
                else:
                    current.result = h
            cats.append(current)
            continue
        seg_links = [(h, b) for h, b in zip(links, base) if _RE_SEG.match(b)]
        # Segment rows belong to the category table; the time schedule further
        # down links the same SEG pages and must not be read as segments.
        if current is not None and seg_links and tr.parent is current_table:
            cells = tr.kids("td")
            seg = {"name": "", "page": "", "panel": "", "pdf": ""}
            for td in cells:
                t = cell_text(text, td)
                if t and not _hrefs(inner(text, td)) and not seg["name"]:
                    seg["name"] = t
            for h, b in seg_links:
                if _RE_SEG.match(b).group(2):
                    seg["panel"] = h
                else:
                    seg["page"] = h
            pdfs = [h for h in links if h.lower().endswith(".pdf")]
            if pdfs:
                seg["pdf"] = pdfs[0]
            current.segments.append(seg)
    return cats


def protocol_link(text: str):
    """The 'Download Event protocol' form action, when the index has one."""
    m = re.search(r"""(?i)<form[^>]+action\s*=\s*["']?([^"'\s>]+\.pdf)""", text)
    return m.group(1) if m else None


# ── editing ───────────────────────────────────────────────────────────────────

def _blank_text_nodes(fragment: str) -> str:
    """Drop the flag image and every visible text run of a club cell, keeping
    its markup (the nested one-row table) and whitespace so the layout and the
    diff stay minimal."""
    fragment = _RE_IMG.sub("", fragment)
    parts = re.split(r"(<[^>]*>)", fragment)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        visible = html.unescape(part).replace(" ", " ")
        if visible.strip():
            lead = part[:len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            parts[i] = lead + trail
    return "".join(parts)


@dataclass
class Edit:
    start: int
    end: int
    new: str


def redact_rows(text: str, rows, replacement_src: str):
    """Edits replacing the name cell content of each row with the replacement
    (dropping the bios link) and emptying its club cell (flag and abbreviation).
    Every other cell — placement, start number, scores — is untouched."""
    edits = []
    for row in rows:
        name_td = row.cells[row.name_col]
        edits.append(Edit(name_td.open_end, name_td.close_start, replacement_src))
        if 0 <= row.club_col < len(row.cells):
            club_td = row.cells[row.club_col]
            old = inner(text, club_td)
            new = _blank_text_nodes(old)
            if new != old:
                edits.append(Edit(club_td.open_end, club_td.close_start, new))
    return edits


def apply_edits(text: str, edits) -> str:
    for e in sorted(edits, key=lambda e: e.start, reverse=True):
        text = text[:e.start] + e.new + text[e.end:]
    return text


def redact_page(data: bytes, skater_names):
    """Redact every row of `data` whose name matches one of `skater_names`.
    Returns (new_bytes, matched) where matched is a list of
    (name_index, row_number) pairs; new_bytes is `data` itself when nothing
    matched."""
    text, codec = decode(data)
    rows = skater_rows(text)
    targets, matched = [], []
    keys = [names.name_key(n) for n in skater_names]
    for r_i, row in enumerate(rows):
        k = names.name_key(row.name)
        for n_i, key in enumerate(keys):
            if key and k == key:
                targets.append(row)
                matched.append((n_i, r_i))
                break
    if not targets:
        return data, []
    src = encode_replacement(REPLACEMENT, data, codec)
    new_text = apply_edits(text, redact_rows(text, targets, src))
    return new_text.encode(codec), matched


def table_snapshot(data: bytes):
    """Every skater row as its list of cell texts — what verification compares."""
    text, _ = decode(data)
    return [list(r.texts) for r in skater_rows(text)], [(r.name_col, r.club_col) for r in skater_rows(text)]
