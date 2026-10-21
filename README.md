# Overwatch Backend 

FastAPI backend for Overwatch: GitHub/Jira connectors, AST-based architecture graph (tree-sitter → Neo4j), a work-item board mirrored two-way with Jira, and Gemini-grounded diagram Q&A.

**Stack:** FastAPI · MongoDB (motor) · Neo4j · tree-sitter · Gemini · GitHub App · Jira OAuth 2.0 (3LO)

## Local setup

Requires [uv](https://docs.astral.sh/uv/), Python 3.14 and Docker. Use `localhost` everywhere (never
`127.0.0.1`), otherwise the auth cookies aren't sent.

```sh
docker compose up -d mongo neo4j   # Neo4j login: neo4j / localdevpassword
uv sync
cp .env.example .env               # fill in the secrets (keep MONGO_URI on localhost)
uv run uvicorn app.main:app --reload --port 8000
```

### GitHub App

Sign-in and repo access both go through one GitHub App. Create a personal one for development
(GitHub → Settings → Developer settings → GitHub Apps → New GitHub App):

| Setting | Value |
| --- | --- |
| Homepage URL | `http://localhost:3000` |
| Callback URL | `http://localhost:8000/auth/github/callback` |
| Expire user authorization tokens | on |
| Request user authorization (OAuth) during installation | off |
| Setup URL | `http://localhost:3000/github/setup` (Redirect on update: on); it sends users on to onboarding or Settings → Repositories |
| Webhook URL | your smee.io channel (below) |
| Webhook secret | `openssl rand -hex 32` → `GITHUB_WEBHOOK_SECRET` |
| Repository permissions | Contents: read-only, Metadata: read-only |
| Subscribe to events | Push |

