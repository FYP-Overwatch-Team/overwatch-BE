# Knowledge Graph — Implementation Plan

Status: **proposed, not started** · Rewritten 2026-09-20 (supersedes the first draft)

Performance numbers here were measured on real repos on 2026-09-19; the method
is in §5.

---

## 1. Goal and non-goals

**Goal.** Build a knowledge graph of an entire repository, derived from the
code itself: modules, files, classes, functions, methods, HTTP routes and
external packages, and the relationships between them (imports, calls, React
renders, inheritance, route handling).

**Non-goals for this plan.** Blast radius / T-GNN / co-change history · ticket
embeddings · the contract checker itself · frontend drill-down UI · linking
across repositories · languages beyond Python and JS/TS.

**Why now.** Ticket intelligence and the contract checker both need
function-level structure. Neither can be built on today's folder-level graph.

## 2. Principles

1. **Derived, never guessed.** Every node and edge traces back to a syntax
   node. Unresolved references are counted and reported, never name-matched
   into an edge.
2. **Stages are pure and separable.** Only *fetch* and *store* touch the
   outside world. Extract, link and build are pure functions over typed data,
   so they are testable without a database and cheap to re-run.
3. **Least privilege, least retention.** Tokens never touch disk. Source code
   is a liability: keep only the facts we need, for as long as the repo is
   connected.
4. **Fail soft, report honestly.** One unparsable file must not fail an index.
   Coverage is surfaced in `graph_stats`, not hidden.
5. **Deterministic.** Same commit in, same graph out, byte for byte. This is
   what makes snapshot tests and delta writes possible.

## 3. Security model

We run a parser over other people's private source code, hold tokens that can
read those repos, and send derived text to a third-party LLM. The threats that
follow from that:

| # | Threat | Mitigation | Where |
|---|---|---|---|
| S1 | **GitHub token leaks from disk.** Today `git clone https://x-access-token:TOKEN@…` persists the token in `.git/config`, and `repos/` is mode 755 | Never put credentials in the URL or argv. Set `origin` to a clean URL and supply the token per command through `GIT_ASKPASS` + env (`/proc/pid/environ` is owner-only, unlike argv). Scrub existing configs on boot. `chmod 700` the repos dir; set the process umask | PR 0 |
| S2 | **Token leaks via process list.** Credentials in argv are visible to any local user | Same as S1: no secrets in argv, ever | PR 0 |
| S3 | **Symlink escape.** A repo can ship `link → /etc/passwd` or `→ ~/.ssh`, and the parser would read outside the checkout | Skip symlinks in discovery; assert every resolved path is inside the checkout root | PR 1 |
| S4 | **Path traversal from webhook input.** Today a file path from the payload is joined to the workdir | Incremental updates stop trusting payload paths entirely; they diff the checkout by content hash. Any path that still arrives from outside is validated against the root | PR 1, PR 7 |
| S5 | **Resource exhaustion / malicious inputs.** Huge repos, 100 MB minified bundles, deeply nested files, pathological parses | Caps at every level: file size, file count, total bytes, per-file parse timeout, per-worker address-space limit. Parsing runs in separate processes, so a hang or crash kills a worker, not the API | PR 2 |
| S6 | **Cypher injection.** `neighborhood()` interpolates `hops` into the query string with `%` | Parameters only. The one value that cannot be a parameter (path depth) is validated against an allow-list of 1–3 | PR 0 |
| S7 | **Cross-tenant graph access.** Node ids embed `repo_full_name`, so a crafted id could reach another repo's subgraph | One authorization dependency for every graph route: the caller must have that repo connected. Every query is filtered by `repo`, and a node's `repo` property must match the authorized repo | PR 8 |
| S8 | **Secrets inside the code we store.** Hardcoded keys can appear in signatures, default arguments and docstrings | Store bounded text (truncate docstrings), and run a secret-pattern redaction pass before anything is stored or sent to the LLM | PR 3, PR 9 |
| S9 | **Prompt injection from source code.** A comment can say "ignore previous instructions" | Code-derived text is data, never instruction: it goes in a clearly delimited block. The existing invariant stays — every id the model returns is validated against the live graph, and anything else is dropped | PR 9 |
| S10 | **Third-party exposure.** Grounding sends derived code facts to Gemini | Send the smallest subgraph that answers the question, never raw file contents; document it; keep a kill switch (`LLM_GROUNDING_ENABLED`) | PR 9 |
| S11 | **Data remains after disconnect.** There is no disconnect path today, so checkouts, facts and graph nodes outlive the connection | Add repo disconnect that removes the checkout, the facts and the graph, and delete the webhook from GitHub | PR 7 |
| S12 | **Supply chain.** tree-sitter grammars are downloaded at runtime on first use | Pin versions and prefetch them into the Docker image; the pack verifies checksums. Production never downloads at run time | PR 2 |
| S13 | **Log leakage.** Source lines or tokens in logs | Existing redaction stays; the pipeline logs counts, paths and durations — never file contents or code text | all |

