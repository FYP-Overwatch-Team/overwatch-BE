# Frontend (supfaizan/overwatch-fyp)

Next.js 16 App Router, React 19, strict TypeScript, Tailwind v4, pnpm. Installed and expected to be used: TanStack Query, axios, react-hook-form with Zod, zustand, radix-ui with cva, next-themes, sonner, socket.io-client, xyflow with dagre. The worktree is `../overwatch-fyp-review`. `AGENTS.md` is the house style; read it before reviewing.

## Install

- `pnpm install --frozen-lockfile` in the review worktree. If node_modules is missing, say so rather than installing into the user's checkout.

## Validate

- **Lint the changed files:** `pnpm exec oxlint $(git diff --name-only --diff-filter=d origin/main...HEAD -- '*.ts' '*.tsx')`. Without `--diff-filter=d`, a deleted file in the list breaks the run. Run the same on main's versions of those files to compare warning counts.
- **Typecheck:** `pnpm exec tsc --noEmit --incremental false`. If it fails only on `.next/types` for a route that no longer exists, that's stale generated output; `pnpm build` regenerates it.
- **Tests:** `pnpm exec vitest run <files>` when the PR has or touches specs. There is no suite yet, so a new behaviour without tests is a Note, not a blocker, unless the PR is a bug fix.
- **Build:** `pnpm build`, the only proof the app compiles as a whole, including server/client boundaries.
- **CI and hooks:** none yet. Everything above is on you.

## Runtime checks when behaviour matters

- **Production build:** `pnpm build && pnpm start -p 3091`, then hit the touched routes with `curl -sI`. Remember `proxy.ts` redirects every path not in its matcher to `/waitlist`; a 307 there is the proxy, not a bug.
- **Browser behaviour** (redirects, auth, theme, keyboard, loading states): use the agent-browser or Claude in Chrome tooling against the running build. Check light and dark themes for any UI change.

## Conventions to hold the PR to (from AGENTS.md)

- **Ownership:** single-feature code lives in `features/<feature>/` (`index.tsx` entry, `components/`, `api.ts`). `shared/` only for code two or more features actually use. Pages in `app/` stay thin.
- **Dependency direction:** `app → features → shared`. A feature importing another feature's internals, or `shared/` importing a feature, is a blocker-level design issue; propose the fix (compose in `app/`, or promote to `shared/`).
- **Client boundary:** `'use client'` only where browser APIs, state or client hooks need it, and as low in the tree as possible. A whole page marked client to use one hook is a should-fix.
- **Secrets:** nothing secret in client modules or `NEXT_PUBLIC_*`. Server-only modules should `import 'server-only'`. A leaked secret is a blocker.
- **Auth:** protected data is checked on the server. Proxy redirects and hidden UI are not authorization.
- **Data access:** requests in the feature's `api.ts`, not inline in components. Reuse TanStack Query patterns once they exist rather than ad hoc `useEffect` fetching.
- **Styling:** Tailwind utilities and the tokens in `app/globals.css` (`bg-background`, `bg-surface`, `text-muted`, `border-border`, `primary`...). Raw hex, arbitrary values and new CSS files need a reason. Colors must work in dark mode, so tokens over `dark:` overrides over hardcoded values.
- **Shared components:** use `shared/components/button.tsx` and friends instead of restyled `<button>`s. Class merging goes through `cn` from `shared/lib/cn.ts`; custom theme shadows must be registered there or `tailwind-merge` silently drops them.
- **Types:** no `any`, no unjustified casts or `!`. Validate untrusted input with Zod.
- **A11y:** labels, keyboard access, visible focus, `aria-label` on icon-only buttons.

## Check against the backend

- **Contract:** field names, enum strings and status codes must match the BE route and `docs/FRONTEND_GUIDE.md` in overwatch-BE. When the guide and the code disagree, the route file wins; flag the guide as stale.
- **Errors:** every BE error is `{error_code, message}`. UI that maps `error_code` strings must use the exact values from the BE (`FRONTEND_GUIDE.md` §12) or users see raw errors.
- **Auth flow:** GitHub OAuth only. Access JWT plus a rotating refresh token. Presenting an already-rotated refresh token revokes the whole session family (`refresh_token_reused`), so two parallel refreshes from the FE log the user out. Refresh must be single-flight.
- **Removed fields and endpoints** slip past typecheck while the FE type keeps the field optional. Grep for every field the BE PR removed or renamed.
- **Deploy order:** an FE PR whose BE half isn't on main yet is a Deploy constraint (BE first), unless the FE tolerates both shapes.

## Pitfalls

- `??` doesn't catch `''`, so blank strings skip fallbacks.
- `next-themes` sets the theme class before hydration; reading `resolvedTheme` during render causes hydration mismatches. Swap visuals with `dark:` classes instead.
- Dynamic Tailwind class names (`bg-${x}`) aren't generated; use a static map or an inline CSS var.
- Adding a route that must bypass the waitlist needs an exception in the `proxy.ts` matcher.
