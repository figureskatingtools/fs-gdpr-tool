# CLAUDE.md — GDPR Removal Tool architecture

A sibling of `fs-protocolgenerator`, `fs-judgepapers` and `fs-scoremodifier` on
`figureskatingtools.com`. It removes skaters' names and clubs from a published FS
Manager results site (HTML + judges-scores PDFs) and from a protocol PDF made by the
Protocol Generator, and returns a ZIP of only the changed files.

**This repo is backend-only.** The UI lives in the `figureskatingtools-site` repo under
the nav's **Tools** dropdown (`SMALL_TOOLS`) at `/tools/gdpr/`; the site router handles
the Entra login and proxies `/tools/gdpr/api/*` here with `x-proxy-secret` +
`x-forwarded-user-email` (see **PROXY-CONTRACT.md**). No code is shared with the other
tool repos — patterns are copied, not imported.

**Never commit real names or real competition data** (fixtures, tests, commit messages,
logs, docs). The example sites contain minors' personal data; use them only to learn the
structure, and only print masked output when inspecting them.

## Stack

- Python 3.13 Azure Functions (`infra/functions/`), Flex Consumption FC1,
  `AuthLevel.ANONYMOUS` behind the router's proxy-secret gate.
- `pymupdf` (AGPL, accepted) for true redaction and full-rewrite saves; `pypdf` as a
  second extractor in verification. Test-only: `pytest`, `pikepdf`, `reportlab`, `pillow`.
- IaC: subscription-scoped Bicep (`infra/main.bicep`, `modules/{storage,function,roleassignment}.bicep`).
  Host storage only (`app-package` + runtime state), **no data container, no table, no
  Application Insights**.

## Stateless request model (`function_app.py`)

Each route runs inside `workspace.workspace()` — a fresh temp dir removed in `finally`.
The steps are linked only by what the UI sends back:

1. `scan {eventUrl}` → `fetcher.fetch_event` + `engine.build_catalog` → categories,
   skaters (ids `c<cat>-s<n>`, deterministic from the files), `snapshot` (sha256 over
   every fetched file).
2. `protocol_plan {…, protocolUrl}` → refetch, check snapshot (409 `event_changed`),
   `protocol.plan` → pages + `planHash` (protocol bytes + plan + selection). Writes nothing.
3. `generate {…, protocol?: {url, planHash, confirmed: true}}` → refuses an unconfirmed
   protocol before fetching anything (400), refetches, checks snapshot, `protocol.apply`
   recomputes the plan and refuses a hash mismatch (409 `protocol_plan_changed`), then
   `engine.generate` → changed files + name-free report → `package.build_zip`.

Logging: exception *types* only (`_guard`). Never log URLs, names or page text.

## Modules

- `fetcher.py` — event-dir crawl. `classify_link` normalises FS Manager's mix of unquoted
  relative and absolute `http://` links: same host + path under the event dir = in-dir
  (`rel`); same host outside = shared asset, fetched only when referenced as a resource
  (`src=`, `<link>`), never crawled or modified; anything else ignored (`/bios/` included).
  `ALLOWED_RESULT_HOSTS` allow-list enforced on every request and redirect. `http_get` is
  the single network entry point (tests monkeypatch it).
- `fsm_html.py` — `decode` picks a codec that round-trips the bytes exactly (FS Manager
  pages are ASCII + `&#xF6;` entities + CRLF; older ones cp1252/UTF-8). A small tag
  scanner (`parse_structure`) builds a table/tr/td tree with offsets; `skater_rows` reads
  rows of tables whose header has a **Name** column (that excludes the officials table and
  the club cell's own flag table). `redact_page` splices edits into the original text:
  name cell inner → replacement (entities on ASCII pages), club cell → flag `<img>` and
  text runs dropped, markup kept. `parse_index` maps categories → EN/RS/SEG/OF/PDF; segment
  rows count only in the category table's own `<table>` (the schedule table below links
  the SEG pages again).
