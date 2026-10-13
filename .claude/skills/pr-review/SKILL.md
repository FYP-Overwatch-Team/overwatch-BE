---
name: pr-review
description: "Review an open pull request in the Overwatch repos (frontend overwatch-fyp, backend overwatch-BE) against its ticket or PR body and give a merge verdict under exactly three headings: Merge blockers, Deploy constraints, Notes. Works in a separate review worktree, merges main in locally, proves suspected bugs with failing tests, and pushes only when the user says so. Use it whenever the user runs /pr-review, asks to review a PR or 'the next PR', asks whether a PR can be approved or merged, wants a PR checked against its ticket, or wants to work through the open PR backlog oldest first, even when they only give a PR number."
argument-hint: "[fe|be] [pr-number] [--delegate]"
---

# PR review

Arguments: `$ARGUMENTS`

The user merges PRs themselves. Your job is a verdict they can act on without re-checking your work, plus fixes and pushes only when they ask. Scripts live in `${CLAUDE_SKILL_DIR}/scripts` (each prints usage with `--help`); deeper guidance lives in `${CLAUDE_SKILL_DIR}/references`, named below where it applies.

This skill is identical in both repos. Work out which one you're in with `git remote get-url origin`; the other is expected as a sibling checkout.

## Before you start

- **GitHub:** `gh` must be signed in with access to both repos.
- **Tickets:** optional, see step 2.3.
- **The other repo:** expected beside this one (`../overwatch-fyp` or `../overwatch-BE`). If it's missing, tell the user and offer to clone it.

## 1. Work out what to review

Paths are relative to the folder that holds both checkouts.

| Alias              | GitHub repo             | Checkout       | Review worktree        | Read                   |
| ------------------ | ----------------------- | -------------- | ---------------------- | ---------------------- |
| frontend, fe       | supfaizan/overwatch-fyp | overwatch-fyp  | overwatch-fyp-review   | references/frontend.md |
| backend, be        | rizwan521/overwatch-BE  | overwatch-BE   | overwatch-BE-review    | references/backend.md  |

- **Repo and number**: review that PR.
- **Repo only**: list its open PRs oldest first and start on the oldest that isn't a draft. Show the list in a few lines, marking drafts and stacks (a base that isn't main):
  `gh pr list --repo <repo> --state open --limit 50 --json number,title,author,createdAt,headRefName,baseRefName,isDraft --jq 'sort_by(.createdAt)[] | "#\(.number) \(.createdAt[0:10]) \(.author.login) \(.headRefName) -> \(.baseRefName) draft=\(.isDraft) \(.title)"'`
- **Number only**: find which repo has an open PR with that number; ask if both do.
- **Nothing**: summarise open PRs per repo (count and oldest) and ask where to start.
- **`--delegate`**: hand the PR to a background agent using references/delegate-brief.md, so you stay free for the user. When it reports, spot-check its riskiest claim yourself before relaying the verdict.
- **Dependency bumps** (Dependabot, "chore(deps)", lockfile-only PRs): also read references/dependency-bumps.md.

## 2. Read what is already known

Do this before reading code, because the review is against the spec and against what reviewers already said.

1. **Metadata and CI**: `gh pr view <n> --repo <repo> --json title,body,author,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,reviewDecision,commits,statusCheckRollup` and `gh pr checks <n> --repo <repo>`. The FE has no CI yet, so "no checks" there is expected, not green.
2. **All three comment endpoints.** Copilot often posts no inline comments and puts real findings in its review body under "Suppressed comments". An empty inline list never means there are no comments.
   - `gh api repos/<repo>/pulls/<n>/comments --jq '.[] | "[\(.user.login)] \(.path):\(.line // .original_line)\n\(.body)\n---"'`
   - `gh api repos/<repo>/issues/<n>/comments --jq '.[] | "[\(.user.login)]\n\(.body)\n---"'`
   - `gh api repos/<repo>/pulls/<n>/reviews --jq '.[] | "[\(.user.login) \(.state) \(.commit_id[0:8])]\n\(.body)\n---"'`

   For each finding, settle whether it is valid and whether it is addressed. A finding can also be stale: a later commit or main changed the code under it.
