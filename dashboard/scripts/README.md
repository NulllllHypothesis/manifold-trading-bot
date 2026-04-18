# Dashboard scripts

## What's here

| Script | Purpose |
|---|---|
| [`check-connection.sh`](check-connection.sh) | Diagnose the full connection pipeline to the sandbox — local tooling, env, TCP, SSH auth, remote files, bot processes. Never modifies anything. |
| [`sync-from-sandbox.sh`](sync-from-sandbox.sh) | Pull the bot's data files from the sandbox to a local snapshot directory the dashboard reads. Idempotent — only transfers changed files. |

## Quick start

```bash
cd dashboard
pnpm check          # diagnose the sandbox connection
pnpm sync           # one-shot pull (needs auth first — see below)
pnpm dev:sandbox    # sync once, then boot dev server against the snapshot
pnpm sync:watch     # re-sync every 60 seconds (run in a second terminal)
```

## Auth setup (one-time)

The connection check will tell you which path is working. You need **one** of:

### Option A — SSH key auth (recommended)

Push your laptop's public key to the sandbox. You'll be prompted for the sandbox
password once, then never again:

```bash
ssh-copy-id hackathon-server
```

After this, `pnpm check` should show `SSH key auth: working (preferred)`.

### Option B — password in workspace/.env

Less ideal (password on disk), but works if you can't edit the sandbox's
`~/.ssh/authorized_keys`:

```bash
echo 'SANDBOX_PASS=yourpassword' >> ../.env
chmod 600 ../.env
```

Make sure `.env` is gitignored. It already is in this repo.

## Why a local snapshot, not direct reads?

The dashboard could read sandbox files over SSHFS or an HTTP shim. We
deliberately don't. Rsync-to-snapshot gives us:

- **Zero persistent CPU cost on the sandbox.** The sandbox is already strained
  (75% Ollama timeout rate). A persistent HTTP server would make that worse.
- **Offline dev.** Once synced, the dashboard works without network.
- **No new ports exposed.** No HTTP on the friend's home network.
- **Honest freshness.** The dashboard's Diagnostics page shows real file mtimes,
  which is exactly what we want an operator to see.

Trade-off: data is as fresh as the last sync. Mitigation: `pnpm sync:watch`
runs every 60 seconds in the background. That's plenty for a bot that only
updates hourly.

## What gets synced

Defined in [`sync-from-sandbox.sh`](sync-from-sandbox.sh), kept in sync with
`dashboard/src/lib/config.ts::DATA_PATHS`. Total transfer is typically <5 MB
and takes <2 s on a warm connection.

- `market_research.json` — latest research snapshot (~100 KB)
- `manifold_bot/paper_trading_state.json` — positions + balance
- `auto_trades.json`, `pending_swaps.json`
- `data/research_counters.jsonl`, `data/trader_counters.jsonl` — funnel logs
- `data/strategy_weights.json`, `data/calibration_table.json`,
  `data/category_accuracy.json`
- `data/m2_audit.json`, `data/eval_report.json`, `data/backtest_report.json`
- `data/ai_timeout_cooldown.json`, `data/news_cache.json`
- `data/calibration.db`, `data/market_snapshots.db` — SQLite (~2 MB each)

## Snapshot location

Default: `/tmp/manifold-sandbox-snapshot/`

Override with env var `SANDBOX_SNAPSHOT_DIR`.

The dashboard reads from this path when you use `pnpm dev:sandbox`; it does
not read the live local workspace (`../`) in that mode. To switch back to
reading the local workspace for testing, just use `pnpm dev`.