## 4. Architecture

Dependencies point inward. The inner package imports no FastAPI, no Motor and
no Neo4j.

```
   api/routes        transport: auth, validation, serialisation
        │
   services/         orchestration: jobs, authorization, statuses
        │
   knowledge_graph/  pure domain: discover → extract → link → build
        │
   ports (Protocols) SourceProvider · FactsStore · GraphStore
        │
   db/ + integrations adapters: Mongo, Neo4j, GitHub
```

Ports are Protocols with in-memory fakes, matching the existing
`get_x()` / `use_x()` injection style, so every stage is unit-testable without
containers.

```
app/knowledge_graph/
  facts.py                frozen dataclasses: FileFacts, Definition, ImportRef,
                          CallSite, RenderSite, RouteDef, HttpCall
  limits.py               every cap in one place
  discovery.py            walk, ignore rules, symlink and containment checks,
                          language detection
  project_config.py       tsconfig paths/baseUrl, package.json, Python roots
  extract/
    extractor.py          run a language's queries → FileFacts (pure)
    pool.py               process pool: timeout + memory cap per file
    languages/base.py     LanguageSpec protocol
    languages/python.py   + queries/python.scm
    languages/javascript.py + queries/{javascript,typescript,tsx}.scm
  link/
    imports.py            specifier → file | package
    symbols.py            symbol table; call / render / inheritance binding
    http.py               route ↔ client-call matching (PR 10)
    report.py             resolution coverage stats
  build/
    ids.py                the only place ids are formatted
    builder.py            facts + links → GraphSnapshot
    projection.py         file graph → module view (what the dashboard shows)
    diff.py               snapshot vs stored → GraphDelta
  pipeline.py             stage orchestration; no I/O of its own
app/services/
  source_service.py       checkout lifecycle (replaces the clone half of repo_service)
  graph_repository.py     Neo4j adapter + in-memory fake
  graph_query_service.py  drill-down, neighbours, search, stats
  facts_repository.py     Mongo facts cache
app/workers/jobs.py       index_repo / sync_repo; locking; coalescing
```

Adding a language = one `LanguageSpec` + one `.scm` file. No other file changes.

## 5. Performance and scale

**Measured** (2026-09-19, 8-thread laptop; tree-sitter with an approximation of
the planned queries on real repos; Neo4j 5.26 Community with a synthetic graph
of the same size and shape; call edges assume ~30% of distinct call pairs
resolve — an assumption until PR 4 exists):

| Repo | Files · LOC | Parse 1 core / 8 | Facts JSON | Graph | Neo4j write | Neo4j disk |
|---|---|---|---|---|---|---|
| overwatch-fyp | 97 · 5.7k | 0.15 s | 0.1 MB | ~350 · ~800 | < 1 s* | < 1 MB* |
| FastAPI | 1,142 · 115k | 0.9 s | 1.3 MB | ~7k · ~13k | ~3 s* | ~5 MB* |
| Django | 2,977 · 536k | 5.7 s / 1.5 s | 11 MB | 47k · 96k | 23.5 s | 40 MB |
| VS Code | 13,283 · 3.9M | 36.6 s / 11.9 s | 76 MB | 185k · 551k | 84 s | ~195 MB |

\* extrapolated from the measured rate.

Reads at VS Code scale (p50/p95): 1-hop neighbours 10/18 ms · 2-hop 16/21 ms ·
callers of a symbol 7/10 ms · name search 9/13 ms. Hashing every file: 0.07 s
(Django), 0.42 s (VS Code). Holding all facts in memory: 96 MB / 449 MB.

**Conclusions that shape the design**

- Parsing is cheap (~100k LOC/s/core); **the Neo4j write dominates**. So: first
  index uses `CREATE` into a clean version, every later run writes only a delta.
- Memory is the real ceiling on small instances: 449 MB of facts for a 3.9M-line
  repo would not fit a 512 MB host. Workers and batch sizes are derived from
  available CPU and RAM, never hardcoded.
