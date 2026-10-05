# Proxy contract — how requests reach this Function App

This repo is **backend-only**. Nothing browses the Function App directly: the GDPR
Removal Tool UI is served by the `figureskatingtools-site` router
(`figureskatingtools.com/tools/gdpr/`), which handles the Entra (Easy Auth) login and
forwards every `/tools/gdpr/api/*` request here as `/api/*`, server-to-server.

```
Browser ── https://figureskatingtools.com/tools/gdpr/api/scan
             │  (Easy Auth session cookie)
             ▼
        site router (server.js)
             │  strips the /tools/gdpr prefix
             │  adds  x-proxy-secret: <PROXY_SHARED_SECRET_GDPRTOOL>
             │  adds  x-forwarded-user-email: <signed-in user's email>
             ▼
        https://func-fs-gdpr-<hash>.azurewebsites.net/api/scan
```

## Headers

| Header | Set by | Meaning |
| --- | --- | --- |
| `x-proxy-secret` | router | Must equal the Function App's `PROXY_SHARED_SECRET` app setting (constant-time compare). Wrong/missing → 401. |
| `x-forwarded-user-email` | router | The signed-in user's email. Checked against `ALLOWED_USER_EMAILS` when that is set (else 403). |

Both are implemented in `infra/functions/auth.py` (`proxy_secret_ok`, `user_email`,
`user_allowed`); every POST route goes through `function_app._guard`.

**If `PROXY_SHARED_SECRET` is empty the gate is disabled** — local-dev mode only;
deployed environments always set it from the GitHub environment secret.

Identity precedence after the secret check: `x-ms-client-principal-name` (only if Easy
Auth were ever enabled on this app — it is not), then `x-forwarded-user-email`. Anything
else → 401.

## Routes

All bodies are JSON; URLs and names are never put in query strings.

| Route | Body | Returns |
| --- | --- | --- |
| `GET /api/health` | – | `{"status": "ok"}` (no auth) |
| `POST /api/scan` | `{eventUrl}` | `eventUrl`, `baseUrl`, `snapshot`, `categories[{id, name, segments, skaters[{id, name, club, status, sources, alsoIn}]}]`, `suggestedProtocolUrl`, `stats` |
| `POST /api/protocol_plan` | `{eventUrl, snapshot, selection, protocolUrl}` | `outputPath`, `pageCount`, `pages[{page, type, typeLabel, action, skaters, heading}]`, `planHash` — writes nothing |
| `POST /api/generate` | `{eventUrl, snapshot, selection, protocol?: {url, planHash, confirmed: true}}` | `ok`, `manifest`, `zipName`, `zipBase64` |

`selection` is a list of skater ids from `scan`; `skaters` in a plan are indexes into it.

Error bodies are `{"error": "<code>"}`: 400 `invalid_url` / `host_not_allowed` /
`invalid_selection` / `protocol_not_confirmed` / `not_a_pdf` / `invalid_json`, 401
`unauthorized`, 403 `forbidden`, 404 `event_not_found` / `protocol_not_found`, 409
`event_changed` (the event's files differ from the scanned snapshot) /
`protocol_plan_changed` (the protocol or selection differs from the confirmed plan), 413
`file_too_large` / `too_many_files` / `event_too_large`, 422 `no_categories` /
`unsupported_encoding`, 502 `upstream_error` / `upstream_unreachable`, 500
`internal_error`.

## Testing it with curl

```bash
# infra/functions/local.settings.json → "PROXY_SHARED_SECRET": "devsecret"
curl -s http://localhost:7071/api/scan \
  -H 'x-proxy-secret: devsecret' \
  -H 'x-forwarded-user-email: tester@example.com' \
  -d '{"eventUrl": "https://www.figureskatingresults.fi/results/<season>/<event>/"}'
```

Omitting either header must return **401** — that is the regression check after any
change to the auth helpers or to the router.
