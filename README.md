# Overwatch Backend (Eval 1)

FastAPI backend for Overwatch: GitHub/Jira connectors, AST-based architecture graph (tree-sitter → Neo4j), Jira ticket viewer, and Gemini-grounded diagram Q&A.

**Stack:** FastAPI · MongoDB (motor) · Neo4j · tree-sitter · Gemini · GitHub OAuth · Jira OAuth 2.0 (3LO)

## Development setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync                 # create .venv and install all deps (incl. dev group)
cp .env.example .env    # then fill in secrets
uv run uvicorn app.main:app --reload
```

## Tests

```sh
uv run pytest                    # full suite
uv run pytest -m regression      # regression snapshot suite only
```

See `overwatch-eval1-backend-plan.md` for the full build plan and phase breakdown.

## Deployment (eval 1)

**Services (all free-tier):**
- **API** — Railway or Render, Docker deploy using the included `Dockerfile`
- **MongoDB** — [MongoDB Atlas](https://www.mongodb.com/atlas) free tier (M0)
- **Neo4j** — [Neo4j AuraDB](https://neo4j.com/cloud/aura-free/) free tier

**Steps:**
1. Provision Atlas (get `MONGO_URI`) and AuraDB (get `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`).
2. Register GitHub and Atlassian (Jira) OAuth apps; set their callback URLs to
   `https://<your-api-domain>/auth/github/callback` and `https://<your-api-domain>/jira/callback`.
3. Get a Gemini API key.
4. On Railway/Render, set the build to use the repo's `Dockerfile` and configure
   these secrets via the platform's secret manager (never commit them):
   - `JWT_SECRET_KEY`, `FERNET_KEY` (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   - `MONGO_URI`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
   - `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
   - `JIRA_CLIENT_ID`, `JIRA_CLIENT_SECRET`
   - `GEMINI_API_KEY`
   - `APP_BASE_URL` (your deployed API URL — used to build OAuth callback/webhook URLs)
   - `FRONTEND_ORIGIN` (your deployed frontend URL — CORS is locked to exactly this origin)
5. Deploy, then hit `GET /health` to confirm the app booted and connected to Mongo/Neo4j.

**Note on ticket freshness:** Jira tickets sync on a polling loop
(`ticket_sync_interval_seconds`, default 5 min) rather than via Jira webhooks —
real-time sync would need more Atlassian app configuration than eval 1 warrants.
