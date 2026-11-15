# Where real blockers hide in this stack

Check the items that fit the change. When a PR here turns up a new class of blocker, add it with the PR number so the list stays grounded.

## Drift between PRs and repos

- **A field, endpoint or `error_code` that main removed or renamed is still used.** FE types that keep the field optional hide it from typecheck, so it shows up as a silent `undefined` or a 404. After merging main in, grep the branch for everything main removed or renamed, and grep the other repo too.
- **A clean auto-merge dropped an argument.** The branch added an optional parameter; a call site main added after the fork doesn't pass it, so the default silently switches the feature off. Check every call site of every function whose signature the PR changed, including ones main added.
- **BE contract changed without the FE guide.** A new or changed route, field or `error_code` needs `docs/FRONTEND_GUIDE.md` updated in the same PR.

## Things tests and types can't see

- **Missing `user_id` scoping.** Every Mongo read and write on per-user data filters by the caller's `user_id`, including in services and jobs called from routes. A route that takes `repo_full_name` from the client and looks it up without `user_id` is an access-control bug.
- **Uniqueness that isn't enforced.** No Mongo index is created anywhere, and mongomock won't complain. "Find then insert" or "check then update" is only safe with a unique index or a single atomic `update_one(..., upsert=True)` / `find_one_and_update`.
- **Cypher that never ran.** Tests use the in-memory graph repo. Check `MERGE` keys (node ids are `<repo_full_name>:<group>`), that deletes are scoped by `repo`, and that a re-parse can't leave stale edges or nodes.
- **New settings.** A new field in `config.py` needs `.env.example`, a prod value on EC2, and a safe default. A secret with a usable dev default is a blocker.
- **Wrong env or URL.** OAuth redirect URIs, webhook URLs and `FRONTEND_ORIGIN` built from the wrong setting work locally and break in prod.
- **FE server/client boundary.** A secret or server-only import reachable from a `'use client'` module ships to the browser. `pnpm build` catches some of this; reading the import graph catches the rest.

## Security-sensitive flows

- **Tokens:** generated with `secrets.token_urlsafe` (or equivalent entropy), hashed at rest when they're bearer credentials, Fernet-encrypted when we must read them back (GitHub, Jira). Single use means one atomic conditional update, consumed before the side effect.
- **Refresh rotation:** reusing an old refresh token must fail, and two concurrent refreshes must not both succeed.
- **OAuth `state`:** generated per flow, checked on callback, single use. A missing check is CSRF on account linking.
- **Webhooks:** HMAC over the raw body with `hmac.compare_digest`, checked before any parsing or side effect; per-repo secret; delivery dedup.
- **CORS:** stays locked to `frontend_origin`. Never `*` with credentials.
- **Logs:** no tokens, secrets or Authorization headers. New sensitive key names must be added to `SENSITIVE_KEYS`.
- **"Not found" paths:** don't reveal whether another user's repo or project exists (404 for both missing and not-yours).
- **LLM input:** repo content and ticket text going into Gemini prompts is untrusted. Grounding rules must hold even if the content contains instructions.

## Concurrency and background work

- **Read-validate-write races.** A rule checked against a read made before an unconditional write breaks under concurrent requests. Look for a conditional update instead.
- **Overlapping jobs.** Two webhooks or a webhook plus a retry can run `run_incremental_reparse` for the same repo at once. Check the PR doesn't assume one job at a time.
- **Lost work.** `BackgroundTasks` and the polling loop die with the process. A status set to "parsing" before a crash stays stuck; check there's a way out (retry endpoint, timeout).
- **The polling loop** swallows exceptions per iteration; a bug there fails silently every five minutes. Check it logs enough to notice.

## Silent failure

Ask what fails quietly, not just what fails loudly.

- **`??` doesn't catch `''`** in TS, and `or` does catch `0`/`""`/`[]` in Python. Both skip or trigger fallbacks unexpectedly.
- **Swallowed exceptions** (`except Exception: pass`, a catch that returns `[]`) turn outages into empty UIs.
- **Pagination:** GitHub and Jira list APIs page. Code that reads only the first page silently drops repos, files or tickets.
- **tree-sitter:** unsupported file types or parse errors should be skipped and counted, not abort the whole parse, and not produce a graph that looks complete.
- **`tailwind-merge`** silently drops custom classes it doesn't know (theme shadows). Register them in `shared/lib/cn.ts`.

## Tests

- A fix whose test still passes with the fix reverted proves nothing.
- A test that only passes because of a fake (mongomock, in-memory graph, `FakeGeminiClient`, respx) proves the code against the fake. Say so when the behaviour depends on the real service.
- Regenerated regression snapshots need a reason; diff the snapshot JSON and check every change is intended.
- For moved or rewritten tests, count before and after and check the old behaviour still has a test (`scripts/test-diff.cjs` for FE Vitest specs; compare `uv run pytest --collect-only -q` output for BE).
