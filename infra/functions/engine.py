"""
Catalog (categories + skaters) and generation of the redacted files.

The catalog is derived only from the fetched files, deterministically, so the
skater ids it hands to the UI (`c<category>-s<n>`) resolve to the same skaters
when the event is fetched again for generation — which is how the backend stays
stateless between requests. The snapshot hash makes sure "the same files" holds.
"""
import re
from dataclasses import dataclass, field

import fetcher
import fsm_html
import names
import pdf_redact
import protocol as protocol_mod
import verify


@dataclass
class Skater:
    id: str
    category: int
    name: str
    club: str
    status: str = ""                       # "WD" etc. when not placed
    sources: list = field(default_factory=list)


@dataclass
class Category:
    number: int
    name: str
    html_pages: list                       # rel paths: entries, result, segments
    pdfs: list                             # judges scores PDFs
    segments: list
    skaters: list = field(default_factory=list)


def _rel(event, link):
    """Index links are relative to the index page."""
    link = (link or "").strip()
    if not link:
        return None
    kind, rel = fetcher.classify_link(event.base_url, event.index_url, link)
    return rel if kind == "dir" and rel in event.files else None


def build_catalog(event):
    index_text, _ = fsm_html.decode(event.read(event.index_name))
    cats = []
    for ic in fsm_html.parse_index(index_text):
        pages, pdfs, segs = [], [], []
        for link in (ic.entries, ic.result):
            r = _rel(event, link)
            if r:
                pages.append(r)
        for s in ic.segments:
            page, pdf = _rel(event, s["page"]), _rel(event, s["pdf"])
            if page:
                pages.append(page)
            if pdf:
                pdfs.append(pdf)
            segs.append({"name": s["name"], "page": page, "panel": _rel(event, s["panel"]), "pdf": pdf})
        cat = Category(ic.number, ic.name, pages, pdfs, segs)
        _collect_skaters(event, cat, [_rel(event, ic.entries), _rel(event, ic.result)])
        cats.append(cat)
    return cats


def _collect_skaters(event, cat, sources):
    """Skaters from the Entries and Result pages, merged by name."""
    by_key = {}
    for rel, label in zip(sources, ("entries", "result")):
        if not rel:
            continue
        text, _ = fsm_html.decode(event.read(rel))
        for row in fsm_html.skater_rows(text):
            key = names.name_key(row.name)
            if not key or row.name == fsm_html.REPLACEMENT:
                continue
            sk = by_key.get(key)
            if sk is None:
                sk = Skater(f"c{cat.number}-s{len(by_key) + 1}", cat.number, row.name, row.club)
                by_key[key] = sk
                cat.skaters.append(sk)
            if label not in sk.sources:
                sk.sources.append(label)
            if not sk.club and row.club:
                sk.club = row.club
            placement = row.texts[0] if row.texts else ""
            if label == "result" and placement and not re.fullmatch(r"\d+\.?", placement):
                sk.status = placement


def catalog_json(cats):
    key_index = {}
    for c in cats:
        for s in c.skaters:
            key_index.setdefault(names.name_key(s.name), []).append(s.id)
    return [{
        "id": f"c{c.number}",
        "number": c.number,
        "name": c.name,
        "segments": [s["name"] for s in c.segments],
        "files": len(c.html_pages) + len(c.pdfs),
        "skaters": [{
            "id": s.id, "name": s.name, "club": s.club, "status": s.status,
            "sources": s.sources,
            "alsoIn": [i for i in key_index[names.name_key(s.name)] if i != s.id],
        } for s in c.skaters],
    } for c in cats]


class SelectionError(ValueError):
    pass


def resolve_selection(cats, ids):
    by_id = {s.id: (s, c) for c in cats for s in c.skaters}
    out, seen = [], set()
    for i in ids or []:
        if i in seen:
            continue
        seen.add(i)
        if i not in by_id:
            raise SelectionError(i)
        out.append(by_id[i])
    if not out:
        raise SelectionError("empty")
    return out


def labels(selection):
    return [{"label": f"selected skater {n}", "category": c.name}
            for n, (s, c) in enumerate(selection, 1)]


def unique_names(selection):
    """Distinct names (by key) with their first club, and a map from selection
    index to name index — a skater selected in two categories is one name."""
    keys, out, clubs, idx = {}, [], [], []
    for s, _ in selection:
        k = names.name_key(s.name)
        if k not in keys:
            keys[k] = len(out)
            out.append(s.name)
            clubs.append(s.club)
        idx.append(keys[k])
    return out, clubs, idx


