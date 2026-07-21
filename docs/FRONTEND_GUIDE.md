# Overwatch Backend — Frontend Integration Guide

This is a complete reference for building the frontend against the Overwatch
backend, generated directly from the current code (`app/`). If behavior here
ever looks wrong, the route/service file listed next to each section is the
source of truth.

**Base URL (dev):** `http://localhost:8000`
**Content type:** all request/response bodies are JSON unless noted.
**Auth:** GitHub OAuth only — there is no email/password signup.

---

## Table of contents

1. [Big picture: how a user flow works](#1-big-picture-how-a-user-flow-works)
2. [Authentication](#2-authentication)
3. [Error format (every endpoint)](#3-error-format-every-endpoint)
4. [GitHub connector](#4-github-connector)
5. [Jira connector](#5-jira-connector)
6. [Onboarding status](#6-onboarding-status)
7. [Architecture graph](#7-architecture-graph)
8. [Tickets](#8-tickets)
9. [LLM Q&A / flow walkthrough](#9-llm-qa--flow-walkthrough)
10. [Webhooks (backend-to-backend, not called by frontend)](#10-webhooks-backend-to-backend-not-called-by-frontend)
11. [Misc endpoints](#11-misc-endpoints)
12. [Full error_code reference](#12-full-error_code-reference)
13. [Environment variables the frontend cares about](#13-environment-variables-the-frontend-cares-about)
14. [Things that are deliberately simplified (say this in a demo, don't hide it)](#14-things-that-are-deliberately-simplified)

---

## 1. Big picture: how a user flow works

```
1. User clicks "Sign in with GitHub"
     -> GET /auth/github/login          (browser redirect, not fetch)
     -> GitHub consent screen
     -> GET /auth/github/callback       (GitHub redirects here)
     -> backend redirects to:
        {FRONTEND_ORIGIN}/auth/complete#access_token=<jwt>
     -> frontend reads the JWT from the URL fragment and stores it in memory

2. Frontend polls onboarding status while the user connects things:
     -> GET /onboarding/status          (Bearer token)

3. User connects a GitHub repo:
     -> GET /github/repos               (list repos to choose from)
     -> POST /github/repos/connect      (webhook + clone + parse kicked off)
     -> poll /onboarding/status or /github/repos/connected until parse_status = "done"

4. User connects Jira (optional, independent of GitHub):
     -> GET /jira/login                 (browser redirect, needs Bearer token — see note below)
     -> Atlassian consent screen
     -> GET /jira/callback              (Atlassian redirects here)
     -> backend redirects to {FRONTEND_ORIGIN}/onboarding?jira=connected
     -> GET /jira/projects              (list projects to choose from)
     -> POST /jira/projects/connect     (ticket sync kicked off)

5. Dashboard:
     -> GET /graph?repo_full_name=...   (architecture diagram)
     -> GET /tickets                    (Jira ticket list)
     -> POST /query/ask                 (chat-style Q&A over the diagram)
     -> POST /query/flow                (animated flow walkthrough)

6. Access token expires after 15 minutes:
     -> POST /auth/refresh              (uses httpOnly cookie, no body needed)
     -> get a new access_token, retry the failed request
```

---

## 2. Authentication

Source: `app/api/routes/auth.py`, `app/services/auth_service.py`, `app/api/deps.py`, `app/core/security.py`

### Identity provider

GitHub is the **only** identity provider. There is no `/auth/signup` or
`/auth/login` with a password — signing in with GitHub both creates the
account (on first login) and logs the user in.

### Login flow

**`GET /auth/github/login`**
Not a fetch call — navigate the browser here directly (`window.location.href = ...`).
- Sets an `overwatch_oauth_state` httpOnly cookie (CSRF nonce, 10 min TTL, scoped to path `/auth`).
- Redirects (307) to GitHub's OAuth consent screen requesting scopes `read:user repo admin:repo_hook`.

**`GET /auth/github/callback?code=...&state=...`**
GitHub redirects here automatically after consent — the frontend never calls this directly.
- Validates `state` against the `overwatch_oauth_state` cookie (constant-time compare). Mismatch → `401 oauth_state_mismatch`.
- Exchanges the code, fetches the GitHub profile, upserts the `User` document, stores the GitHub access token encrypted.
- Issues a session and redirects (307) to:
  ```
  {FRONTEND_ORIGIN}/auth/complete#access_token=<jwt>
  ```
  Sets the refresh token as an httpOnly cookie in the same response (see below).
- **Frontend must implement a route at `/auth/complete`** that reads `access_token` from the URL **fragment** (`window.location.hash`, not query params — fragments are never sent to the server, which is intentional) and stores it in memory/state (not localStorage — see note below).

### Tokens

| Token | Lifetime | Where it lives | How the frontend uses it |
|---|---|---|---|
| **Access token** (JWT) | 15 minutes | Returned to frontend in the URL fragment / refresh response body | Send as `Authorization: Bearer <token>` on every protected request |
| **Refresh token** | 30 days | httpOnly, `Secure` (prod)/`SameSite=Lax` cookie, path `/auth`, name `overwatch_refresh` | Never touched by JS — the browser sends it automatically to `/auth/refresh` and `/auth/logout` |

The access JWT is a standard HS256 JWT with `sub` (user id) and `exp` claims — decode client-side if you need the user id without an extra request, but always treat `exp` as authoritative only after a real 401.

**Recommendation:** keep the access token in memory (a JS variable / React context), not localStorage, since it's short-lived and refreshable. Losing it on a hard refresh is expected — call `POST /auth/refresh` on app boot to silently re-establish a session from the refresh cookie.

### Refresh

**`POST /auth/refresh`** — no body required; the refresh cookie does the work.
```json
// 200 response
{ "access_token": "<new jwt>", "token_type": "bearer" }
```
- Rotates the refresh token (old one is invalidated, new one set as the cookie automatically).
- **Reuse detection:** if a refresh token that was already rotated/invalidated is presented again (e.g. stale tab, token theft), the **entire session family is revoked** and this call returns `401 refresh_token_reused`. Every device/tab using that session must log in again — this is intentional, not a bug.
- `401 missing_refresh_token` if there's no cookie at all → treat as logged out, redirect to login.
- `401 refresh_token_expired` if the 30-day TTL has passed.

**Recommended pattern:** wrap your HTTP client so that any `401` with `error_code` in `{"invalid_token", "missing_token"}` triggers exactly one silent `POST /auth/refresh` + retry. If the refresh call itself 401s, redirect to `/auth/github/login`.

### Logout

**`POST /auth/logout`** — no body. Revokes the whole refresh-token family and clears the cookie.
```json
{ "status": "logged_out" }
```
Frontend should also discard the in-memory access token.

### Protected routes

Every non-auth endpoint requires:
```
Authorization: Bearer <access_token>
```
Missing header → `401 missing_token`. Invalid/expired/tampered JWT → `401 invalid_token`. Valid JWT for a user that no longer exists in the DB → `401 unknown_user` (shouldn't normally happen).

### Current user

**`GET /me`** (defined directly in `app/main.py`)
```json
{
  "id": "a1b2c3...",
  "github_login": "octocat",
  "name": "Octo Cat",
  "avatar_url": "https://avatars.githubusercontent.com/..."
}
```

---

## 3. Error format (every endpoint)

Source: `app/core/exceptions.py`

**Every** error response — validation, domain errors, 404s, unhandled
exceptions — has the exact same shape. There is never a raw stack trace or an
inconsistent shape sent to the client:

```json
{ "error_code": "some_snake_case_code", "message": "human-readable text" }
```

| HTTP status | When |
|---|---|
| 400 | Generic bad request (`error_code: "bad_request"` unless overridden) |
| 401 | Auth failures — see the [error_code reference](#12-full-error_code-reference) |
| 403 | Forbidden (e.g. connecting a repo you don't admin) |
| 404 | Not found (repo not connected, graph not ready, etc.) |
| 409 | Conflict (already connected) |
| 422 | Request body/query failed Pydantic validation — `error_code: "validation_error"`, generic message (field-level detail is intentionally not exposed) |
| 500 | Unhandled server error — always `{"error_code": "internal_error", "message": "An internal error occurred"}`, real detail only in server logs |
| 502 | Upstream (GitHub/Jira/Gemini) failure — `error_code: "external_service_error"` unless overridden |

Every response also carries an `x-request-id` header — include it when reporting bugs to the backend team.

---

## 4. GitHub connector

Source: `app/api/routes/github.py`, `app/services/repo_service.py`

### List repos available to connect

**`GET /github/repos`** (auth required)
```json
{
  "repos": [
    {
      "full_name": "octocat/hello-world",
      "private": false,
      "default_branch": "main",
      "admin": true,
      "updated_at": "2026-07-01T00:00:00Z"
    }
  ]
}
```
- `admin: false` means this token can't create a webhook on that repo — you can still show it in the UI, but connecting it will 403 (see below). Consider graying it out or showing a tooltip.
- Fully paginated server-side (backend walks all GitHub pages); one call returns everything.

### Connect a repo

**`POST /github/repos/connect`** (auth required)
```json
// request
{ "repo_full_name": "octocat/hello-world" }
```
```json
// 201 response
{
  "repo_full_name": "octocat/hello-world",
  "default_branch": "main",
  "webhook_status": "created",   // "pending" | "created" | "failed"
  "parse_status": "pending"      // "pending" | "in_progress" | "done" | "failed"
}
```
**Important:** this response is a snapshot taken right after the connect call returns — webhook registration, repo clone, and the initial AST parse all happen server-side (partially in the background). `parse_status` will very likely still say `"pending"` in this response. **Poll `/onboarding/status` or `/github/repos/connected` to watch it progress to `"done"`.**

`webhook_status` and `parse_status` are **tracked independently** — one can fail while the other succeeds (e.g. webhook creation fails but the clone/parse still completes). Show both, don't collapse them into one "connected ✓" boolean.

Failure modes:
- `403 repo_admin_required` — the connecting user isn't a repo admin (required to register a webhook). Nothing is created; safe to just show an error.
- `409 repo_already_connected` — already connected by this user.

### List connected repos (for dashboard/onboarding polling)

**`GET /github/repos/connected`** (auth required)
```json
{
  "repos": [
    {
      "repo_full_name": "octocat/hello-world",
      "default_branch": "main",
      "webhook_status": "created",
      "parse_status": "done",
      "parse_error": null   // populated with a message string if parse_status == "failed"
    }
  ]
}
```

---

## 5. Jira connector

Source: `app/api/routes/jira.py`, `app/services/jira_service.py`, `app/integrations/jira_client.py`

### Login

**`GET /jira/login`** (auth required — this endpoint reads `Authorization: Bearer <token>` via `get_current_user_id`, unlike `/auth/github/login` which needs no auth because it *creates* the session; Jira's login *links to* an existing one, so it needs to know which user is linking)

**This is a known integration rough edge:** a plain browser navigation (`window.location.href = ...`) cannot attach an `Authorization` header, so this endpoint can't be triggered that way like `/auth/github/login` can. Until/unless backend changes this to accept the token another way (e.g. a short-lived query-param ticket), the working pattern is to `fetch()` this endpoint with the Bearer header and `redirect: "manual"` (or have your framework's server-side layer make the call and forward the resulting redirect), rather than a raw `<a href>`/`window.location` navigation. Flag this to backend if it's blocking implementation — it's the one endpoint in the API that doesn't fit the "just navigate the browser" OAuth pattern used everywhere else.

- Sets `overwatch_jira_state` httpOnly cookie (10 min, path `/jira`). State value is `"{user_id}.{random_nonce}"`.
- Redirects to Atlassian's consent screen. Scopes requested: `read:jira-work read:jira-user offline_access`.

**`GET /jira/callback?code=...&state=...`** — Atlassian redirects here automatically.
- Validates state against the cookie. Mismatch → `401 oauth_state_mismatch`.
- Exchanges the code, fetches accessible Atlassian sites, stores the **first** accessible site's `cloud_id` (eval-1 simplification — see [§14](#14-things-that-are-deliberately-simplified)).
- Redirects to `{FRONTEND_ORIGIN}/onboarding?jira=connected`. **Frontend should handle this query param** to refresh onboarding status / show a success toast.
- `404 no_jira_sites` (surfaced as a redirect-time error, not directly visible to frontend JS since it's a browser redirect — if this matters, ask backend to redirect to an error route instead) if the Atlassian account has no accessible Jira sites.

### List projects

**`GET /jira/projects`** (auth required)
```json
{
  "projects": [
    { "key": "OVR", "name": "Overwatch", "id": "10001" }
  ]
}
```
- `401 jira_needs_reauth` if Jira isn't connected yet, or the refresh token was rejected by Atlassian (user must redo `/jira/login`).

### Connect a project

**`POST /jira/projects/connect`** (auth required)
```json
// request
{ "cloud_id": "cloud-1", "project_key": "OVR" }
```
```json
// 201 response
{ "project_key": "OVR", "cloud_id": "cloud-1", "sync_status": "pending" }
```
Ticket sync runs in the background afterward. `409 project_already_connected` if already connected.

### Token refresh behavior (informational — happens transparently server-side)

Jira access tokens expire hourly. The backend refreshes them **proactively** (2 minutes before expiry) on every Jira-dependent call, so the frontend never sees a mid-request Jira token expiry. If Atlassian rejects the refresh outright, the connection is marked `needs_reauth` and every subsequent Jira call 401s with `jira_needs_reauth` until the user redoes `/jira/login`.

---

## 6. Onboarding status

Source: `app/api/routes/onboarding.py`

**`GET /onboarding/status`** (auth required) — cheap, DB-only, safe to poll every few seconds.

```json
{
  "github": {
    "connected": true,
    "needs_reauth": false,
    "repo_connected": true,
    "parse_status": "in_progress",
    "webhook_status": "created"
  },
  "jira": {
    "connected": false,
    "needs_reauth": false,
    "project_connected": false,
    "sync_status": null
  }
}
```

- `connected` = the OAuth connection itself is active (token stored, not `needs_reauth`).
- `repo_connected` / `project_connected` = whether *a* repo/project has been linked (only the **most recently connected** one's status is reported — this endpoint reports one repo/project per provider, not a list; use `/github/repos/connected` for the full list).
- **GitHub and Jira are fully independent.** Do not block the dashboard on both being done — a user might only ever connect GitHub.
- Poll this during onboarding; stop polling once the statuses you care about hit a terminal state (`"done"` or `"failed"`).

---

## 7. Architecture graph

Source: `app/api/routes/graph.py`, `app/parsing/graph_builder.py`, `app/services/graph_service.py`

**`GET /graph?repo_full_name=octocat/hello-world`** (auth required)

```json
{
  "repo_full_name": "octocat/hello-world",
  "parse_status": "done",
  "nodes": [
    {
      "id": "octocat/hello-world:api",
      "name": "api",
      "kind": "module",
      "file_count": 4,
      "definition_count": 6
    },
    {
      "id": "octocat/hello-world:__external__",
      "name": "external dependencies",
      "kind": "external",
      "file_count": 0,
      "definition_count": 0
    }
  ],
  "edges": [
    {
      "source": "octocat/hello-world:api",
      "target": "octocat/hello-world:services",
      "type": "DEPENDS_ON",
      "weight": 3
    },
    {
      "source": "octocat/hello-world:api",
      "target": "octocat/hello-world:__external__",
      "type": "EXTERNAL_DEPENDENCY",
      "weight": 2
    }
  ]
}
```

### Node/edge model — read this before building the diagram renderer

- **One node per top-level directory** in the repo (e.g. `api/`, `services/`, `utils/`). Files directly in the repo root become a synthetic node named `"root"`. This is a stated eval-1 simplification, not a bug — a directory is not necessarily "a microservice."
- **Node ids are stable** across re-parses (`"{repo_full_name}:{directory_name}"`) — safe to use as React keys / diagram layout anchors that persist across polling refreshes.
- `kind` is `"module"` for normal directory nodes, `"external"` for the one synthetic external-dependency node (present only if the repo has at least one unresolved/external import).
- **Edge types:**
  - `"DEPENDS_ON"` — an internal import from one directory's code into another's.
  - `"EXTERNAL_DEPENDENCY"` — one or more imports of third-party packages (npm/pip packages are never modeled as individual nodes — they all collapse into edges pointing at the single `__external__` node).
- `weight` = number of import statements contributing to that edge (useful for line thickness in a diagram, not a hard metric).
- If `parse_status` is `"pending"` or `"in_progress"`, `nodes`/`edges` will likely be empty or stale — show a loading state rather than an empty diagram.
- `404 repo_not_connected` if the repo hasn't been connected by this user.

### Live updates

Every push to the repo's **default branch specifically** (pushes to other
branches are ignored) re-parses **only the changed files** via a GitHub
webhook (server-side, not something the frontend triggers) and updates the
graph within roughly the same request cycle. Poll `/graph` again after
showing a "repo updated" notification, or just re-fetch on an interval while
the user has the diagram open.

### Supported source languages

Only files with these extensions are parsed into the graph — anything else
(docs, JSON/YAML configs, images, etc.) is silently skipped and never
appears as part of a node's `file_count`:

`.py` `.js` `.jsx` `.mjs` `.cjs` `.ts` `.tsx`

`node_modules`, `.git`, `dist`, `build`, `venv`/`.venv`, `__pycache__`, and a
few similar directories are always excluded, and the repo's own `.gitignore`
is respected on top of that.

---

## 8. Tickets

Source: `app/api/routes/tickets.py`, `app/services/ticket_service.py`

**`GET /tickets`** (auth required)

Query params (all optional):
| Param | Type | Default | Notes |
|---|---|---|---|
| `project_key` | string | — | e.g. `"OVR"` |
| `status` | string | — | exact match against Jira's status name, e.g. `"In Progress"` |
| `assignee` | string | — | exact match against the assignee's display name |
| `page` | int | `1` | 1-indexed |
| `page_size` | int | `25` | max `100` |

```json
{
  "tickets": [
    {
      "ticket_key": "OVR-2",
      "project_key": "OVR",
      "summary": "Fix login redirect loop",
      "status": "In Progress",
      "assignee": "Sam Lee",
      "priority": "Highest",
      "updated_at": "2026-07-15T09:30:00.000+0000"
    }
  ],
  "total": 3,
  "page": 1,
  "page_size": 25
}
```
- Sorted by `updated_at` descending.
- Results are scoped to the requesting user automatically — no need to pass a user filter.
- `assignee` can be `null` (unassigned tickets) — filtering by `assignee=null` as a literal string won't match; there's currently no "unassigned only" filter exposed.
- Tickets refresh via a background poll every 5 minutes (not real-time / no Jira webhooks — see [§14](#14-things-that-are-deliberately-simplified)). There's no manual "sync now" endpoint currently.

---

## 9. LLM Q&A / flow walkthrough

Source: `app/api/routes/query.py`, `app/services/llm_grounding_service.py`

Both endpoints require the target repo to already be connected (`404 repo_not_connected` otherwise) and to have a non-empty graph (`404 graph_not_ready` if the parse hasn't produced any nodes yet).

### Ask a question about the diagram

**`POST /query/ask`** (auth required)
```json
// request
{
  "repo_full_name": "octocat/hello-world",
  "question": "What does the api module depend on?",
  "focused_node_id": "octocat/hello-world:api"   // optional
}
```
```json
// 200 response
{
  "answer": "The api module depends on services and one external package.",
  "highlighted_node_ids": [
    "octocat/hello-world:api",
    "octocat/hello-world:services"
  ]
}
```
- If `focused_node_id` is provided, the model only sees that node's 2-hop neighborhood (cheaper, more focused answers). Pass the `id` from a `/graph` node the user clicked on.
- If omitted, the model sees a lightweight summary of the **whole** graph (names + edge types only, not full file lists).
- `404 node_not_found` if `focused_node_id` doesn't exist in the current graph.
- **`highlighted_node_ids` is guaranteed to only contain ids that exist in the current graph right now** — the backend validates every id the LLM returns and silently drops anything hallucinated. Safe to feed directly into your diagram's "highlight these nodes" logic without any existence checking on the frontend.

### Flow walkthrough (for the animation feature)

**`POST /query/flow`** (auth required)
```json
// request
{ "repo_full_name": "octocat/hello-world", "question": "Walk me through a login request" }
```
```json
// 200 response
{
  "step_node_ids": [
    "octocat/hello-world:api",
    "octocat/hello-world:services",
    "octocat/hello-world:db"
  ]
}
```
- `step_node_ids` is an **ordered** list — drive your flow animation by stepping through it in order (e.g. highlight/pulse each node in sequence).
- Same validation guarantee as `/query/ask`: every id is real and currently in the graph; hallucinated steps are dropped, not passed through with a broken reference.
- Can return an empty list if nothing resolved — handle that as "couldn't determine a flow" in the UI rather than a hard error.

### Error to handle specifically

`502 llm_bad_output` — Gemini returned something the backend couldn't parse as JSON. Rare, but treat like any other transient failure (retry button), not a validation error.

---

## 10. Webhooks (backend-to-backend, not called by frontend)

Source: `app/api/routes/webhooks.py`

`POST /webhooks/github` exists purely for GitHub to call. It is not part of
the frontend's integration surface — listed here only so you know it's why
the graph updates without the user doing anything after a `git push`.

---

## 11. Misc endpoints

**`GET /health`** — no auth. `{"status": "ok", "env": "dev"}`. Useful for a connectivity check / status page.

---

## 12. Full `error_code` reference

Every value that currently appears anywhere in the codebase, grouped by area. Unless noted, message text is human-readable and safe to show directly, but treat `error_code` (not the message) as the thing you branch logic on — messages can change wording.

| error_code | HTTP | Where | Meaning |
|---|---|---|---|
| `validation_error` | 422 | any endpoint | request body/query failed schema validation |
| `http_error` | (varies) | any endpoint | generic Starlette HTTP error (e.g. hitting an unknown route → 404) |
| `internal_error` | 500 | any endpoint | unhandled server exception; detail is server-log-only |
| `missing_token` | 401 | any protected endpoint | no `Authorization` header |
| `invalid_token` | 401 | any protected endpoint | JWT invalid/expired/tampered |
| `unknown_user` | 401 | any protected endpoint | JWT valid but user no longer exists |
| `oauth_state_mismatch` | 401 | `/auth/github/callback`, `/jira/callback` | CSRF state cookie missing or mismatched |
| `missing_refresh_token` | 401 | `/auth/refresh` | no refresh cookie present |
| `refresh_token_reused` | 401 | `/auth/refresh` | replay of an already-rotated token — whole session family revoked, force re-login |
| `refresh_token_expired` | 401 | `/auth/refresh` | refresh token past its 30-day TTL |
| `invalid_refresh_token` | 401 | `/auth/refresh` | token not recognized at all |
| `github_needs_reauth` | 401 | any GitHub-backed endpoint | GitHub token missing/revoked; user must redo `/auth/github/login` |
| `jira_needs_reauth` | 401 | any Jira-backed endpoint | Jira token missing/refresh rejected; user must redo `/jira/login` |
| `repo_admin_required` | 403 | `POST /github/repos/connect` | user lacks admin rights on the repo (needed for webhook) |
| `repo_already_connected` | 409 | `POST /github/repos/connect` | duplicate connect |
| `repo_not_found` | 404 | (github client internal) | repo doesn't exist / no access |
| `repo_not_connected` | 404 | `/graph`, `/query/ask`, `/query/flow` | target repo not connected by this user |
| `project_already_connected` | 409 | `POST /jira/projects/connect` | duplicate connect |
| `no_jira_sites` | 404 | `/jira/callback` | Atlassian account has zero accessible sites |
| `jira_projects_unavailable` | 404 | `GET /jira/projects` | Jira project list call failed |
| `jira_search_failed` | 502 | ticket sync (background) | Jira ticket search call failed |
| `jira_rate_limited` | 502 | any Jira call | Jira returned 429 |
| `github_rate_limited` | 502 | any GitHub call | GitHub rate limit exhausted (`X-RateLimit-Remaining: 0`) |
| `external_service_error` | 502 | any integration | generic upstream (GitHub/Jira/Gemini) failure |
| `webhook_rejected` | 401 | `POST /webhooks/github` | unrecognized payload / unknown repo (not user-facing) |
| `webhook_bad_signature` | 401 | `POST /webhooks/github` | HMAC mismatch (not user-facing) |
| `graph_not_ready` | 404 | `/query/ask`, `/query/flow` | repo connected but graph has no nodes yet (parse not done) |
| `node_not_found` | 404 | `POST /query/ask` | `focused_node_id` doesn't exist in the current graph |
| `llm_bad_output` | 502 | `/query/ask`, `/query/flow` | Gemini response wasn't valid JSON |
| `gemini_unavailable` | 502 | (gemini client internal) | Gemini call failed/timed out after retry |

---

## 13. Environment variables the frontend cares about

The frontend doesn't read backend env vars directly, but two of them define contracts the frontend must match:

- **`FRONTEND_ORIGIN`** (backend config) — must exactly match your frontend's deployed origin. This is both the CORS allow-list (locked to exactly this one origin — `*` is never used, because GitHub/Jira tokens flow through this API) **and** the redirect target after both OAuth callbacks. Coordinate with backend before changing your frontend's URL.
- **`APP_BASE_URL`** (backend config) — the backend's own public URL, used to build the OAuth `redirect_uri` values registered with GitHub/Atlassian. Not directly consumed by the frontend, but if OAuth callbacks 404 or mismatch, this is usually why.

CORS is configured with `allow_credentials: true`, so any `fetch`/`axios` calls that need the refresh cookie (i.e. `/auth/refresh`, `/auth/logout`) must be made with `credentials: "include"`.

---

## 14. Things that are deliberately simplified

Worth knowing so you don't build UI assuming more sophistication than exists yet (all called out explicitly in the backend plan, not accidental):

1. **Graph nodes = top-level directories, not real services.** A `utils/` folder gets its own node exactly like `api/` does. Fine for a demo, not a real service-boundary detector.
2. **External packages are one node.** You can't currently distinguish "depends on `express`" from "depends on `lodash`" in the graph — both are just edges into a single `external dependencies` node.
3. **Jira: first accessible site only.** If a user's Atlassian account has access to multiple Jira sites, only the first one returned by Atlassian is used — there's no site picker.
4. **Tickets sync on a 5-minute poll, not real-time.** No Jira webhook integration. If you need "just synced" freshness, there's currently no manual re-sync endpoint — that would need to be requested from backend.
5. **`/onboarding/status` reports only the most-recently-connected repo/project**, not a full list, even though a user can connect multiple repos (`/github/repos/connected` has the full list).
6. **No email/password auth.** GitHub is the sole identity provider; there's no account-linking flow if a user wants to switch GitHub accounts.