3. **The spec.** Use the first that exists:
   - **Jira**, if an Atlassian MCP server is connected and a ticket key (e.g. `OW-123`) is in the branch, title or body. Find the tool with ToolSearch (`jira issue`), get the cloudId from the accessible-resources tool, and read summary, description, status, comments and links as markdown. For a stacked PR, read every ticket in the stack.
   - **A linked GitHub issue** (`Closes #12`): `gh issue view <n> --repo <repo> --comments`.
   - **The PR body.** Say in Notes that it was the only spec.

   Where the spec and current main disagree, review against main and flag the spec as stale. If the MCP isn't connected but a key is present, say in Notes that the ticket wasn't checked.
4. **Related PRs**: look for an open PR in the other repo for the same work (same ticket key, branch name or feature). Review BE before FE and put their deploy order under Deploy constraints.

## 3. Set up the worktree and merge main locally

Never review in the user's own checkout. It usually holds their work in progress.

- In the review worktree, `git status --short` must be empty. If it isn't, stop and tell the user rather than cleaning it. Then `git fetch origin` and `git checkout -B review-<n> origin/<headRefName>`.
- Never use `git stash`: the stash list is shared with the user's checkouts.
- If the worktree is missing, create it with `git -C <checkout> worktree add ../<worktree> origin/main`, then install as the repo reference describes.
- If the branch is behind main, merge main locally so you review what will actually ship: `git merge origin/main --no-edit`. It stays local until the user says push.
- Before resolving anything beyond a trivial conflict, read references/merging.md. Check the files git merged on its own as well as the conflicted ones: clean auto-merges can drop a new argument or duplicate a test.

## 4. Review the PR's own change

- Read the PR's own delta, not the merge result: `git diff origin/main...origin/<headRefName>` (three dots), or against the base branch for a stacked PR.
- Read whole files where a hunk alone doesn't show the behaviour.
- Map every acceptance criterion to code and to a test.
- Look for scope drift in both directions. Quantify it ("40 of 600 added lines are unrelated restyling") rather than asserting it, and check the author's reasoning before calling it creep.
- Check the repo's own conventions: `AGENTS.md` in the FE; the existing layering (`api/routes` → `services` → `db`/`integrations`) in the BE.
- Go through references/scrutiny.md, the patterns most likely to produce real blockers in this stack.
- For a refactor that should not change behaviour, prove it with references/refactors.md rather than by reading.
- If the user says the author is junior, look harder at edge cases, tests and scope.

Prove a claim before you report it, because a wrong blocker costs the user a round trip with the author.

- A suspected bug gets a failing test first. If the bug is real, keep it as the regression test.
- A fix's tests must fail with the fix reverted: revert locally, run, restore.
- Mongo or Neo4j behaviour the fakes can't model (unique indexes, aggregation, transactions, Cypher) gets checked against a real local instance (`docker compose up` in the BE).
- Never query prod data unless the user opens access, and then read-only, counts only, never printing a connection string.
- Mark each finding as introduced by this PR or pre-existing. Pre-existing problems are Notes and follow-ups, not blockers.

## 5. Validate

Run the checks in the repo's reference file, one test process at a time. Compare lint warnings against main instead of reading absolute numbers, and put the real counts in the report.

## 6. Report

Start with a one-line verdict: "Good to merge." or "Not yet:" and the reason. Then exactly these three headings:

```
## Merge blockers
## Deploy constraints
## Notes
```

- **Merge blockers**: things that are broken and must change in this PR. That covers bugs and regressions, a fix with no test (or a test that passes without the fix), valid unaddressed review findings, red CI and unresolved conflicts. Nothing else goes here. Write "None." when empty.
- **Deploy constraints**: the code is right, but the rollout needs care. Examples: new env vars, data migrations or backfills, BE/FE deploy order, OAuth callback or webhook URL changes, post-deploy checks. A BE merge to main deploys to prod automatically, so these apply the moment it merges. Write "None." when empty.
- **Notes**: should-fixes, gaps, acceptance-criteria coverage, spec drift, and each review comment with whether it's addressed. Also pre-existing issues found along the way, nits, suspicions you checked and ruled out, anything you could not verify, and the validation you ran (commands, test counts, lint deltas).

