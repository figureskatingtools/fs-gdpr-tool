"""
Download an FS Manager event directory.

Starting from the event's index page, every page, PDF and image linked from
the index or its pages *inside the event directory* is fetched, recursively for
HTML. Links are normalised first: FS Manager mixes unquoted relative links
(`href=CAT001RS.htm`) with absolute `http://host/results/<event>/…` ones, and an
absolute link into the same directory is treated exactly like a relative one
whatever its scheme. Assets outside the directory are fetched only when a page
references them directly as a resource (`src=`, `<link href=…>` — style sheets,
scripts, flags) and only from the same host; they are never crawled, never
modified and never part of the output. Anchors out of the directory — skater
bios, club sites — are not followed at all.

Only hosts on the allow-list (`ALLOWED_RESULT_HOSTS`) are ever contacted,
including across redirects, so the backend cannot be pointed at arbitrary
URLs. Everything is written into the caller's temporary work directory.
"""
import hashlib
import os
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOWED_HOSTS = "www.figureskatingresults.fi,figureskatingresults.fi,fs.lintuala.fi"

MAX_FILES = 800
MAX_SHARED_FILES = 100
MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_PROTOCOL_BYTES = 150 * 1024 * 1024
MAX_TOTAL_BYTES = 400 * 1024 * 1024
TIMEOUT_S = 30
WORKERS = 8
USER_AGENT = "fs-gdpr-tool (figureskatingtools.com)"

