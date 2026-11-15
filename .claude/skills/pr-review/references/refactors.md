# Refactor PRs: proving "no functional change"

"It looks the same" isn't evidence; the checks below are. Script paths are relative to `${CLAUDE_SKILL_DIR}`, and commands run from the PR worktree.

## FE (TypeScript)

The AST scripts load TypeScript from the worktree's node_modules. They compare AST prints, ignoring comments, formatting and visibility modifiers. Never compare with brace-counting regexes; they stop at destructured parameters and report different code as identical.

1. **Moved declarations:** `node scripts/verify-move.cjs origin/main <old-file> <new-file>[,<new-file>] [--rename from=to]`.
   - Every removed declaration should come back MATCH. DIFFER prints a statement-level diff; GONE means it vanished.
   - It also lists declarations in the targets that weren't moved: either new code or a copy left behind in both places.
2. **Judge every DIFFER.** It's either a behaviour-identical swap or a real change to flag.
3. **Individual members:** `node scripts/ast-compare.cjs <from-spec> <to-spec> <name>[,...]`. Use `--config` with a JSON list of `{ name, from, to, renames }` when names or files differ per member, and `--list <spec>` to see what's available.
4. **A function or component split into several:** `node scripts/split-check.cjs <orig-spec> <name> <split-spec> <a,b,c>` reports which original statements survived verbatim and lists the new glue. Read the glue by hand.
5. **Moved tests:** `node scripts/test-diff.cjs --base origin/main:<old-spec>,origin/main:<new-spec> --head WORKTREE:<old-spec>,WORKTREE:<new-spec>`. Every removed test should reappear as moved or retitled.

## BE (Python)

No script; use the standard library from a scratch file, run with `uv run python <scratch>/compare.py`:

- Parse both versions (`git show <ref>:<path>`) with `ast.parse`, find each function or method by name, and compare `ast.dump(node, include_attributes=False)` with the docstring stripped. Equal dumps mean equal code modulo formatting and comments.
- For moved tests, compare `uv run pytest --collect-only -q` on main and on the branch, mapping old node ids to new.

## Wiring the compiler can't check

- **FE:** a component moved into `shared/` must not import from `features/`; a moved client component keeps its `'use client'` line; a moved module that used `server-only` keeps it. Re-exported names still resolve for every importer (`pnpm build`).
- **BE:** a moved route keeps its path, method, `response_model`, status code and `Depends(get_current_user_id)`; the router is still included in `create_app`. A moved service function keeps its `user_id` filter. Module-level singletons and `use_*` injection hooks (tests rely on them) still exist with the same names.

## Reporting

- Only real behaviour changes or broken wiring go under Merge blockers.
- List moved-but-different items with your judgement under Notes.
- Say exactly what you compared, for example "5 moved members: 3 MATCH, 2 DIFFER (both behaviour-neutral, explained below)".