Keep the lines sharp. A missing nice-to-have is a should-fix, not a blocker. A deploy-order coupling with another PR is a deploy constraint, not a blocker.

Use file:line links and plain factual sentences, with no em-dashes. End with what happens next: the fixes you propose (with a recommendation when there's a real choice), merge order, and the next PR.

## 7. Fix, push and watch CI, when the user says so

Pushing to an author's branch can dismiss approvals and collide with an author who is still pushing. So propose first, and push only on the user's explicit word in that message ("push", "fix it").

- Make each fix with a regression test that is red before the fix and green after.
- Commit with a conventional message and no narrative code comments. No AI attribution anywhere (no `Co-Authored-By` trailer, no mention of Claude, Codex or any other tool in commits, PR text or comments); see AGENTS.md.
- Before pushing, check the author hasn't pushed since you checked out: `git fetch origin <headRefName>`, then `git merge-base --is-ancestor origin/<headRefName> HEAD`. If that fails, stop and tell the user (references/merging.md covers rebuilding on their latest).
- Push with `git push origin HEAD:<headRefName>`. Never use `--no-verify`; the BE pre-push hook runs the full pytest suite and must pass.
- Watch CI in the background with `gh pr checks <n> --repo <repo> --watch --interval 30`. It can report "no checks" before GitHub registers the run, so check again. Never report a run that started before your push.

## 8. Record and move on

- **Close the report with what should outlive the PR:**
  - should-fixes and pre-existing bugs worth a ticket (they don't go into the PR);
  - deploy-time actions such as env vars on the EC2 host, backfills and post-deploy checks, phrased so someone can act on them.

  If you keep persistent notes (auto-memory), record them there too.
- **GitHub and Jira:** don't comment on GitHub or edit tickets unless asked.
- **Next PR:** on "Merged" or "on to N", take the next oldest PR, or the other repo's counterpart. When a stack's base merges, merge main into the next PR in the stack before reviewing it.
- **Worktrees:** leave them in place unless the user asks you to remove them.

## Ground rules

- **Secrets:** never edit `.env` or other secrets, never print a database URI, token or key.
- **Prod** (EC2 host, Atlas, Aura) is strictly read-only unless the user explicitly asks for a write.
- **No destructive git on branches you don't own:** no reset --hard, no force push, no checkout over the user's files.
- **Temporary files** go in your scratchpad, not in the worktree.

## Scripts

Run them from inside the relevant worktree with `node`. A spec argument is `<git-ref>:<path>`, `WORKTREE:<path>` or a plain path. The line-based scripts work in both repos; the AST ones parse TypeScript, so they're FE-only and need its node_modules (for Python, see references/refactors.md).

| Script              | Repo | Use it to                                                                                                                            |
| ------------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------ |
| preserved-lines.cjs | both | Find lines a change added that a merge lost                                                                                          |
| three-way-lines.cjs | both | Audit line counts in a merge (LOST and EXTRA lines)                                                                                  |
| moved-lines.cjs     | both | Find edits that need porting into files the other side created by moving code                                                        |
| take-side.cjs       | both | Resolve every hunk in a file to ours or theirs, keeping clean auto-merges                                                            |
| ast-compare.cjs     | fe   | Compare named functions, components or consts between two versions, ignoring formatting and comments; `--list` and `--print` to explore |
| verify-move.cjs     | fe   | Prove declarations removed from a file reappear unchanged where they moved                                                           |
| split-check.cjs     | fe   | Check a function split into several kept its statements, and show the new glue                                                       |
| test-diff.cjs       | fe   | Show added, removed, changed, moved and duplicated Vitest tests across spec files                                                    |
