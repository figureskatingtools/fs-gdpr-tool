# fs-gdpr-tool — GDPR Removal Tool

Part of the [figureskatingtools.com](https://figureskatingtools.com) family of tools. It
handles **removal requests on published figure skating results**: given the event page of
a competition published by Swiss Timing **FS Manager** (and optionally its protocol PDF made
by the Protocol Generator), it replaces the selected skaters' names and clubs with
**"Nimi poistettu pyynnöstä"** in every affected file and hands back a ZIP with only the
changed files, ready to upload over the published ones.

> **Republishing undoes the removal.** FS Manager regenerates every page and PDF from its
> own database. If a competition is republished from FS Manager after a removal, the
> removal must be run again (and ideally the skater removed or anonymised in FS Manager
> itself first).

## Workflow (UI: Tools → GDPR Removal Tool, `/tools/gdpr/`)

1. **Fetch** — paste the event URL (`…/results/<season>/<event>/` or its `index.htm`).
   The backend downloads the index and every page, PDF and image it links to inside the
   event directory (plus the style sheets/scripts/flags the pages reference directly).
2. **Select** — all categories are listed with their skaters (from the Entries and
   Result pages). Pick one or more skaters, across categories if needed; a skater who
   appears in another category under the same name is flagged and selected along.
3. **Protocol (optional)** — give the protocol PDF URL (pre-filled when the index links
   one). The tool shows every page where a selected skater appears: page number, type
   (podium, results, segment results, judges details, withdrawn list) and the planned
   action — podium pages are **removed**, every other page gets the name and club
   **redacted**. Nothing is generated until you **confirm the plan**.
4. **Generate** — the tool rewrites the affected files, verifies the result and offers
   the ZIP:
   - **HTML** (Entries `CATnnnEN.htm`, Result `CATnnnRS.htm`, Starting order / Detailed
     classification `SEGnnn.htm`): the name cell becomes "Nimi poistettu pyynnöstä"
     without the bio link, the club cell loses its flag and abbreviation. Placement,
     start number and scores stay, so everyone else's results remain correct. Every other
     byte of the page — encoding, entities, CRLF line endings, layout — is unchanged.
   - **Judges Scores PDFs** (`*_JudgesDetailsperSkater.pdf`) and the **protocol**: true
     redaction — the glyphs are removed from the text layer (not covered with a box) and
     the replacement is written at the same position; saved as a full rewrite with
     garbage collection, never an incremental update. Removed podium pages take their
     photos with them. The protocol keeps its file name.
   - Panel of Judges pages and the index carry no skater data and are not changed.
5. **Verify** (automatic) — every file as it will be published is searched for the removed
   names (any name order, case, HTML entities, NFC/NFD, diacritics), and every changed
   file is compared with its original: same rows, every other skater's placement and
   scores unchanged, every other word of every PDF page kept, removed podium images gone.
   Any remaining occurrence is reported with file and location.
6. **Download** the ZIP: the changed files at their original relative paths plus
   `gdpr-manifest.json` (changed files, removed protocol pages, verification results).
   The manifest refers to skaters only as "selected skater 1", "selected skater 2" with
   their category — never by name.

## Privacy of the tool itself

- **Stateless.** Every request works in its own temporary directory, which is deleted
  when the request ends — success or error. Nothing is stored between the steps: the UI
  sends back the skater ids and a hash of the fetched files, and the backend fetches the
  event again for generation (refusing if it changed in between). The ZIP is returned in
  the response and never stored server-side; the UI discards it after the download.
- **No logs, caches or telemetry with names or competition data.** URLs and names travel
  only in request bodies; the backend logs nothing but exception types; there is no
  Application Insights, no blob container and no table.
- **Fetching is restricted** to the hosts in `ALLOWED_RESULT_HOSTS` (also across
  redirects), the event directory and the shared assets its pages reference.

## Limitations found in the FS Manager formats

- **Skater bio pages** (`/bios/isufs….htm`) are linked from every name but live outside
  the event directory; this tool does not change them. Remove them separately.
- **Images are not inspected.** A name drawn into an image (e.g. a custom event header)
  would not be found; FS Manager's own pages have none.
- **Pairs, ice dance and teams** are listed as one entry per pair/team (the name cell
  holds both names); selecting it removes the whole entry.
- **The club flag is identifying too**: its file name is the club abbreviation
  (`../flags/CLUB.GIF`), so the flag image is removed together with the club text.
- **Live events**: FS Manager pages refresh every 30 s while a competition is running.
  If a page changes between fetch and generate the tool refuses (409) — fetch again.
- **Redacted PDF text uses Helvetica** (regular/bold to match). FS Manager's embedded
  subset fonts may not contain the letters of the replacement text, so it is drawn with a
  standard font at the name's size and position.
- **Hand-edited or re-exported pages** whose markup differs from FS Manager's (no "Name"
  header column in the skater tables) are not recognised; verification would still
  report the remaining names.

## Architecture

This repo holds the **backend only**. The user interface lives in
[`figureskatingtools-site`](https://github.com/figureskatingtools/figureskatingtools-site)
under the **Tools** menu at `https://figureskatingtools.com/tools/gdpr/`; that site's router
does the Entra login and proxies `/tools/gdpr/api/*` to this Function App, adding
`x-proxy-secret` and `x-forwarded-user-email` (see [PROXY-CONTRACT.md](PROXY-CONTRACT.md)).

- **Backend** — Python 3.13 Azure Functions (`infra/functions/`): `pymupdf` (true PDF
  redaction), `pypdf` (second text extractor for verification), standard library for
  fetching and HTML.
- **Hosting** — Flex-Consumption Function App + its host storage account. IaC in `infra/`
  (Bicep). No data storage, no Application Insights, no custom domain.

## Local development

```bash
cd infra/functions
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q        # synthetic fixtures only
func start                                  # needs Azure Functions Core Tools + Azurite
curl -s http://localhost:7071/api/scan -H 'x-forwarded-user-email: tester@example.com' \
  -d '{"eventUrl": "https://www.figureskatingresults.fi/results/<season>/<event>/"}'
```

For a UI, run the router + Vite dev server from `figureskatingtools-site` against this
backend (`FUNCTION_APP_URL_GDPRTOOL=http://localhost:7071`).

## Deployment

There is no long-lived `test` branch; `test` and `prod` are GitHub environments.

1. Branch from `main` and push the branch.
2. Deploy it to test: run `.github/workflows/deploy.yml` manually (`workflow_dispatch`) on that
   branch with `environment=test`. The `test` environment holds whichever branch was deployed last.
3. Open a pull request to `main`. `.github/workflows/test.yml` runs the suite on every pull
   request, and `.github/CODEOWNERS` requests the owner's review.
4. Merge. The push to `main` deploys prod, and the branch is deleted on merge.

Every deploy runs the pytest suite again before packaging, so a red suite also blocks it.
Manual equivalents: `deploy_infra.sh`, `deploy_backend.sh`. See `CLAUDE.md` for details and the
required GitHub environment configuration.
