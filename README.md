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
