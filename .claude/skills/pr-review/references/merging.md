# Merging main in: stacks, conflicts and ports

Most conflicts come from squash merges. A stack's branches carry the original commits while main has one squashed commit, so git sees unrelated histories touching the same lines. The subtle mistakes then come from merges that look clean: a duplicated test, or a call site that silently loses a new argument.

Script paths are relative to `${CLAUDE_SKILL_DIR}`. The line-based scripts work in both repos.

## Squash divergence

- For files the branch didn't author, take main's version. Hand-merge only the branch's own files.
- When a stack's base PR has just been squash-merged, check whether main's version of each conflicted file equals the previous stack branch's version. If it does, the branch's side is right: `node scripts/take-side.cjs ours <file>` (keeps clean auto-merges) or `git checkout --ours <file>`.
- Cascade a stack downwards: after main is merged into the base, merge the base into the child, and so on.

## Expected-tree method (divergent stacks)

Build the tree you expect, then make the real merge equal it.

1. Get the PR's own delta: `git diff <previous-stack-point> origin/<head> > <scratch>/own.patch`. The previous point is the base PR's head, or the merge-base for a single PR.
2. Apply it to main in a scratch index:
   - `GIT_INDEX_FILE=<scratch>/exp.index git read-tree origin/main`
   - `GIT_INDEX_FILE=<scratch>/exp.index git apply --cached --3way <scratch>/own.patch`
   - `git apply` is all-or-nothing. Use `--exclude=<path>` for files main changed that the PR deletes, then `git rm --cached` those.
3. Port by hand anything main changed underneath. `GIT_INDEX_FILE=<scratch>/exp.index git write-tree` prints the expected tree, EXP.
4. Do the real `git merge origin/main`, resolve it (or `git checkout EXP -- <files>`), and confirm the result equals EXP: `git write-tree` before committing, `HEAD^{tree}` after.

## After any non-trivial merge

- **Inspect auto-merged files too.** `git diff --diff-filter=U` lists only the files git could not merge.
- **Duplicated or dropped tests:**
  - FE: `node scripts/test-diff.cjs --base origin/main:<spec> --head WORKTREE:<spec>`.
  - BE: compare `uv run pytest --collect-only -q` before and after; duplicated test names in one module silently shadow each other in Python.
- **Lost or duplicated lines:**
  - during the merge: `node scripts/three-way-lines.cjs $(git merge-base HEAD MERGE_HEAD) HEAD MERGE_HEAD`;
  - after committing: `<merge>^1 <merge>^2 --merged <merge>`.
- **Redo a botched resolution:** `git checkout -m -- <file>` restores the conflict markers.
- **Reindentation:** `git diff -w` separates it from real change. In Python, indentation is behaviour, so read those hunks anyway.
- **Before committing the merge,** run the repo's validation (frontend.md or backend.md).

## Code that moved

If main moved or deleted a file the PR edited, or the other way round, the edits have to be carried to the new location. Git won't tell you.

- `node scripts/moved-lines.cjs $(git merge-base HEAD MERGE_HEAD) HEAD MERGE_HEAD` groups edits made at the old location whose old text lives on in files the other side created. A cluster of hits for one file pair is a real move; one or two shared lines are usually boilerplate.
- Look for review fixes made earlier in the old location too.

## Two PRs reworking the same files

After resolving, prove both sides survived:

- For the PR's own lines: `node scripts/preserved-lines.cjs --diff <merge-base> origin/<head> --label "#<n>"`.
- For the PR already on main: `node scripts/preserved-lines.cjs --diff <squash>^ <squash> --label "#<other>"`.
- Classify every missing line as an intended supersession or a loss. Restore the losses and list the supersessions in Notes.

## A refactor landed first: the resolution is a port

When main has extracted the code the PR changes into new modules:

- Take main's structure, then port the PR's changes function by function into their new homes.
- Prove the port with references/refactors.md (AST compare for TS, `ast.dump` for Python), and run a negative control against main's pre-port version, which should DIFFER.
- Port the tests too, then mutation-test them: break the ported code, watch the tests fail, restore it byte for byte.

## The author pushes while you work

- Before pushing, fetch and check `git merge-base --is-ancestor origin/<head> HEAD`.
- If the branch moved, stop and tell the user. When they confirm the author is done:
  1. back up your commits on a branch;
  2. rebuild on the author's latest;
  3. re-apply your changes and re-validate;
  4. push as a fast-forward.
- If the author resolved the main merge themselves, verify their resolution rather than redoing it.

## Merge commit message

`Merge branch 'main' into <headRefName>`. Amend git's default "Merge remote-tracking branch ..." message before pushing.
