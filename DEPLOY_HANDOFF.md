# Handoff prompt — deploy the Transactions-move + Screener frontend changes

Paste everything below the line into a Claude Code session that has **Supabase MCP**
and **Cloudflare MCP** connected and access to this repo on disk.

---

You have Supabase MCP and Cloudflare MCP connected. Repo:
`/Users/eden.swacknebius.com/Documents/Eden/Portfolio_App`, frontend in `portfolio-pal/`.
Goal: **build and deploy the current frontend working tree** so two already-written
changes go live. Do not rewrite the feature code — it's done and type-checked.

## What's shipping (already in the working tree)
1. **Transactions moved into Settings.** `/transactions` route is now `/settings/history`;
   removed from the main nav; a "History" section in Settings links to it; the page has
   a back-link to Settings. Files: `src/App.tsx`, `src/components/Layout.tsx`,
   `src/pages/Settings.tsx`, `src/pages/Transactions.tsx`.
2. **New `/screener` page** (4 tabs, reads Supabase `screener_cache`). Files:
   `src/pages/Screener.tsx` (new), `src/App.tsx` (route), `src/components/Layout.tsx`
   (nav link), `src/lib/api.ts` (`fetchScreenerResults` + types).

## Already done — do NOT redo
- The Supabase `screener_cache` table + RLS already exist (project `cbfasivpzaacbojmpqef`).
  You can sanity-check via Supabase MCP that the table and `screener_cache_select` policy
  are present, but do not recreate them. The table is empty until the Python screener
  service runs (out of scope here) — `/screener` will show its empty state, which is fine.

## Deploy steps
1. **Discover the deploy target.** The local `portfolio-pal/` is NOT a git repo (no `.git`).
   Use Cloudflare MCP to list **Pages** projects on the account that owns the
   `snaptrade-proxy` / `finhub-ticker-proxy` workers and find the one serving this app
   (a portfolio / wealth-tracker site). Also check for a connected GitHub repo or Lovable
   project (migration filenames are Lovable-style UUIDs).
2. **Build:** `cd portfolio-pal && bun install && bun run build` (falls back to `npm ci &&
   npm run build`). Output goes to `portfolio-pal/dist/`. Note: the project has some
   PRE-EXISTING `tsc` type errors that Vite ignores — a clean `vite build` is success;
   don't get blocked chasing them.
3. **Deploy `dist/`** to the Cloudflare Pages project found in step 1 (Cloudflare MCP
   deploy, or `wrangler pages deploy dist --project-name=<name>`). If the app is instead
   GitHub/Lovable-backed (not Pages), STOP and report the remote — pushing needs the
   upstream wired up first since this folder has no `.git`.

## Verify after deploy
- App loads; main nav no longer shows "Transactions".
- Settings page shows a "History" section whose "Open" button goes to `/settings/history`
  and renders the transactions UI.
- `/screener` loads with 4 tabs and shows the empty state (no data yet).

Report: the deploy target you found, the deployed URL, and the verification result.
If neither a Pages project nor a git remote is found, stop and report — do not invent one.