def generate(event, cats, selection, protocol_result=None):
    """Redact every affected file. Returns (changed {rel: bytes}, report)."""
    changed = {}
    html_checks, pdf_checks = [], []
    per_file = {}
    found = [0] * len(selection)

    by_cat = {}
    for n, (s, c) in enumerate(selection):
        by_cat.setdefault(c.number, (c, []))[1].append(n)

    for _, (cat, members) in sorted(by_cat.items()):
        cat_names = [selection[n][0].name for n in members]
        cat_clubs = [selection[n][0].club for n in members]
        for rel in cat.html_pages:
            old = event.read(rel)
            new, matched = fsm_html.redact_page(old, cat_names)
            if not matched:
                continue
            changed[rel] = new
            per_file[rel] = {"path": rel, "kind": _kind(rel),
                             "skaters": sorted({members[m[0]] for m in matched}),
                             "rowsRedacted": len(matched)}
            for m in matched:
                found[members[m[0]]] += 1
            html_checks.append(verify.html_invariance(rel, old, new))
        for rel in cat.pdfs:
            old = event.read(rel)
            new, hits, written = pdf_redact.redact_pdf(old, cat_names, cat_clubs)
            if new is None:
                continue
            changed[rel] = new
            per_file[rel] = {"path": rel, "kind": "judges_scores_pdf",
                             "skaters": sorted({members[h.skater] for h in hits}),
                             "occurrencesRedacted": len(hits)}
            for h in hits:
                found[members[h.skater]] += 1
            pdf_checks.append(verify.pdf_invariance(rel, old, new, hits, written))

    protocol_info = None
    if protocol_result is not None:
        rel, old, new, info, name_idx = protocol_result
        changed[rel] = new
        per_file[rel] = {"path": rel, "kind": "protocol_pdf",
                         "skaters": sorted({n for n, ni in enumerate(name_idx)
                                            for e in info["plan"]["pages"] if ni in e["skaters"]}),
                         "pagesRemoved": info["removedPages"],
                         "pagesRedacted": info["redactedPages"]}
        page_map = {old_no: new_no for new_no, old_no in enumerate(info["keptPagesOriginal"])}
        check = verify.pdf_invariance(rel, old, new, info["hits"], info["written"],
                                      page_map=page_map, removed=set(p - 1 for p in info["removedPages"]))
        leftover = set(info["removedImageDigests"]) & protocol_mod.all_image_digests(new)
        if leftover:
            check["ok"] = False
            check["problems"].append(f"{len(leftover)} image(s) of removed pages still in the file")
        check["removedPagesImagesGone"] = not leftover
        pdf_checks.append(check)
        protocol_info = {"path": rel, "removedPages": info["removedPages"],
                         "redactedPages": info["redactedPages"],
                         "pages": [{k: e[k] for k in ("page", "type", "action")} |
                                   {"skaters": sorted({n for n, ni in enumerate(name_idx) if ni in e["skaters"]})}
                                   for e in info["plan"]["pages"]]}

    # Residual search over everything as it will be published.
    final = {rel: event.read(rel) for rel in event.files}
    final.update(changed)
    for url, path in event.shared.items():
        final["(shared) " + url.split("://", 1)[-1].split("/", 1)[-1]] = path.read_bytes()
    uniq, _, name_idx = unique_names(selection)
    residual_by_name = verify.residual_search(final, uniq)
    residual = []
    for h in residual_by_name:
        for n, ni in enumerate(name_idx):
            if ni == h["skater"]:
                residual.append({"file": h["file"], "location": h["location"], "skater": n})
                break

    not_found = [n for n, k in enumerate(found) if k == 0]
    ok = (not residual and all(c["ok"] for c in html_checks + pdf_checks) and not not_found)
    report = {
        "ok": ok,
        "files": [per_file[k] for k in sorted(per_file)],
        "protocol": protocol_info,
        "residual": residual,
        "htmlChecks": html_checks,
        "pdfChecks": pdf_checks,
        "notFound": not_found,
        "filesSearched": len(final),
        "missingLinks": len(event.missing),
    }
    return changed, report


def _kind(rel):
    kind, _ = fsm_html.page_kind(rel)
    return {fsm_html.PAGE_ENTRIES: "entries_html", fsm_html.PAGE_RESULT: "result_html",
            fsm_html.PAGE_SEGMENT: "segment_html"}.get(kind, "html")
