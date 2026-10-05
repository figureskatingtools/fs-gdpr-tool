"""
The output ZIP: only the changed files, at their original paths relative to the
event directory (ready to upload over the published ones), plus
`gdpr-manifest.json`.

The manifest never contains a removed name: skaters are "selected skater N"
with their category, and every count/location comes from the verification
report, which is name-free by construction. `assert_name_free` re-checks the
serialised manifest against the names before it is packed.
"""
import io
import json
import zipfile
from datetime import datetime, timezone

import names

MANIFEST_NAME = "gdpr-manifest.json"
TOOL = "fs-gdpr-tool"

NOTES = [
    "Upload the files in this archive over the published files at the same paths.",
    "If the competition is republished from FS Manager, the removal must be run again: "
    "FS Manager regenerates every page and PDF from its database.",
    "Skater bio pages (/bios/…) linked from the result pages live outside the event "
    "directory and are not changed by this tool; remove them separately if they exist.",
    "The fetched and generated files were held only in a temporary directory for the "
    "duration of the request and have been deleted.",
]


class NameLeak(Exception):
    pass


def _label(n):
    return f"selected skater {n + 1}"


def build_manifest(event_url, skater_labels, report):
    def lab(ids):
        return [_label(i) for i in ids]

    files = []
    for f in report["files"]:
        entry = {k: v for k, v in f.items() if k != "skaters"}
        entry["skaters"] = lab(f["skaters"])
        files.append(entry)
    protocol = None
    if report.get("protocol"):
        p = report["protocol"]
        protocol = {
            "path": p["path"],
            "removedPages": p["removedPages"],
            "redactedPages": p["redactedPages"],
            "pages": [{**e, "skaters": lab(e["skaters"])} for e in p["pages"]],
        }
    return {
        "tool": TOOL,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event_url,
        "replacementText": "Nimi poistettu pyynnöstä",
        "skaters": skater_labels,
        "changedFiles": files,
        "protocol": protocol,
        "verification": {
            "ok": report["ok"],
            "filesSearched": report["filesSearched"],
            "remainingOccurrences": [
                {"file": h["file"], "location": h["location"], "skater": _label(h["skater"])}
                for h in report["residual"]],
            "skatersNotFound": lab(report["notFound"]),
            "htmlPages": report["htmlChecks"],
            "pdfFiles": report["pdfChecks"],
        },
        "notes": NOTES,
    }


def assert_name_free(blob: str, skater_names):
    folded = names.fold(blob)
    for n in skater_names:
        if names.find_in_text(folded, n):
            raise NameLeak("manifest would contain a removed name")


def build_zip(changed, manifest, skater_names) -> bytes:
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    assert_name_free(text, skater_names)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST_NAME, text.encode("utf-8"))
        for rel in sorted(changed):
            z.writestr(rel, changed[rel])
    return buf.getvalue()