- A symbol-level graph cannot be sent to an LLM (Django alone has ~44k symbols).
  Grounding must retrieve a subgraph — which the numbers say costs ~16 ms.

**Budgets** (enforced in CI where practical):

| Path | Budget |
|---|---|
| First index, ≤ 150k LOC, 2 cores | < 60 s end to end (excluding clone) |
| Push → graph updated, small push | < 10 s |
| `GET /graph` (module view) | p95 < 200 ms |
| Drill-down / neighbours / search | p95 < 300 ms |
| Webhook response | < 1 s, always (work is queued, never inline) |

**Caps and graceful degradation** (`limits.py`): max file size 512 KB · max
files 20,000 · max total source 200 MB · per-file parse timeout 5 s · worker
address space capped · max symbols per repo 200,000. Above the symbol cap the
repo is indexed at file level only, with a reason recorded in `graph_stats` and
shown in the UI, rather than failing or silently truncating.

## 6. Data model

### Neo4j

Every node: `repo`, `version` (commit SHA). Every edge: `repo`, `origin_file`
(the file whose code produced it — what makes delta updates possible).

| Label | Id | Key properties |
|---|---|---|
| `Repository` | `owner/name` | default_branch, commit_sha, indexed_at |
| `Module` | `owner/name:api` *(unchanged from today)* | name, path, file_count, symbol_count |
| `File` | `owner/name:api/users.py` | path, language, loc, content_hash |
| `Symbol` | `owner/name:api/users.py#UserService.get` | name, qualified_name, kind, start_line, end_line, signature, exported, docstring (truncated, redacted) |
| `Route` | `owner/name:route:GET /users/{id}` | method, path, framework, file_path, line |
| `Package` | `owner/name:pkg:npm/stripe` | name, ecosystem |

Edges: `CONTAINS` (Repository→Module→File) · `DEFINES` (File→Symbol) ·
`HAS_MEMBER` (class→method) · `IMPORTS` (File→File) · `USES_PACKAGE`
(File→Package) · `CALLS` (File|Symbol→Symbol) · `RENDERS` · `EXTENDS` /
`IMPLEMENTS` · `HANDLES` (Symbol→Route) · `REQUESTS` (→Route) · `DEPENDS_ON`
(Module→Module, weighted — the materialised view the diagram reads).

Constraints: unique `id` per label. Indexes: `repo`, `Symbol.name`.

Id rules (in `build/ids.py`, nowhere else): path-derived and stable across
re-parses; duplicate names in a file get an ordinal (`#helper@2`); anonymous
default exports use `#default`.

### MongoDB

- `file_facts` (replaces `file_parses`): `{repo_full_name, path, content_hash,
  parser_version, language, facts}`, unique on (repo, path). This is the cache
  that makes incremental cheap and re-linking free.
- `repos` gains `graph_version`, `indexed_at`, `sync_requested_at` and
  `graph_stats` (files parsed/skipped, symbols, imports resolved/total, calls
  resolved/total, degraded-mode reason).

## 7. Pipeline

**Full index**

```
checkout @sha → discover → extract (pool, cached by hash) → link → build
              → write nodes/edges at `version = sha` → delete version ≠ sha → stats
```

**Incremental sync** (a push only *triggers* this; payload contents are not trusted)

```
fetch @sha → hash every file → re-extract only changed hashes → relink from cache
           → diff against stored snapshot → write only the delta
```

Correctness rule, enforced by a property test: any sequence of edits followed by
an incremental sync must produce exactly the same graph as a full index.

**Concurrency and failure**

- One in-flight job per repo, as today (`parse_status` claim).
- **Coalescing:** a push arriving mid-run sets `sync_requested_at` instead of
  being dropped, and one more sync runs at the end. Today such a push is lost.
- **Stale-lock recovery:** `in_progress` older than N minutes is reclaimable, so
  a restart mid-parse cannot wedge a repo forever.
- Per-file failures are collected into stats; the index still completes.
- Jobs stay behind a small interface so `BackgroundTasks` can be swapped for a
  real queue when there is more than one instance — no pipeline change needed.

## 8. API

| Endpoint | Notes |
|---|---|
| `GET /graph?repo_full_name=` | **Unchanged response shape** — the dashboard keeps working |
| `GET /graph/nodes/{id}` | Detail + children (module → files → symbols) |
| `GET /graph/nodes/{id}/neighbors` | `direction`, `edge_types`, `depth` (1–3, allow-listed) |
| `GET /graph/search` | Name search, paginated, capped |
| `GET /graph/stats` | Counts and resolution coverage |
| `DELETE /github/repos/{owner}/{name}` | Disconnect: remove webhook, checkout, facts and graph |

