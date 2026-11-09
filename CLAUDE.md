# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Expol NZ raw material purchasing tool: tracks weekly stock, usage, forecasts, purchase orders and inter-branch transfers per branch. It is a static site with no build system, package manager, tests or linter. Each HTML file is opened or served directly, and all data comes from a Supabase backend (PostgREST at `https://jrpysikxibrbbellroid.supabase.co/rest/v1/`, using the public anon key embedded in each file).

## Files

- **`desktop.html`** (~17k lines, ~870KB): the full app. It is an **esbuild bundle output** (entry marked `// tmp/full_entry2.jsx` around line 12719), with React and ReactDOM production builds inlined above it. The JSX source is **not in this repo**, so edits go straight into the compiled JS: write `import_react.default.createElement(...)` calls and `(0, import_react.useState)(...)`, not JSX. App code runs from about line 12719 to the end; everything above it is vendored React and should not be touched.
- **`mobile.html`** (~730 lines): a separate, read-mostly PWA view written in plain vanilla JS (a global `state` object plus `render*()` functions that build HTML strings). It has no React.
- **`sw.js`**: service worker for web push notifications (overdue order alerts). It must be served from the same origin as the HTML. Notification icons are `icon-192.png` (full colour) and `icon-72.png` (white-on-transparent badge), referenced by relative path because the site is hosted under a GitHub Pages subpath. Use relative URLs, not root-absolute ones.
- **`supabase/functions/send-push-alerts/index.ts`**: Deno Edge Function that sends the daily 9am NZT overdue/critical order push. pg_cron calls it at 20:00 and 21:00 UTC, and it only sends on the run that falls at 9am Pacific/Auckland. It requires the service role key in `Authorization`. `?force=1` sends at any time, and `?force=1&dry=1` returns the alerts without sending. It uses aes128gcm encryption (required for iOS) and VAPID done by hand with WebCrypto, with no npm dependencies. The VAPID public key must match `VAPID_PUBLIC_KEY` in both HTML files. The private key is the `VAPID_PRIVATE_KEY` Supabase secret and is never stored in the repo. Deploy with `supabase functions deploy send-push-alerts`.

## Architecture (desktop.html)

- **Data layer:** the `sb()` helper and its wrappers `dbGet`, `dbPost`, `dbPatch` and `dbDel` take PostgREST paths with query strings, e.g. `dbGet("orders?select=*&order=eta")`. Some code calls `fetch(SUPABASE_URL + ...)` directly with `SB_HDRS`. Upserts use `?on_conflict=...` with `Prefer: resolution=merge-duplicates`.
- **Loading:** `App()` loads every table at once in `load()` with `Promise.all`, keeps the results in top-level state, and passes them down as props. Large tables (`usage`, `erp_po_lines`) are paged 1000 rows at a time. After a mutation, components call `reload()` to fetch everything again.
- **Tables:** `branches`, `materials` (branch-specific), `material_types` (shared types, with `stock_code`), `weeks` (per branch, `week_number`/`week_ending`), `usage` (unique on `week_id,material_id`), `orders` and `order_lines`, `transfers`, `suppliers`, `branch_locations`, `stock_reconciliation` (ERP-verified stock), `erp_po_lines` (synced from the ERP), `push_subscriptions`.
- **Branch visibility:** a branch only gets a tab (and appears in All view, Forecast Orders, alerts and dropdowns) if it has at least one row in `branch_locations` (Settings → Locations). If there are no mappings at all, every branch is shown. In `App()`, `allBranches` is the full table and is only passed to `SettingsPage`, so hidden branches can be mapped again. `branches` is the filtered list from `visibleBranches()`. `mobile.html` applies the same rule in `load()`.
- **Stock engine: `computeStocks(bw, ul, bOrds, bLines, mats, bTransfers, curIdx, bRecon)`** is the core calculation. It runs a week-by-week stock balance:
  - Weeks at or before `curIdx` use actual usage, and weeks after it use the forecast.
  - Order lines are split into received, partially received (`sup_quant`) and pending portions, each placed in a week by received date or ETA.
  - Transfers into the branch are added and transfers out are subtracted.
  - `opening_stock` only anchors the first week.
  - The ERP figure from reconciliation (`erp_anchor`) resets the running balance so later weeks stay aligned with the ERP.
  - The displayed figure is chosen in this order: admin `closing_stock`, then ERP stock, then the calculated value.
- **Current week:** `curIdx` is the index of the first week whose `week_ending` is on or after today, using `todayLocal()` (local date, not UTC).
- **Stock status:** `stockStatus` and `avg6` (6-week average usage) produce the ok / watch / low / critical levels.
- **Main components:** `BranchView` (per-branch tabs), `AllView` (all branches, admin only), `ForecastTool` (builds suggested orders grouped by branch and supplier, and checks for whole containers), `OrderModal`, `TransferModal`, `StockChart`, and the settings screens.
- **Roles:** a client-side Team/Admin toggle (`role` state). It has no real auth; it only hides admin-only UI such as forecast orders, order editing and settings.
- **ERP-locked orders:** order lines on an order with a PO reference are controlled by the ERP and can't be edited in the app. Only forecast orders, or orders without a PO, can be edited by hand.
- **Styling:** all inline style objects in a GitHub-dark palette (`#0d1117`, `#161b22`, `#30363d`, etc.), with shared button styles `btnP`, `btnG`, `btnD` and `btnS`.

## Duplicated logic: keep both files in sync

`mobile.html` has its own copies of `computeStocks`, `stockStatus`, `avg6` and `todayLocal`, plus the Supabase constants. When you change stock calculation logic in one file, make the same change in the other. The two copies have drifted before; for example, reconciliation support was added to the mobile copy in a separate commit.

## Working tips

- `desktop.html` is too large to read in one go. Use Grep to find the function you need, then read only that line range.
- No local tooling is needed. To test, open the HTML file in a browser or serve the folder statically (push notifications and `sw.js` need http(s), not `file://`).
