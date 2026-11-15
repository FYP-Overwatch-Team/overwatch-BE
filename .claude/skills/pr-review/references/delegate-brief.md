# Delegating one PR to a background agent

Use this with `--delegate`, or whenever you want a PR reviewed while you keep working with the user.

- **How to launch:** spawn a general-purpose Agent with `run_in_background: true`.
  - Fill in every `<...>` below, and paste in the validation block from frontend.md or backend.md for the PR's repo.
  - Replace the background line with anything specific you already know: bug history, suspected paths, races to check. If you know nothing, delete it.
  - Never send two agents into the same worktree.
- **When it reports:**
  1. spot-check its most consequential claim yourself;
  2. confirm CI on the head commit (BE only; the FE has no CI yet);
  3. then relay the verdict in the standard format.

```
Review <owner/repo> PR #<n> ("<title>", branch <headRefName>, author <author>, head <short sha>) and decide whether it is good to merge. The user merges PRs themselves; you only review and report. Do NOT push, comment on GitHub, edit the PR, or change anything outside the review worktree.

Where to work: the review worktree <absolute worktree path>. Check `git status` is clean (stop and report if not), then `git fetch origin <headRefName> main` and `git checkout -B review-<n> origin/<headRefName>`. Never touch the user's primary checkout or other worktrees. Never use git stash. No destructive git operations. Do not edit .env or secrets; never print tokens or connection strings.

Steps:
1. `gh pr view <n> --repo <owner/repo>`, `gh pr diff <n> --repo <owner/repo>`, `gh pr checks <n> --repo <owner/repo>`.
2. Read all three comment endpoints: `gh api repos/<owner/repo>/pulls/<n>/comments`, `.../issues/<n>/comments`, `.../pulls/<n>/reviews`. Read the full Copilot review BODY too; Copilot hides some findings under "Suppressed comments" there. Check whether each finding was addressed.
3. Spec: <Jira key and how to read it | linked issue #<i> via `gh issue view` | the PR body is the only spec>. Check the PR covers every acceptance criterion.
4. Merge main locally to validate the post-merge state: `git merge origin/main --no-edit` (never pushed). Resolve only trivial conflicts and say exactly what you did; otherwise report the conflicts.
5. Review the PR's own diff (`git diff origin/main...origin/<headRefName>`) for correctness. Read <skill-dir>/references/scrutiny.md and <skill-dir>/references/<frontend|backend>.md first and check the items that fit. Background (verify it, it may be outdated): <background or delete this line>.
6. Validate in the worktree, ONE command at a time:
   <paste the repo's validation commands>
   - Prove regression tests: revert the fix locally, run them, confirm they fail, restore.
   - If dependencies aren't installed in the worktree, install them there (pnpm install --frozen-lockfile / uv sync --locked), never in the user's checkout.
7. Clean up: leave the worktree on review-<n> with no temp files.

Report back: start with a one-line verdict (good to merge, or not yet and why). Then exactly three headings:
- **Merge blockers**: anything that must change first (bugs, failing or missing tests for the fix, unaddressed valid review findings, CI failures, conflicts).
- **Deploy constraints**: env vars, data migrations or backfills, BE/FE deploy order, OAuth or webhook URL changes, or "None". Remember a BE merge to main deploys to prod.
- **Notes**: non-blocking observations, AC coverage, and the exact validation commands you ran with their results.
Use file:line references. Keep it factual and concise. Don't use em-dashes. Say clearly what you could not verify.
```

Replace `<skill-dir>` with this skill's base directory, since the agent doesn't inherit the variable.