All of them go through one `require_connected_repo` dependency (S7). Responses
carry an ETag derived from `graph_version`, so the dashboard's polling is cheap.

## 9. LLM grounding rules

Unchanged intent, stricter rules: retrieve a bounded subgraph (never the whole
graph, never file contents); enforce a token budget before the call; wrap
code-derived text as untrusted data; validate every returned id against the live
graph and drop the rest; keep a kill switch.

## 10. Implementation steps

Each step is one reviewable PR that leaves `main` working.

| PR | Scope | Done when |
|---|---|---|
| **0** | ✅ **Done (2026-09-20).** Security and stability fixes to today's pipeline: token out of `.git/config` and argv (`app/integrations/git_cli.py`), boot-time scrub of existing checkouts, `chmod 700` repos dir, repository-name validation, symlink and traversal checks in discovery, `hops` allow-list, stale-lock recovery, coalesced resync, parsing moved off the event loop | Tests cover each fix; no token on disk in a fresh clone; API stays responsive during a parse |
| **1** | ✅ **Done (2026-09-20).** IR (`facts.py`), `limits.py`, discovery, project config | Unit tests incl. symlink escape, traversal, caps |
| **2** | ✅ **Done (2026-09-20).** Extractor + `.scm` queries (Python, JS/JSX, TS/TSX) + process pool with timeout and memory cap; grammar prefetch and git in the Docker image | Golden snapshots per fixture file; both local repos extract with no crash; a timed-out batch recycles its workers |
| **3** | ✅ **Done (2026-09-20).** Facts store (`FactsStore` port + Mongo and in-memory adapters), content-hash + parser-version cache, `sync_plan` | Re-running an index re-parses nothing; both adapters pass the same suite (redaction landed early, in PR 2) |
| **4** | ✅ **Done (2026-09-20).** Linker: imports (incl. tsconfig aliases), symbol table, calls, renders, inheritance, coverage report | **99.4% of internal imports resolve on `overwatch-fyp`** (was ~43%); 100% on this backend |
| **5** | ✅ **Done (2026-09-20).** Builder, ids, module projection, snapshot diffing | Snapshot tests; ids stable across re-parse; module ids unchanged from v1 |
| **6** | ✅ **Done (2026-09-20).** Neo4j adapter: constraints, batched transactional writes, delta writes, in-memory fake | Both implementations pass one suite, 8 of the tests against a real Neo4j 5; tenant-isolation and label-injection tests included |
| **7** | ✅ **Done (2026-09-20).** Pipeline + jobs: full index, incremental sync, stats on the repo document, disconnect/delete path, `KNOWLEDGE_GRAPH_V2` flag (coalescing and stale locks landed in PR 0) | Property test passes: incremental == full; disconnect leaves nothing behind, including the checkout |
| **8** | ✅ **Done (2026-09-20).** API: `/graph` served from the knowledge graph, plus node detail, neighbours, search, stats and edge-types; one authorisation dependency; bounded parameters; ETags | Cross-tenant access fails closed (id from another repository returns 404); every parameter bound-checked |
| **9** | ✅ **Done (2026-09-20).** Grounding on the new graph: retrieval by keyword, relation expansion, token budget, fenced untrusted context, validation via the context's own label map | Flow-ordering regression passes; invented names dropped; highlights reported as modules, references as symbols |
| **10** | ✅ **Done (2026-09-20).** HTTP layer: FastAPI/Flask decorators, Express handlers, Next.js route files, `fetch`/`axios`/`requests` calls, pattern matching | A TypeScript `fetch("/api/users/42")` links to a Python `@router.get("/api/users/{user_id}")` |
| **11** | ✅ **Done (2026-09-20).** Old path removed: `app/parsing/`, `graph_service`, `file_parses`, the feature flag and the v1 tests are gone; legacy `:Service` nodes purged at boot | One pipeline in the tree; 391 tests pass, including against a real Neo4j |

PR 0 is worth shipping this week regardless of when the rest starts.

## 11. Testing

- **Unit** per stage on small fixtures: extraction per language, each
  import-resolution rule, call binding, id formatting, caps, redaction.
- **Regression snapshots:** extend `tests/regression/fixtures/` with classes,
  methods, calls, JSX, aliases, cycles and routes; pin FileFacts and the graph.
