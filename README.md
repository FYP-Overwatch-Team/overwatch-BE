# Overwatch Backend 

FastAPI backend for Overwatch: GitHub/Jira connectors, AST-based architecture graph (tree-sitter → Neo4j), Jira ticket viewer, and Gemini-grounded diagram Q&A.

**Stack:** FastAPI · MongoDB (motor) · Neo4j · tree-sitter · Gemini · GitHub OAuth · Jira OAuth 2.0 (3LO)

## Development setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync                 # create .venv and install all deps (incl. dev group)
cp .env.example .env    # then fill in secrets
uv run uvicorn app.main:app --reload
```

GitHub cannot deliver webhooks to `localhost`. For local webhook testing, expose
port 8000 with ngrok and set its HTTPS URL before starting the API:

```sh
ngrok http 8000
# .env
PUBLIC_WEBHOOK_BASE_URL=https://<your-ngrok-domain>
```

## Tests

```sh
uv run pytest                    # full suite
uv run pytest -m regression      # regression snapshot suite only
```

See `overwatch-eval1-backend-plan.md` for the full build plan and phase breakdown.

## Git hooks

Enable the tracked pre-push hook once after cloning:

```sh
git config core.hooksPath .githooks
```

Every push runs the full backend test suite and is blocked if any test fails.
Use `git push --no-verify` only for exceptional, intentional bypasses.

## EC2 deployment

The production stack runs the API and Caddy on EC2. MongoDB Atlas and Neo4j
Aura remain external. Pushes to `main` are tested, published to GHCR, deployed
to EC2, health checked, and rolled back on failure by `.github/workflows/ci.yml`.

### One-time server setup

Install Docker, create `/opt/overwatch`, and save the production environment at
`/opt/overwatch/.env` with mode `600`. In addition to the application settings
shown in `.env.example`, it must contain Compose deployment values:

```env
API_DOMAIN=overwatch-api.duckdns.org
BACKEND_IMAGE=ghcr.io/rizwan521/overwatch-be
```

Use the public API URL for both `APP_BASE_URL` and `PUBLIC_WEBHOOK_BASE_URL`.
Set `FRONTEND_ORIGIN` to the exact deployed frontend origin. Keep Atlas, Aura,
OAuth, Gemini, JWT, and Fernet credentials only in this server-side file.

The EC2 security group must allow ports 80 and 443 publicly and port 22 only
from trusted addresses. Port 8000 is internal to the Compose network.

### GitHub production environment

Create a GitHub environment named `production` with these secrets:

- `EC2_SSH_PRIVATE_KEY` — a dedicated deployment private key
- `EC2_SSH_KNOWN_HOSTS` — output from `ssh-keyscan -H <api-domain>`

Add these environment variables:

```text
EC2_HOST=overwatch-api.duckdns.org
EC2_USER=ubuntu
EC2_APP_DIR=/opt/overwatch
HEALTH_URL=https://overwatch-api.duckdns.org/health
```

The workflow copies `compose.prod.yml`, `Caddyfile`, and `scripts/deploy.sh` to
the server. Caddy obtains and renews HTTPS certificates automatically. The
application `.env` is never copied from GitHub.

GitHub and Jira callback URLs must be:

```text
https://<api-domain>/auth/github/callback
https://<api-domain>/jira/callback
```

Jira tickets sync on a polling loop (`ticket_sync_interval_seconds`, default
five minutes) rather than through Jira webhooks.
