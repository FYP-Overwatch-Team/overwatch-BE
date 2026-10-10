# Backend (rizwan521/overwatch-BE)

FastAPI on Python 3.14 with uv. MongoDB via motor (Atlas in prod), Neo4j (Aura in prod), tree-sitter parsing into an architecture graph, Gemini-grounded Q&A, GitHub and Jira OAuth connectors, GitHub push webhooks, and a Jira polling loop. The worktree is `../overwatch-BE-review`.

Layout: `app/api/routes` (thin routers, auth via `Depends(get_current_user_id)`) → `app/services` → `app/db` (collection accessors in `mongo.py`, `neo4j_client.py`) and `app/integrations` (GitHub, Jira, Gemini clients). Background work is in `app/workers/jobs.py`. Errors are `AppError` subclasses in `app/core/exceptions.py`. `docs/FRONTEND_GUIDE.md` is the FE contract.

## Install

- `uv sync --locked` in the review worktree. `--locked` fails if `uv.lock` doesn't match `pyproject.toml`, which is itself a finding.
- Tests set their own env in `tests/conftest.py`; no `.env` is needed to run them.

## Validate, one process at a time

CI (`.github/workflows/ci.yml`) runs exactly these, so match it:

- **Unit and integration:** `uv run pytest -q -m "not regression"`.
- **Regression snapshots:** `uv run pytest -q -m regression`. A changed snapshot under `tests/regression/fixtures` needs a reason in the PR; regenerating snapshots to make a test pass hides regressions.
- **Single test:** `uv run pytest tests/unit/test_x.py::test_name -q`.
- **Prove a test is real:** revert the fix locally, run the single test, watch it fail, restore.
- **Compile check of touched files:** `uv run python -m py_compile <files>`. There is no linter or type checker configured; don't introduce one in a review.
- **Pre-push hook** (`.githooks/pre-push`) runs the full `uv run pytest`. It's only active after `git config core.hooksPath .githooks`.

## What the test fakes hide

The autouse fixtures swap in `mongomock-motor`, an `InMemoryGraphRepository` and (opt-in) `FakeGeminiClient`. Green tests therefore say nothing about:

- **Mongo indexes and uniqueness:** mongomock doesn't enforce real index semantics. No `create_index` exists in the app today, so any "this is unique" assumption is unenforced in prod.
- **Transactions, aggregation edge cases, `$` operators** mongomock implements differently or not at all.
- **Cypher:** the in-memory graph repo never runs the queries in `Neo4jGraphRepository`. A Cypher change needs a run against real Neo4j: `docker compose up mongo neo4j -d` and a scratch script or test pointing `NEO4J_URI` at it.
- **Gemini:** prompt and schema changes are only checked for shape. Look at the prompt text and the grounding rules in `llm_grounding_service.py` by hand.
- **External HTTP:** `respx` mocks. Check the mocked request matches the real GitHub/Jira API (URL, method, pagination, error bodies).

## Deploy facts for the report

- **Merge to main = prod deploy.** CI builds an image, pushes to GHCR, deploys to EC2 over SSH, health checks `/health`, and rolls back on failure. There's no staging. Anything risky is a Deploy constraint the moment it merges.
- **New settings** in `app/core/config.py` need a value in `/opt/overwatch/.env` on the EC2 host before merge, unless the default is safe for prod. A default like `jwt_secret_key = "dev-only-secret"` is never safe; flag new secrets with dev defaults.
- **`.env.example`** must list every new setting.
- **OAuth and webhook URLs:** changes to callback paths or `APP_BASE_URL`/`PUBLIC_WEBHOOK_BASE_URL` usage need the GitHub OAuth app, Jira app and existing repo webhooks updated.
- **Data shape changes** to Mongo documents or graph nodes need a backfill or tolerant reads, because existing prod documents keep the old shape.
- **`compose.prod.yml`, `Caddyfile`, `scripts/deploy.sh`, `Dockerfile`** changes affect the deploy itself; read them as carefully as app code.
- **FE coupling:** a changed response shape or `error_code` is an FE Deploy constraint and needs `docs/FRONTEND_GUIDE.md` updated in the same PR.

## Repo pitfalls (also in scrutiny.md)

- Every per-user query must filter by `user_id`; one missing filter leaks another user's repos, tickets or tokens.
- The Neo4j graph is keyed by `repo_full_name`, not user. Two users connecting the same repo share and overwrite one graph, and a disconnect by one affects the other.
- Webhook dedup is find-then-insert on `webhook_deliveries` with no unique index, so concurrent retries can both process.
- `BackgroundTasks` jobs run in-process on one instance: lost on restart, not retried, and can overlap for the same repo.
- `get_settings()` is `lru_cache`d; tests must `cache_clear()` after changing env (conftest does it for the autouse env).
- Refresh rotation (`auth_service.rotate_refresh_token`) is find-then-update, not one conditional update, so two concurrent refreshes with the same token can both pass the `revoked` check.
- Third-party tokens are Fernet-encrypted at rest (`encrypt_secret`); refresh tokens are stored as SHA-256 hashes. New secrets must follow the same pattern.
- structlog redaction only covers keys in `SENSITIVE_KEYS` plus bearer strings (`app/core/logging.py`). A new secret logged under another key name leaks.
- Unhandled exceptions become a generic 500 `internal_error`; raise an `AppError` subclass with a specific `error_code` for anything the FE should handle.