Then generate a client secret and a private key. Save the key as `github-app.pem` in this folder
(gitignored) and set `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` and
`GITHUB_APP_SLUG` (the app's URL name). Installation, repository-selection and authorization
events are delivered to every App automatically.

GitHub can't reach `localhost`, so forward webhooks through [smee.io](https://smee.io/new):

```sh
npx smee-client --url https://smee.io/<channel> --target http://localhost:8000/webhooks/github
```

If signatures fail through smee, use `cloudflared tunnel --url http://localhost:8000` instead and
put its URL (plus `/webhooks/github`) in the App's webhook settings.

### Jira

Create an OAuth 2.0 (3LO) app in the Atlassian developer console with callback
`http://localhost:8000/jira/callback` and scopes `read:jira-work`, `write:jira-work`, `read:jira-user`, `offline_access`,
plus the Jira Software granular scopes that boards, their columns and sprints need (Jira Software accepts no classic
scopes): `read:board-scope:jira-software`, `write:board-scope:jira-software`, `read:board-scope.admin:jira-software`,
`read:sprint:jira-software`, `write:sprint:jira-software`, `read:issue:jira-software`.
Then set `JIRA_CLIENT_ID` and `JIRA_CLIENT_SECRET`.

### Email

Sign-up codes, password resets and workspace invites are emailed. Locally `EMAIL_BACKEND=console` logs them
(with the code, in dev only). In production use `EMAIL_BACKEND=smtp` with a Gmail account: turn on 2-step
verification, create an App Password, and set `SMTP_USERNAME` (the address), `SMTP_PASSWORD` (the App Password)
and `EMAIL_FROM` (the same address or a verified "send as" alias). Gmail allows about 500 recipients a day;
`INVITE_EMAIL_DAILY_LIMIT_*` keep invites well under it.

## API responses

Every endpoint returns the same envelope. Routes just return their data and declare its type
(`-> Me`, `-> list[ConnectedRepo]`); routers made with `api_router()` wrap it on the way out
(`app/core/responses.py`, like a NestJS interceptor), and the exception handlers give errors the same shape.
Return `WithMeta(data, meta)` to add pagination or counts.

```json
{ "status": "success", "code": 200, "data": { }, "meta": { "page": 1, "page_size": 25, "total": 80 } }
{ "status": "error", "code": 401, "data": null, "meta": { "error_code": "invalid_token", "message": "..." } }
```

`code` mirrors the HTTP status; `meta` carries pagination, or `error_code` and `message` on errors.
Only the OAuth callbacks (`/auth/github/callback`, `/jira/callback`) respond with redirects instead.

## Tests

```sh
uv run pytest                    # full suite
uv run pytest -m regression      # regression snapshot suite only
```

Tests that need a real Mongo or Neo4j (races, Cypher) skip unless `docker compose up -d mongo neo4j`
is running.

## Git hooks

Enable the tracked pre-push hook once after cloning:

```sh
git config core.hooksPath .githooks
```

Every push runs the full backend test suite and is blocked if any test fails.
Use `git push --no-verify` only for exceptional, intentional bypasses.

## Deployment

The production API runs in Docker on an AWS Lightsail server behind the
Cloudflare proxy. MongoDB Atlas and Neo4j Aura remain external. Pushes to `main`
are tested, published to GHCR, deployed to the server, health checked, and
rolled back on failure by `.github/workflows/ci.yml`.

### One-time server setup

Install Docker, create `/opt/overwatch`, and save the production environment at
`/opt/overwatch/.env` with mode `600`. In addition to the application settings
shown in `.env.example`, it must contain the image to deploy:

```env
BACKEND_IMAGE=ghcr.io/rizwan521/overwatch-be
```

Use the public API URL for `APP_BASE_URL`. Set `FRONTEND_ORIGIN` to the exact
deployed frontend origin. Keep Atlas, Aura, GitHub App, Jira, Gemini, JWT, and
Fernet credentials only in this server-side file.

Cloudflare terminates HTTPS for visitors (SSL mode Full (strict)). The API serves
HTTPS to Cloudflare with a Cloudflare Origin CA certificate saved as
`/opt/overwatch/certs/origin.pem` and `origin.key` (mode `600`). The DNS record
for the API must be proxied, and the server firewall must allow port 443 only
from [Cloudflare's IP ranges](https://www.cloudflare.com/ips-v4) and port 22 for
SSH. `compose.prod.yml` trusts `X-Forwarded-For` from the same ranges, so update
both if Cloudflare changes them.

### GitHub production environment

Create a GitHub environment named `production` with these secrets:

- `EC2_SSH_PRIVATE_KEY` — a dedicated deployment private key
- `EC2_SSH_KNOWN_HOSTS` — output from `ssh-keyscan -H <server-ip>`

Add these environment variables. `EC2_HOST` is the server's static IP, since the
proxied API domain resolves to Cloudflare rather than the server:

```text
EC2_HOST=<server-static-ip>
EC2_USER=admin
EC2_APP_DIR=/opt/overwatch
HEALTH_URL=https://api.usebearings.app/health
```

The workflow copies `compose.prod.yml` and `scripts/deploy.sh` to the server.
The application `.env` and the origin certificate are never copied from GitHub.

### Rollout steps

1. Provision Atlas (get `MONGO_URI`) and AuraDB (get `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`).
2. Create the production GitHub App (settings as above, with the deployed URLs:
   callback `https://<your-api-domain>/auth/github/callback`, webhook
   `https://<your-api-domain>/webhooks/github`, setup URL `https://<your-frontend>/github/setup`)
   and an Atlassian app with callback `https://<your-api-domain>/jira/callback`.
3. Get a Gemini API key.
4. Put these in `/opt/overwatch/.env` on the server (see One-time server setup; never commit them):
   - `JWT_SECRET_KEY`, `FERNET_KEY` (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   - `MONGO_URI`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
   - `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_SLUG`,
     `GITHUB_WEBHOOK_SECRET`, and `GITHUB_APP_PRIVATE_KEY_PATH` pointing at the mounted `.pem`
   - `JIRA_CLIENT_ID`, `JIRA_CLIENT_SECRET`
   - `GEMINI_API_KEY`, and optionally the AI chat limits `CHAT_USER_HOURLY_LIMIT` (30),
     `CHAT_WORKSPACE_DAILY_LIMIT` (200) and `CHAT_DAILY_BUDGET` (500); keep the budget below
     the Gemini project's daily quota
   - `APP_BASE_URL` (your deployed API URL — used to build OAuth callbacks)
   - `FRONTEND_ORIGIN` (your deployed frontend URL — CORS is locked to exactly this origin)
5. Moving from the old GitHub OAuth app: delete the old per-repo webhooks, then run
   `uv run python scripts/migrate_github_app.py` (dry run) and again with `--apply`.
6. Moving to workspaces: stop the API, run `uv run python scripts/migrate_workspaces.py`
   (dry run) and again with `--apply`, then deploy the backend, then the frontend. The new
   unique indexes can't be built while data is still keyed by user, and that failure is
   only logged at boot.
7. Moving to connectors and the board: add the `write:jira-work` scope to the Atlassian app,
   stop the API, run `uv run python scripts/migrate_connectors.py` (dry run) and again with
   `--apply`, then deploy the backend, then the frontend. Existing Jira connections keep
   syncing but must be reconnected once before board changes can be written back.
   Once the board looks right, `--apply --drop-legacy` removes `jira_projects` and `tickets`.
8. Moving to boards: add the Jira Software scopes above to the Atlassian app, deploy, then run
   `uv run python scripts/migrate_boards.py` (dry run) and again with `--apply`, and have an admin reconnect
   Jira so its board columns and sprints can be read. Until they reconnect, columns are offered from the
   project's statuses instead.
9. Email and password sign-in: set the email variables above, deploy, then run
   `uv run python scripts/migrate_native_auth.py` (dry run) and again with `--apply`. It replaces the
   `github_id` index (the API also does this on boot), gives GitHub users their login as username, and marks
   existing invites' kind. Users whose login couldn't be used choose a username the next time they sign in.
10. Deploy, then hit `GET /health` to confirm the app booted and connected to Mongo/Neo4j.

**Note on ticket freshness:** Jira issues sync on a polling loop
(`ticket_sync_interval_seconds`, default 5 min) rather than via Jira webhooks, which need a
public URL and expire every 30 days. Most runs only read issues updated since the last one;
a full run, which also drops issues deleted in Jira, happens hourly or from "Sync now".
