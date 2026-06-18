# Post-Mortem: Loss of live paper-trading data (2026-06-11)

**Status:** Resolved (data unrecoverable on-server; code intact)
**Severity:** High — total loss of runtime trading history
**Author:** Investigation 2026-06-11
**Affected system:** `manifold-trading-bot` running in the `hackathon-sandbox` container on the homelab (192.168.1.162)

---

## Summary

The bot's entire live runtime dataset — the paper-trading ledger, the
calibration database, and the trade/research logs — was destroyed on
**2026-06-11 at ~15:20 UTC** when the `hackathon-sandbox` container was rebuilt
and recreated during routine homelab maintenance (an OpenClaw version upgrade
and a Tailscale auth-key rotation).

The data was **not** lost gradually or at the May 7 "freeze." It survived intact
for **two and a half months** and was destroyed in a single step this morning,
because it lived only inside the container's writable layer — not on a mounted
volume or the persistent host directory. Recreating the container deleted the
only copy.

**All source code is safe in git.** Only the derived learning artifacts that were
committed (`data/strategy_weights.json`, `data/calibration_table.json`,
`data/category_accuracy.json`) survive. The raw, bet-by-bet history does not.

---

## What was lost vs. what survived

| Artifact | Location | Status |
|---|---|---|
| All source code, tests, dashboard, automation | git (`origin/main`, all branches) | **Safe** |
| Strategy weights (derived) | `data/strategy_weights.json` (committed) | **Safe** |
| Calibration table (derived) | `data/calibration_table.json` (committed) | **Safe** |
| Category accuracy (derived) | `data/category_accuracy.json` (committed) | **Safe** |
| **Paper-trading state / balance** | `manifold_bot/paper_trading_state.json` (gitignored) | **LOST** |
| **Calibration DB (bet outcomes, EV, P&L)** | `data/calibration.db` (gitignored) | **LOST** |
| **Market snapshots DB (ML training data)** | `data/market_snapshots.db` (gitignored) | **LOST** |
| **Trade log** | `auto_trades.json` (gitignored) | **LOST** |
| **Research log** | `market_research.json` (gitignored) | **LOST** |

Last known performance snapshot (from committed `MEMORY.md`, not the live ledger):
balance **$379.59** with **$500** in open positions at 10/10 max slots — i.e. drawn
down from the $1,000 paper start at that point in the run. This is a mid-run
figure; the actual final ledger is gone.

---

## Timeline (UTC, reconstructed from the systemd journal and Docker metadata)

| When | Event | Evidence |
|---|---|---|
| **2026-03-27 16:43** | `hackathon-sandbox` container created; bot built during the hackathon | journald: container's first and only network-join |
| **2026-03-27 → 05-07** | Bot runs actively, develops, accumulates data in the container's writable layer | git history; 296 tests; 144 commits in W15 alone |
| **2026-05-07** | Development frozen ("portfolio-freeze"); container keeps running | last commit `ab99a02` |
| **2026-03-27 → 06-03** | Container runs continuously — **zero restarts** across a single host boot | journald: one boot spanned Feb 15 → Jun 3; no container restart events |
| **2026-06-03 18:10** | Homelab host dies (start of the 8-day unnoticed outage) | journald: boot −1 ends here |
| **2026-06-03 → 06-11** | Host down for ~8 days | gap between journal boots |
| **2026-06-11 14:55** | Host boots; old container auto-restarts (`--restart unless-stopped`) **on its same writable layer — data still intact** | journald: container rejoins network 14:56 |
| **2026-06-11 ~15:20** | `hackathon-sandbox` image rebuilt (every layer stamped 15:20) and the old container `docker rm`'d + replaced | `docker history`; `docker inspect` Created timestamp |
| **2026-06-11 ~15:20** | **Writable layer deleted → all runtime data destroyed** | overlay2 search finds no trace |

---

## Root cause

**The bot was deployed in the wrong directory — outside the one persistent
mount the container already had — so all its state sat in the container's
ephemeral writable layer.**

A persistent bind mount *did* exist the whole time: host
`/srv/hackathon/workspaces` → container `/home/hackathon/workspace`. But the bot
ran from `/home/hackathon/.openclaw/workspace` (hardcoded as `WORKSPACE` in
`automation/setup_cron_jobs.py`) — a sibling path **not** under the mount. So the
durable storage was there; the bot simply wasn't using it. Everything it wrote
landed in the ephemeral layer and died with the container.

The original "store on a volume" framing was incomplete in a second way: the
bot's runtime state is **scattered across the whole repo tree**, not just
`data/` — repo root (`auto_trades.json`, `market_research.json`,
`autotrader_disabled.flag`, `pending_*.json`, several `*_report.json`), `data/`
(`calibration.db`, `market_snapshots.db`, `markets_normalized.db`, caches,
`.jsonl` counters), and `manifold_bot/paper_trading_state.json`. The correct fix
is therefore to deploy the **entire repo** under the persistent mount (all paths
are derived relative to the repo root), not to mount a single subdirectory.