- `names.py` — `fold` (entities, spacing diacritics → combining, NFD, drop marks,
  casefold, whitespace), order-insensitive `name_key`, `name_pattern` (all token orders,
  whole words, `[\s,]+` separators), `fold_with_map` for PDF glyph mapping.
- `pdf_redact.py` — glyph-level search on `rawdict` lines (a protocol result line is "1
  Firstname SURNAME" in one span), redaction annots on vertically inset glyph boxes, club
  on the same baseline right of the name, `apply_redactions(images=NONE, graphics=NONE)`,
  replacement drawn at the name's baseline (Helvetica/Helvetica-Bold, shrunk only to fit
  before the next column), empty `/Annots` removed, `save(garbage=4, deflate, clean,
  incremental=False)`.
- `protocol.py` — page classification (judges details / panel / podium via the "PODIUM"
  eyebrow on generated, non-FSM pages / results vs segment results / withdrawn when every
  hit is below a "Withdrawn" heading), `plan`, `plan_hash`, `apply` (redact first, then
  `delete_pages`, records image digests used only by removed pages).
- `verify.py` — residual search over the final published state (HTML per line in text and
  raw source; PDFs via PyMuPDF + pypdf per page plus every non-image/non-font object and
  stream, hex/octal strings decoded); `html_invariance` (row count, other rows identical,
  redacted rows identical except name/club); `pdf_invariance` (every word outside the
  redacted boxes survives on every kept page).
- `engine.py` — catalog, selection resolution, `unique_names` (same name in two
  categories = one name), `generate`.
- `package.py` — manifest with "selected skater N" labels; `assert_name_free` re-checks
  the serialised manifest before zipping.

## Tests

```bash
cd infra/functions
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

`tests/fixtures.py` builds the whole synthetic event and protocol in code (invented
names incl. ä/ö/é, a WD skater, a skater in two categories; `fsm`, `cp1252` and `nfd`
page styles). `tests/test_app.py` drives the routes end to end over a fake web;
`tests/test_units.py` covers the modules, including the fetcher against a real local
HTTP server.

## Deploy

Same shape as the protocol generator, with no long-lived `test` branch: branch
from `main` → dispatch `deploy.yml` on that branch with `environment=test` → PR to
`main` → merge → push to `main` deploys prod → the branch is auto-deleted on merge.
`test` is a GitHub environment only and holds whichever branch was deployed last.
The `prod` GitHub environment only accepts deployments from `main` (deployment
branch policy — enforced, verified with a rejected dispatch from another branch).
`.github/CODEOWNERS` (`* @mmaraa`) requests the owner's review on every PR, but the
repo is private on the free plan, so GitHub offers no `main` ruleset / branch
protection: "merge only reviewed, green PRs" is a convention here, unlike in the
protocol generator.

Push to `main` → prod; `test` via manual
`workflow_dispatch`. Jobs: set-environment → deploy-infra (Bicep) → deploy-backend
(pytest, then zip + `az functionapp deployment source config-zip`). `test.yml` runs the
suite on every PR. All jobs pin `runs-on: ubuntu-26.04` (see the comment in
`deploy.yml`; never `ubuntu-latest`). Dependabot opens its PRs against `main`.

Required GitHub environment config (`test`, `prod`): secrets `AZURE_CLIENT_ID`,
`PROXY_SHARED_SECRET`; vars `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `LOCATION`,
`RESOURCE_GROUP_NAME` (`rg-fs-gdpr-<env>`), optional `ALLOWED_RESULT_HOSTS`,
`ALLOWED_USER_EMAILS`.

## Cross-repo handoff (figureskatingtools-site)

Set the deploy summary's Function App URL in the site repo's matching environment as
`FUNCTION_APP_URL_GDPRTOOL` and copy `PROXY_SHARED_SECRET` there as
`PROXY_SHARED_SECRET_GDPRTOOL`. This repo is private, so it is **not** in the site's
`changelog-sources.json`.
