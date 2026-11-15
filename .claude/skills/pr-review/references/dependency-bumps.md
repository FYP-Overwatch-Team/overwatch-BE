# Dependency bump PRs

- **Read the real changelogs** and release notes for every bumped package, not summaries: `gh release view <tag> --repo <owner>/<repo>`, or the CHANGELOG at the tag. Note behaviour changes that touch how this code uses the package.
- **Prove the lockfile.** A textual merge of a lockfile isn't proof.
  - FE: `pnpm install --frozen-lockfile`. After a deliberate bump, a normal install's lockfile diff should touch only the intended packages. Build-script allowances and overrides live in `pnpm-workspace.yaml`, not package.json.
  - BE: `uv sync --locked`, then `uv lock --check`. Python is pinned to 3.14 (`.python-version`, `pyproject.toml`, CI); a package that drops 3.14 wheels breaks the Docker build, not just local installs.
- **Exercise the change:**
  - FE: lint, typecheck, `pnpm build`, then click through the affected UI in light and dark mode. Next, React, Tailwind and tailwind-merge bumps deserve a look at the component docs (`/docs/ui`, in `pnpm dev`).
  - BE: both pytest runs, then boot the app (`uv run uvicorn app.main:app`) for framework bumps (FastAPI, Starlette, pydantic, motor, neo4j driver). Fakes like mongomock-motor can mask driver API changes.
- **Major bumps** with breaking changes need every call site checked, not just a green build.
- **Deploy:** a BE bump goes to prod on merge. Recommend checking `/health` and one real flow (sign-in, repo graph) right after.