_IN_DIR_EXTS = (".htm", ".html", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".css", ".js", ".ico", ".svg", ".webp")
_HTML_EXTS = (".htm", ".html")
_ATTR = re.compile(r"""(?is)<([a-z]+)\b([^>]*)>""")
_LINK_ATTR = re.compile(r"""(?i)\b(href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")


class FetchError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def allowed_hosts():
    raw = os.environ.get("ALLOWED_RESULT_HOSTS") or DEFAULT_ALLOWED_HOSTS
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _netloc_allowed(parsed) -> bool:
    hosts = allowed_hosts()
    host = (parsed.hostname or "").lower()
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return host in hosts or netloc in hosts


def check_url(url: str):
    """Parse and validate a user-supplied URL; raises FetchError."""
    try:
        parsed = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        raise FetchError("invalid_url")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("invalid_url")
    if parsed.username or parsed.password:
        raise FetchError("invalid_url")
    if not _netloc_allowed(parsed):
        raise FetchError("host_not_allowed")
    return parsed


@dataclass
class Event:
    base_url: str                 # scheme://host/dir/ (trailing slash)
    index_url: str
    index_name: str               # relative path of the index inside the dir
    root: Path                    # work dir holding the event files
    files: dict = field(default_factory=dict)    # rel -> Path
    shared: dict = field(default_factory=dict)   # absolute URL path -> Path
    missing: list = field(default_factory=list)  # rel paths linked but not found

    def read(self, rel) -> bytes:
        return self.files[rel].read_bytes()

    def snapshot(self) -> str:
        h = hashlib.sha256()
        for rel in sorted(self.files):
            h.update(rel.encode("utf-8") + b"\0")
            h.update(hashlib.sha256(self.read(rel)).digest())
        return h.hexdigest()


def event_location(url: str):
    """(base_url, index_url, index_name) for an event page URL."""
    p = check_url(url)
    path = p.path or "/"
    if path.lower().endswith(_HTML_EXTS):
        dir_path, index_name = path.rsplit("/", 1)[0] + "/", path.rsplit("/", 1)[1]
    else:
        dir_path = path if path.endswith("/") else path + "/"
        index_name = "index.htm"
    base = urllib.parse.urlunsplit((p.scheme, p.netloc, dir_path, "", ""))
    return base, urllib.parse.urljoin(base, index_name), index_name


# ── HTTP ──────────────────────────────────────────────────────────────────────

class _AllowListRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _netloc_allowed(urllib.parse.urlsplit(newurl)):
            raise FetchError("host_not_allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_AllowListRedirect)


def http_get(url: str, limit: int = MAX_FILE_BYTES) -> bytes:
    """GET `url`; returns the body, None for 404/410. Raises FetchError."""
    if not _netloc_allowed(urllib.parse.urlsplit(url)):
        raise FetchError("host_not_allowed")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as resp:
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None
        raise FetchError("upstream_error")
    except FetchError:
        raise
    except Exception:
        raise FetchError("upstream_unreachable")
    if len(data) > limit:
        raise FetchError("file_too_large")
    return data


# ── link handling ─────────────────────────────────────────────────────────────

def extract_links(html_text: str):
    """[(tag, attr, value)] for every href/src in the page."""
    out = []
    for m in _ATTR.finditer(html_text):
        tag = m.group(1).lower()
        for a in _LINK_ATTR.finditer(m.group(2)):
            value = next(g for g in a.groups()[1:] if g is not None)
            out.append((tag, a.group(1).lower(), value.strip()))
    return out


def classify_link(base_url: str, page_url: str, value: str):
    """('dir', rel) for a file inside the event directory, ('shared', url) for
    a same-host file outside it, or (None, None)."""
    if not value or value.startswith(("#", "mailto:", "javascript:", "data:", "tel:")):
        return None, None
    absolute = urllib.parse.urljoin(page_url, value)
    p = urllib.parse.urlsplit(absolute)
    b = urllib.parse.urlsplit(base_url)
    if p.scheme not in ("http", "https"):
        return None, None
    if (p.hostname or "").lower() != (b.hostname or "").lower() or (p.port or None) != (b.port or None):
        return None, None
    path = posixpath.normpath(urllib.parse.unquote(p.path)) + ("/" if p.path.endswith("/") else "")
    if not path.startswith("/"):
        return None, None
    if path.lower().startswith(b.path.lower()):
        rel = path[len(b.path):]
        if not rel or rel.endswith("/"):
            return None, None
        if ".." in rel.split("/"):
            return None, None
        return "dir", rel
    shared = urllib.parse.urlunsplit((b.scheme, b.netloc, path, "", ""))
    return "shared", shared


def _is_resource(tag, attr):
    return attr == "src" or tag == "link"


def _safe_target(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep):
        raise FetchError("invalid_path")
    return target


# ── crawl ─────────────────────────────────────────────────────────────────────

def fetch_event(url: str, workdir: Path) -> Event:
    base, index_url, index_name = event_location(url)
    root = Path(workdir) / "event"
    shared_root = Path(workdir) / "shared"
    root.mkdir(parents=True, exist_ok=True)
    shared_root.mkdir(parents=True, exist_ok=True)
    event = Event(base, index_url, index_name, root)

    index = http_get(index_url)
    if index is None:
        raise FetchError("event_not_found")
    total = len(index)
    _write(event, index_name, index)

    queue = [(index_name, index)]
    queued = {index_name}
    shared_wanted = set()
    while queue:
        wanted = []
        for rel, data in queue:
            page_url = urllib.parse.urljoin(base, rel)
            text = data.decode("latin-1")
            for tag, attr, value in extract_links(text):
                kind, target = classify_link(base, page_url, value)
                if kind == "dir":
                    if target not in queued and target.lower().endswith(_IN_DIR_EXTS):
                        queued.add(target)
                        wanted.append(target)
                elif kind == "shared" and _is_resource(tag, attr):
                    shared_wanted.add(target)
        if len(queued) > MAX_FILES:
            raise FetchError("too_many_files")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(lambda r: (r, http_get(urllib.parse.urljoin(base, urllib.parse.quote(r)))), wanted))
        queue = []
        for rel, data in results:
            if data is None:
                event.missing.append(rel)
                continue
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise FetchError("event_too_large")
            _write(event, rel, data)
            if rel.lower().endswith(_HTML_EXTS):
                queue.append((rel, data))

    shared = sorted(shared_wanted)[:MAX_SHARED_FILES]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda u: (u, _get_optional(u)), shared))
    for u, data in results:
        if data is None:
            continue
        path = urllib.parse.urlsplit(u).path.lstrip("/")
        target = _safe_target(shared_root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        event.shared[u] = target
    event.missing.sort()
    return event


def _get_optional(url):
    try:
        return http_get(url)
    except FetchError:
        return None


def _write(event: Event, rel: str, data: bytes):
    target = _safe_target(event.root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    event.files[rel] = target


def fetch_protocol(url: str, workdir: Path):
    """Download a protocol PDF. Returns (bytes, path written)."""
    p = check_url(url)
    if not p.path.lower().endswith(".pdf"):
        raise FetchError("not_a_pdf")
    data = http_get(urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, "")), limit=MAX_PROTOCOL_BYTES)
    if data is None:
        raise FetchError("protocol_not_found")
    if not data.startswith(b"%PDF"):
        raise FetchError("not_a_pdf")
    target = Path(workdir) / "protocol" / "original.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return data, target


def protocol_output_path(event_base: str, protocol_url: str) -> str:
    """Where the redacted protocol goes in the ZIP: its path relative to the
    event directory when it lives there, else just its (unchanged) file name."""
    p = urllib.parse.urlsplit(protocol_url)
    b = urllib.parse.urlsplit(event_base) if event_base else None
    path = urllib.parse.unquote(p.path)
    if b and (p.hostname or "").lower() == (b.hostname or "").lower() and path.startswith(b.path):
        rel = path[len(b.path):]
        if rel and ".." not in rel.split("/"):
            return rel
    return posixpath.basename(path)
