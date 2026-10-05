"""
Name normalisation and matching.

FS Manager writes a skater's name as "Firstname SURNAME" on most pages but other
exports (and the protocol) may print "SURNAME Firstname"; HTML pages escape
non-ASCII letters as numeric entities (`&#xF6;`), PDFs may extract a letter
either precomposed (NFC) or as base letter + combining/spacing mark (NFD, or
"a¨" from some producers). `fold()` reduces all of that to one comparable form:
entities resolved, spacing marks turned into combining ones, diacritics dropped,
case folded, whitespace collapsed. Folding diacritics away means "Mäki" also
matches "Maki" — deliberately: for a removal, finding one occurrence too many
is the safe side, and the edit is still confined to the skater's own rows.

Matching is order-insensitive on whole tokens: "Aino MÄKELÄ", "MÄKELÄ Aino" and
"mäkelä, aino" are the same name.
"""
import html
import itertools
import re
import unicodedata

# Spacing diacritics some PDF producers emit as a separate glyph before or after
# the base letter -> the combining mark NFD would have produced.
_SPACING_TO_COMBINING = {
    "¨": "̈",  # diaeresis
    "´": "́",  # acute
    "`": "̀",  # grave
    "ˆ": "̂",  # circumflex
    "˜": "̃",  # tilde
    "˚": "̊",  # ring above
    "¸": "̧",  # cedilla
    "ˇ": "̌",  # caron
}

_WS = re.compile(r"\s+")

# Separator allowed between name tokens when searching free text: whitespace
# (including line breaks inside a PDF), an optional comma, or the
# letter-spacing some generated pages use.
_SEP = r"[\s,]+"

# Above this many tokens only the written order and its reverse are searched;
# permutations of six or more tokens explode and no real name needs them.
_MAX_PERMUTED_TOKENS = 4


def fold_char(c: str) -> str:
    """Fold one character; may return '' (a combining mark) or several chars."""
    c = _SPACING_TO_COMBINING.get(c, c)
    if c in (" ", " ", " "):
        return " "
    out = []
    for d in unicodedata.normalize("NFD", c):
        if unicodedata.category(d) == "Mn":
            continue
        out.append(d)
    return "".join(out).casefold()


def fold(text: str, *, unescape: bool = True) -> str:
    """Comparable form of `text`: entities resolved, diacritics and case folded,
    whitespace collapsed."""
    if unescape:
        text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    return _WS.sub(" ", "".join(fold_char(c) for c in text)).strip()


def tokens(name: str) -> tuple:
    return tuple(t for t in fold(name).replace(",", " ").split(" ") if t)


def name_key(name: str) -> tuple:
    """Order-insensitive identity of a name."""
    return tuple(sorted(tokens(name)))


def same_name(a: str, b: str) -> bool:
    ka = name_key(a)
    return bool(ka) and ka == name_key(b)


def _orders(toks):
    if len(toks) <= _MAX_PERMUTED_TOKENS:
        return list(itertools.permutations(toks))
    return [toks, tuple(reversed(toks))]


def name_pattern(name: str) -> re.Pattern:
    """Regex finding `name` in *folded* text, in any token order, as whole words."""
    toks = tokens(name)
    if not toks:
        raise ValueError("empty name")
    alts = sorted({_SEP.join(re.escape(t) for t in order) for order in _orders(toks)},
                  key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)")


def find_in_text(text: str, name: str):
    """Spans of `name` in already-folded text."""
    return [m.span() for m in name_pattern(name).finditer(text)]


def fold_with_map(chars):
    """Fold a sequence of characters, returning (folded_text, index_map) where
    index_map[i] is the index in `chars` the i-th folded character came from.
    Used for PDFs, where a match in the folded line has to be mapped back to the
    glyph boxes it covers."""
    out, idx = [], []
    for i, c in enumerate(chars):
        f = fold_char(c)
        if f.isspace():
            f = " "
        for ch in f:
            out.append(ch)
            idx.append(i)
    return "".join(out), idx
