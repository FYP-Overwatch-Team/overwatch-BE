# Overwatch — Eval 1 Backend Build Plan

**Stack:** FastAPI, MongoDB (users/tokens/repos/tickets), Neo4j (AST graph), Gemini 3.5 Flash (LLM layer), GitHub OAuth + Jira OAuth 2.0 (3LO)

**Scope for eval 1:** signup/login with token refresh, GitHub connector + repo linkage, Jira connector + project linkage, AST-based architecture diagram (multi-language, via a prebuilt tree-sitter wrapper), Jira ticket viewer, LLM-grounded diagram Q&A and flow-walkthrough animation.

**Explicitly out of scope:** T-GNN, BERT ticket classifier, contract checker, ticket-to-service auto-mapping. Do not stub these with fake data — omit the UI for them entirely.

---

## Phase 0 — Project scaffold & infra

- FastAPI project with `app/` layout as below. Use `pydantic-settings` for config, load from `.env`.
- MongoDB via `motor` (async driver). Neo4j via the official `neo4j` async Python driver.
- Structured logging (`structlog` or stdlib `logging` with JSON formatter) — every external API call (GitHub, Jira, Gemini) must log request id/status/latency. You'll need this to debug OAuth and webhook issues, which are the most fragile part of this build.
- Global exception handler that returns a consistent error shape `{error_code, message}` — never leak stack traces to the client.
- Test tooling set up from day one: `pytest`, `pytest-asyncio`, `httpx` (async test client for FastAPI), `mongomock-motor` for Mongo, a disposable Neo4j instance for tests (docker-compose test profile or testcontainers), and `pytest-httpx`/`respx` for mocking outbound GitHub/Jira/Gemini calls. Write every external client behind an interface that's injectable — don't hardcode HTTP calls inline, or the test suite in Phase 10 becomes unbuildable later.
- `docker-compose.yml` for local dev: FastAPI, MongoDB, Neo4j (use `neo4j:5-community` image). Don't fight with AuraDB free-tier connection limits during local dev.

```
backend/
  app/
    main.py
    core/
      config.py              # Settings via pydantic-settings
      security.py             # JWT issuing/verification, Fernet encryption for stored OAuth tokens
      exceptions.py            # custom exception classes + handlers
      logging.py
    api/
      deps.py                 # get_current_user, get_db, etc.
      routes/
        auth.py                # signup, login, refresh, logout
        github.py               # oauth callback, repo list/connect
        jira.py                  # oauth callback, project list/connect
        onboarding.py             # status polling endpoint
        graph.py                   # diagram data
        tickets.py                  # jira ticket list/detail
        query.py                     # LLM Q&A + flow walkthrough
        webhooks.py                   # github push webhook
    integrations/
      github_client.py
      jira_client.py
      gemini_client.py
    parsing/
      ast_service.py           # wraps tree-sitter parsing
      graph_builder.py          # AST -> nodes/edges -> Neo4j upsert
    db/
      mongo.py                  # connection + collection accessors
      neo4j_client.py
      models/
        user.py
        oauth_token.py
        repo.py
        jira_project.py
        ticket.py
    services/
      onboarding_service.py     # orchestrates the multi-step onboarding jobs
      graph_service.py
      ticket_service.py
      llm_grounding_service.py
    workers/
      jobs.py                   # background job runner (see Phase 5 note on concurrency)
  requirements.txt
  docker-compose.yml
  .env.example
```

---

## Phase 1 — Auth: signup/login + token refresh

**Do not build your own password auth from scratch if you can avoid it — use GitHub OAuth as the sole identity provider.** This removes an entire class of security bugs (password storage, reset flows, email verification) that add no value to the FYP demo. If a supervisor specifically requires email/password, add it in Phase 1b, but treat GitHub-as-identity as the default plan.

- `GET /auth/github/login` → redirect to GitHub OAuth consent (scopes: `read:user`, `repo`, `admin:repo_hook`)
- `GET /auth/github/callback` → exchange code for access token, fetch GitHub profile, upsert `User` in Mongo, issue:
  - short-lived **access JWT** (15 min), returned to frontend
  - long-lived **refresh token** (opaque random string, hashed before storage — never store raw refresh tokens), set as `httpOnly`, `Secure`, `SameSite=Lax` cookie