Confirmed in code:
- `manifold_bot/paper_trader.py`: `_DB_PATH = _PROJECT_ROOT / "data" / "calibration.db"`; `state_file = manifold_bot/paper_trading_state.json`
- `automation/auto_trader.py`: `trade_log_file = <root>/auto_trades.json`, `research_file = <root>/market_research.json`

The container's persistent bind mount (`/srv/hackathon/workspaces` →
`/home/hackathon/workspace`) was never used by the bot; it only ever contained an
empty `hello.txt`. No Docker volume was ever created for the data.

Consequently, the container was always one `docker rm` away from total data loss.
It survived only because nothing recreated the container for 2.5 months. Today's
maintenance — rebuilding the image to bake in the OpenClaw upgrade and rotating
the leaked Tailscale key — finally did.

### Contributing factors
- **No backups of any kind.** `restic` is installed on the host but never
  configured (no repository, no scheduled job). No LVM snapshots. No backup units.
- **No off-box copy.** The only external pull path was the dashboard's
  `sync-from-sandbox.sh` into `/tmp/...` on an operator's laptop (volatile).
- **The 8-day outage went unnoticed**, indicating the monitoring/alerting gap
  tracked separately in the homelab audit.

---

## Recovery options

| Path | Feasibility | Notes |
|---|---|---|
| Docker overlay2 / orphaned layers | **None** | Full-depth search of the (userns-remapped) overlay2 tree found no bot files. The writable layer was deleted on `docker rm`. |
| restic / LVM / filesystem snapshot | **None** | restic unconfigured; zero LVM snapshots; no backup jobs ever ran. |
| ext4 block-level undelete | **Impractical** | ~4.5h elapsed post-delete on a live ext4-on-LVM volume with an active 10 GB Docker build cache overwriting free space. Low odds, high risk; **not recommended**. |
| **Teammate local copy** | **Only real hope** | Ask Amir / Frank / kmorooka whether anyone has `calibration.db` / `paper_trading_state.json` / a dashboard snapshot (`/tmp/manifold-sandbox-snapshot`) on a laptop. |
| Git (derived artifacts) | **Partial — already have** | Strategy weights, calibration table, category accuracy survive. Raw ledger/P&L does not. |

---

## How we pick up the pieces — forward plan

### 1. Attempt off-box recovery (do first, time-sensitive)
Message the team tonight. Ask each person to check their laptop for any copy of
`calibration.db`, `paper_trading_state.json`, `auto_trades.json`, or a
`/tmp/manifold-sandbox-snapshot` directory. If anyone has even a stale copy, it
beats starting from zero.

### 2. Make the data layer durable *before* the bot ever runs again
Non-negotiable prevention so this cannot recur:
- Bind-mount the bot's `data/` directory (and `paper_trading_state.json`) to the
  persistent host path `/srv/hackathon/workspaces/...` **or** a named Docker volume.
- Treat the container as disposable; treat the volume as the asset.
- Add a nightly `restic` (or even `cp`/`sqlite3 .backup`) job that copies the DBs
  off the container to the host and ideally off-box.

### 3. Re-baseline the bot (if no copy is recovered)
- Start a fresh paper account at the $1,000 baseline.
- Keep the surviving committed `strategy_weights.json` / `calibration_table.json`
  as the warm-start prior, so the bot is not learning from scratch.
- Re-run the weekly `harvest_resolved.py` → `analyze_calibration.py` pipeline to
  rebuild calibration history from Manifold's resolved-market API (this *can* be
  rebuilt — it harvests 2,000+ public resolved markets; only our own bet ledger is
  truly gone).

### 4. Tie into the proving-ground program
This bot is the natural competitor in the planned Kevin-vs-agent paper-trading
comparison (tracked in beads `workbench-1jn`). Because the original ledger is
unrecoverable, any head-to-head must start a **fresh clock** — both agents from a
clean baseline under one agreed scoreboard. The prevention work in step 2 is a
prerequisite for entering the bot into that program.

---

## Lessons

1. **A container's writable layer is not storage.** Anything you want to keep goes
   on a mounted volume, full stop.
2. **"It's been running fine for months" is not durability** — it's the absence of
   a recreate event. The first rebuild is fatal without a volume.
3. **An installed backup tool is not a backup.** `restic` being present meant
   nothing because it was never configured or scheduled.
4. **Outage detection matters.** An 8-day silent outage is its own failure;
   external liveness alerting is tracked in the homelab remediation epic.
