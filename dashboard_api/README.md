# dashboard_api — read-only HTTP shim for the dashboard

This is the **Phase 2** API layer. It does NOT run yet. It exists as a placeholder
and design contract so the dashboard's `lib/data/*` adapters can be swapped to
HTTP calls without changing any UI code when we deploy the dashboard off-sandbox.

## Why this exists

Phase 1 (now): The Next.js dashboard runs on the operator's laptop and reads
the bot's JSON/SQLite files directly via `fs.readFile` (Node Server Components).
This is the cheapest option but only works when the dashboard process can see
the same filesystem the bot writes to (i.e., on the sandbox or via SCP/rsync).

Phase 2 (later): When we want to deploy the dashboard to Vercel (public-shareable
URL, no sandbox CPU cost, no friend-network exposure question), the dashboard
needs an HTTP source of truth for the bot's data. That source is this service.

## Design

- **Stack**: FastAPI (matches the rest of the Python codebase).
- **Auth**: bearer token from `DASHBOARD_API_TOKEN` env var. Single token for v1.
- **Exposure**: bound to Tailscale interface only OR exposed via Tailscale Funnel
  (HTTPS, no firewall changes). Never bound to `0.0.0.0` on the friend's network.
- **Endpoints**: one-to-one with `dashboard/src/lib/data/*` functions:
  - `GET /api/portfolio` → `paper_trading_state.json`
  - `GET /api/research/latest` → `market_research.json::latest`
  - `GET /api/counters/research?tail=N` → `data/research_counters.jsonl`
  - `GET /api/counters/trader?tail=N` → `data/trader_counters.jsonl`
  - `GET /api/strategy-weights` → `data/strategy_weights.json`
  - `GET /api/calibration` → `data/calibration_table.json`
  - `GET /api/swaps` → `pending_swaps.json`
  - `GET /api/diagnostics` → file freshness map
  - `GET /api/health` → 200 if process is up
- **Multi-tenancy**: every endpoint accepts `?bot_id=main` (default `main`).
  When we go multi-tenant, this is the partitioning key.
- **Strict read-only**: no POST/PUT/DELETE in v1. Swap approval is the first
  candidate write endpoint and lives behind a separate auth check when added.

## Deployment plan when we get here

1. Add `dashboard_api/main.py` with the endpoints above.
2. Run on the sandbox under tmux or systemd-user, bound to Tailscale IP.
3. Update `dashboard/.env.production` with `WORKSPACE_ROOT=https://...api.../`
   (or a separate env var) — the data adapters then `fetch()` instead of `readFile()`.
4. Deploy `dashboard/` to Vercel.

## Why not now

Building the API now would mean maintaining two copies of every data-access
function (Python and TypeScript) before either is needed. We defer it until the
dashboard graduates beyond local dev.