- `POST /auth/refresh` → validate refresh token against hashed value in Mongo, rotate it (issue new refresh token, invalidate old one — refresh token reuse detection: if an already-invalidated token is presented, revoke the entire session family and force re-login, this is the standard mitigation for stolen refresh tokens)
- `POST /auth/logout` → invalidate refresh token
- All protected routes depend on `get_current_user` which validates the access JWT and 401s cleanly on expiry (frontend handles silent refresh)

**Edge case to handle explicitly:** GitHub's own OAuth access token also expires/can be revoked independently of your session JWT. Store it encrypted, and wrap every GitHub API call in a handler that catches 401 and marks the connection as `needs_reauth` in Mongo rather than crashing the request.

---

## Phase 2 — GitHub connector + repo linkage

- `GET /github/repos` — list repos the authenticated GitHub token can access (paginate properly, GitHub caps at 100/page)
- `POST /github/repos/connect` `{repo_full_name}`:
  - Verify the user has admin access to the repo (required to create a webhook) — if not, return a clear error rather than a silent webhook-creation failure
  - Register a webhook for `push` events pointing at `POST /webhooks/github`, with a **per-repo webhook secret** (generate randomly, store encrypted, used for HMAC verification)
  - Shallow-clone the repo (or use GitHub's contents API for smaller repos — pick one, don't build both paths for eval 1)
  - Store `Repo` document: `{repo_full_name, default_branch, webhook_id, webhook_secret_encrypted, parse_status}`
  - Enqueue initial AST parse job (Phase 5), set `parse_status: "pending"`

**Edge case to handle explicitly:** webhook registration can succeed while the clone/parse fails, or vice versa. Track `parse_status` and `webhook_status` as independent fields, not one combined flag — the onboarding UI needs to show which part failed.

---

## Phase 3 — Jira connector + project linkage

- Atlassian OAuth 2.0 (3LO): `GET /jira/login` → redirect to `auth.atlassian.com/authorize` with `offline_access` scope (required to get a refresh token) plus read scopes for Jira work items — **verify exact current scope names against Atlassian's live developer docs before implementing, they've changed naming before and this doc's info could be stale**
- `GET /jira/callback` → exchange code for access + refresh token, fetch accessible sites via `https://api.atlassian.com/oauth/token/accessible-resources`, store `cloud_id` per user
- `GET /jira/projects` — list projects on the selected site
- `POST /jira/projects/connect` `{cloud_id, project_key}` → store `JiraProject` doc, enqueue initial ticket sync job