- **Property:** incremental sync == full index over scripted edit sequences.
- **Integration:** Neo4j adapter and API against the CI service containers;
  cross-tenant authorization tests.
- **Security:** no-token-on-disk, symlink escape, traversal, Cypher parameter
  use, redaction.
- **Performance smoke:** index a generated ~1k-file repo in CI and assert the
  budgets in §5, so regressions show up as failures rather than surprises.

## 12. Rollout (completed)

Built behind `KNOWLEDGE_GRAPH_V2`, compared against the old pipeline on the
two local repositories, then switched over and the old path deleted (PR 11).
The flag no longer exists: there is one pipeline.

`/graph` kept its response shape throughout, so the dashboard was never
touched. Existing deployments drop their superseded `:Service` nodes on the
next boot.

**Rollback**, should it be needed, is `git revert` of the PR 11 commit —
the old pipeline is in history, not in the tree.

### Measured again after PR 2

With the real extractor (signatures, docstrings, qualified names, calls,
renders, inheritance) rather than the §5 benchmark approximation:
**56k LOC/s on one core, 134k LOC/s across 8 workers.** Richer output costs
roughly half the throughput of the shallow benchmark, which still puts a
500k-line repository at a few seconds. The Neo4j write remains the bottleneck.

### Resolution measured after PR 4

| Repository | Internal imports resolved | Calls accounted for |
|---|---|---|
| `supfaizan/overwatch-fyp` (97 files) | **99.4%** (172 files, 115 packages, 1 unresolved) | 45.7% — 138 internal + 98 into packages, of 516 |
| this backend (114 files) | **100%** (233 files, 260 packages) | 41.1% — 850 internal + 387 into packages, of 3,010 |

Linking a repository this size takes 5–12 ms, so the whole-repository re-link
that incremental sync depends on is effectively free.

The calls that remain unaccounted are method calls on values whose type we do
not track (`payload.get()`, `res.json()`, `client.send()`). Resolving those
needs type inference; until then they are counted, never guessed. Note that
`calls_resolved` and `calls_to_packages` are reported separately precisely so
this distinction stays visible rather than being flattened into one number.

### End-to-end measurement after PR 6

`supfaizan/overwatch-fyp` (97 files), real Neo4j 5, this laptop:

| Stage | Time |
|---|---|
| Discover + extract | 0.16 s |
| Link + build snapshot | 0.01 s |
| First write (363 nodes, 844 edges) | 2.75 s |
| Incremental write after one file changed | **85 ms** (2 nodes) |

The delta is what makes the second number possible: the same change written in
full would repeat the 2.75 s. The module view now shows real internal
dependencies (`app → features ×9`, `components → features`) that the alias bug
previously hid inside the external node.

### Pipeline measured after PR 7

`supfaizan/overwatch-fyp` (97 files) through the whole pipeline into a real
Neo4j 5, including reading from disk:

| Run | Time | What was written |
|---|---|---|
| First index | 4.85 s | 363 nodes, 844 edges |
| Sync, nothing changed | 0.08 s | 1 node (the repository's commit stamp) |
| Sync, one file edited | 0.20 s | 2 nodes |
| Sync, edit reverted | 0.07 s | 2 nodes |

A push therefore costs a fifth of a second on this repository, against nearly
five seconds to rebuild from scratch.

### Design notes from PR 2

- The promised "one LanguageSpec + one .scm file" landed as query fragments
  **composed** per dialect (`js_core.scm` + `ts_extra.scm` + `jsx.scm`), since
  TypeScript and TSX differ only in which fragments apply. Queries say which
  nodes matter (evaluated in C); the spec says what they mean (names, scopes,
  signatures), which queries cannot express.
- Secret redaction moved earlier, into extraction, so credential-shaped text
  never enters the cache at all.
- The Docker image was missing `git` entirely — indexing would have failed on
  first deploy. Added alongside the grammar prefetch.

## 13. Open decisions

1. **Languages this round:** Python + JS/JSX/TS/TSX (covers both local test
   repos and this backend). Anything else waits.
2. **External packages:** one `Package` node each, with the dashboard still
   collapsing them into one box — or keep a single collapsed node as today.
3. **HTTP layer (PR 10):** in this round, or after the contract checker starts?
4. **Checkout retention:** keep checkouts between pushes (fast incremental
   fetch, source on disk) or delete after indexing and re-fetch on demand
   (less data at rest, slower syncs).