**Token refresh (do this correctly, it's the part most likely to silently break the demo):**
- Atlassian access tokens expire in ~1 hour. Store `access_token`, `refresh_token` (both encrypted), and `expires_at`.
- Every Jira API call goes through a wrapper that checks `expires_at` first and refreshes proactively if within a buffer window (e.g. 2 minutes), rather than waiting for a 401. This avoids a failed request mid-demo.
- Refresh token rotation: Atlassian issues a new refresh token on every refresh — you must overwrite the stored one every time or the next refresh will fail.

---

## Phase 4 — Onboarding orchestration

- `GET /onboarding/status` returns:
```json
{
  "github": {"connected": true, "parse_status": "in_progress"},
  "jira": {"connected": true, "sync_status": "done"}
}
```
- Frontend polls this every few seconds during onboarding and routes to the dashboard once both are `done`. Keep this endpoint cheap — read from Mongo, don't touch GitHub/Jira/Neo4j live.
- Both connectors are independent — user can finish GitHub onboarding without Jira and vice versa; don't hard-block the dashboard on both being connected, since a supervisor demo might test partial setup.

---

## Phase 5 — AST parsing

- **Use a prebuilt multi-language wrapper, don't hand-roll grammar loading.** `tree_sitter_languages` (or `tree_sitter_language_pack`, its actively-maintained successor) bundles compiled grammars for most languages including TS/JS and Python — this is the same approach used by tools like Aider's repo-mapping feature. Verify current package name/maintenance status before committing to it, `tree_sitter_languages` has had maintenance gaps.
- `ast_service.py`:
  - Walk repo tree, skip `node_modules`, `.git`, `dist`, `venv`, etc. (maintain an ignore list, also respect `.gitignore` if present)
  - For each source file, detect language by extension, parse with the matching grammar
  - Extract: import/require statements, top-level class/function definitions, file path
- `graph_builder.py`:
  - **Service/node grouping heuristic for eval 1:** one node per top-level directory under the repo root (configurable depth). State this explicitly as a known simplification in the demo, not a hidden shortcut.
  - Resolve import paths to internal files where possible (relative imports, common path aliases) → build edges between nodes. External package imports become a single "external dependency" edge type, not individual nodes — don't try to model npm packages as graph nodes.
  - Upsert into Neo4j: `MERGE` on stable node ids (derived from path, not autoincrement) so re-parses update rather than duplicate.

**Background job concurrency:** for eval 1, `BackgroundTasks` (FastAPI's built-in) is fine for a single-instance deployment — don't introduce Celery/Redis unless you're already deploying multiple workers. But guard against overlapping parses of the same repo (e.g. a webhook firing while the initial parse is still running) with a simple `parse_status` lock checked before starting a new job.

---

## Phase 6 — Webhook receiver + incremental re-parse

- `POST /webhooks/github`:
  - **Verify HMAC signature first, before touching the payload** — reject with 401 on mismatch, this is your only defense against forged webhook calls
  - **Deduplicate by `X-GitHub-Delivery` header** — GitHub retries webhooks on timeout, and without dedup you'll double-process pushes
  - Extract changed file list from the payload, re-parse only those files, upsert affected nodes/edges (don't do a full repo re-parse on every push — this is what makes the "updates within 60 seconds" claim achievable)
  - Return 200 immediately, do the actual re-parse as a background task — GitHub expects a fast webhook response and will mark it failed on timeout

---

## Phase 7 — Jira ticket sync

- Pull tickets via `GET /rest/api/3/search` (JQL: `project = {key}`), paginate (Jira caps at 100 results/page)
- Store in Mongo as-is (don't over-normalize) — `{ticket_key, summary, status, assignee, priority, updated_at, raw}`
- `GET /tickets` supports filter by status/assignee, sort by updated, paginated
- Re-sync strategy for eval 1: simple polling (e.g. every 5 min via a scheduled job) rather than Jira webhooks — Jira webhook setup requires more Atlassian app configuration than is worth it for a first eval. Say this explicitly if asked why tickets aren't real-time.

---

## Phase 8 — LLM layer (Gemini 3.5 Flash)

- `gemini_client.py`: thin wrapper around the Gemini API, single retry on transient errors, hard timeout (don't let a slow LLM call hang the request — 15s timeout is reasonable for a demo)
- **Grounding is the core constraint — enforce it in the prompt and validate the output, don't just trust it:**
  - System instruction: "Only reference node names and edges provided in the graph context below. Never invent a service that isn't listed."
  - Build the graph context by querying Neo4j: for `/query` with a focused node, pull a 2-hop neighborhood; without a focused node, pull a lightweight summary (node names + edge types only — not full file lists, keep the prompt small and cheap)
- Two distinct endpoints, two distinct prompts — don't reuse one prompt shape for both:
  - `POST /query/ask` `{question, focused_node_id?}` → free-text answer + `highlighted_node_ids[]` (resolve mentioned node names back to ids server-side, don't trust the LLM to return valid ids directly — validate every returned id actually exists in the current graph before sending to frontend)
  - `POST /query/flow` `{question}` → ask specifically for an ordered JSON list of node ids representing the flow steps, used to drive the animation. Use Gemini's structured output / JSON mode rather than parsing free text.
- **Validation step is not optional:** after any LLM response, cross-check every node id it returns against Neo4j before returning to the client. If the LLM hallucinates a node, drop it rather than passing it through — a broken highlight in the demo (pointing at a node that doesn't exist) is worse than a slightly incomplete answer.

---

## Phase 9 — Deployment

- Railway or Render free tier for FastAPI (both support Docker deploys, faster to set up than EC2 for a one-time eval)
- MongoDB Atlas free tier
- Neo4j AuraDB free tier
- Environment secrets (GitHub client secret, Jira client secret, Gemini API key, JWT signing key, Fernet encryption key) — never commit these, use the platform's secret manager
- CORS: lock `allow_origins` to your actual frontend domain, not `*` — GitHub/Jira tokens flowing through this API make an open CORS policy a real risk, not just a lint warning

---

## Phase 10 — Testing & regression suite

Build tests alongside each phase, not as a bolt-on at the end — the plan above is ordered so each phase's tests can be written the same day as the code. Structure:

```
backend/
  tests/
    unit/
      test_security.py          # JWT issue/verify, Fernet encrypt/decrypt, refresh rotation logic
      test_ast_service.py        # tree-sitter extraction on fixture repos
      test_graph_builder.py       # AST output -> node/edge shape, id stability across re-parse
      test_llm_grounding.py        # prompt/context assembly, node-id validation logic
    integration/
      test_auth_flow.py           # signup -> login -> refresh -> reuse-detection revocation
      test_github_onboarding.py    # connect repo -> webhook created -> parse job enqueued (GitHub mocked)
      test_jira_onboarding.py       # connect project -> ticket sync (Jira mocked)
      test_webhook_dedup.py          # same X-GitHub-Delivery id twice -> processed once
      test_webhook_signature.py       # bad HMAC -> 401, good HMAC -> 200
      test_graph_api.py                # seeded Neo4j -> GET /graph shape
      test_query_endpoint.py            # Gemini mocked -> hallucinated node id gets filtered out
    regression/
      fixtures/
        sample_repo_ts/            # small fixed TS repo checked into the test suite
        sample_repo_py/             # small fixed Python repo
        sample_jira_tickets.json     # fixed ticket payload
      test_parse_snapshot.py         # parse sample_repo_ts/py, assert exact node/edge output against a stored snapshot — catches silent breakage when the tree-sitter dependency or extraction logic changes
      test_incremental_reparse.py     # apply a fixed diff to sample_repo, assert only the affected nodes/edges change, everything else stays byte-identical
      test_token_refresh_expiry.py     # simulate a token at expires_at - 1min, assert proactive refresh fires; simulate an already-rotated refresh token, assert session-family revocation
      test_llm_flow_ordering.py         # fixed graph + fixed mocked Gemini response -> assert flow endpoint returns exact ordered id list
```

**Why the `regression/` folder is separate from `integration/`:** integration tests check that a feature works today; regression tests pin down exact expected output against fixed inputs (snapshot-style) so a future change — a tree-sitter version bump, a prompt tweak, a Neo4j query change — that silently alters output gets caught. This matters most for the three places output is easy to accidentally change without noticing: AST parsing, incremental re-parse diffing, and LLM-driven flow ordering.

- **CI**: run the full suite (unit + integration + regression) on every push/PR via GitHub Actions — spin up Mongo and Neo4j as service containers in the workflow, never test against real GitHub/Jira/Gemini (always mocked) so CI doesn't depend on external quota or network flakiness and doesn't leak real tokens into logs.
- **Coverage target for eval 1**: don't chase a number — prioritize coverage on the four fragile surfaces already flagged in this plan: webhook signature/dedup, token refresh rotation, AST node-id stability across re-parse, and LLM output validation. A gap in any of these is what breaks mid-demo, not a gap in, say, the ticket list pagination.
- Add a `pytest -m regression` marker so the regression suite can be run in isolation before a demo as a final "did anything drift" check.

---

## Cross-cutting things Claude Code should not skip

1. **Encrypt every stored OAuth/refresh token at rest** (Fernet, key from env, never logged).
2. **Never log tokens or full webhook payloads** at info level — redact before logging.
3. **Idempotent webhook handling** (dedup key above) — required or you'll get duplicate graph updates.
4. **Independent status tracking per integration** (GitHub parse vs Jira sync) — don't collapse into one boolean.
5. **LLM output validation against the actual graph** before returning to frontend — no unvalidated node ids reach the client.
6. **Rate limit awareness**: GitHub (5000/hr authenticated), Jira (varies by site, check response headers) — wrap clients to read `X-RateLimit-Remaining` and back off, don't let a burst of onboarding requests get the app rate-limited mid-demo.
7. **Tests are not optional per phase** — each phase in this plan should be considered incomplete until its corresponding tests in Phase 10 pass, not something deferred to a final testing pass.
