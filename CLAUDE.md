# Manifold Trading Bot — Project Instructions

Collaborative prediction marketplace paper trading bot using OpenClaw + Manifold Markets API.
Built by a group of friends learning AI together.
Repo: https://github.com/amirghari/manifold-trading-bot

## Infrastructure (Tailscale VPN)

- Homelab: `ssh aleksi@$HOMELAB_IP` (IP in `.env`)
- Hackathon sandbox: `sshpass -p "$SANDBOX_PASS" ssh -o StrictHostKeyChecking=no hackathon@$SANDBOX_IP` (credentials in `.env`)
- Sandbox is 100% isolated from host — cannot see Kevin or host credentials
- sshpass required for non-interactive SSH (install: `brew install hudochenkov/sshpass/sshpass`)

## OpenClaw Architecture

OpenClaw is a self-hosted AI agent gateway (Node.js). It bridges an LLM to chat channels (Telegram) with built-in cron scheduling, session management, and a skills system.

### Process Model

The gateway runs as three processes started by the container's init system via `runuser`:
```
root       runuser -u hackathon -- openclaw gateway run
hackathon  openclaw                    (main process)
hackathon  openclaw-gateway            (gateway worker, ~437 MB RSS)
```
There is **no systemd service, no supervisor, no OS crontab**. The gateway was launched by the container entrypoint (Docker CMD) and has been running continuously since Mar 28. If it dies, the container restarts it.

### Configuration

Main config: `/home/hackathon/.openclaw/openclaw.json`
- **Primary model**: DeepSeek Reasoner (`deepseek-reasoner`, 131K context, reasoning model)
- **Fallback model**: DeepSeek Chat (`deepseek-chat`, 131K context, used by cron jobs)
- **Tools profile**: `coding` — gives shell execution, file read/write
- **Telegram**: Bot `@hackathon_26_bot`, group `-5240775171`, DM allowlist (3 user IDs), group policy `open`
- **Session scoping**: `per-channel-peer` — each DM user gets their own session
- **Gateway**: port 18789, loopback only, token auth
- **Plugins**: `deepseek` plugin enabled
- **Available update**: v2026.3.28 (not yet applied, current is v2026.3.24)

### Session Model (critical for understanding cron behavior)

OpenClaw sessions are the core concept. Each conversation thread is a session:
- **DM sessions**: Keyed as `agent:main:telegram:direct:<userId>` — persistent, the bot remembers previous messages
- **Group sessions**: Keyed as `agent:main:telegram:group:<groupId>` — persistent within the group
- **Cron sessions**: Keyed as `agent:main:cron:<jobId>:run:<sessionId>` — **ISOLATED**. Each cron run gets a fresh session with NO memory of previous runs

This isolation is why the bot rediscovers the same information every hour during cron runs. The bot's startup protocol (from AGENTS.md) reads SOUL.md, USER.md, and memory files, but the cron prompt doesn't instruct it to read project-specific context like which state file to use.

### Cron System

**⚠️ MIGRATED — All scheduling now uses Linux OS crontab, NOT OpenClaw cron.**

OpenClaw `agentTurn` cron jobs route through an LLM agent that has a Telegram session wired in. No configuration prevents the agent from narrating results to the group chat. Migrated to OS crontab on 2026-04-09 — scripts run directly with no LLM in the loop.

**OpenClaw cron jobs** (IDs below) still exist on the server but are **ALL DISABLED**. Do not re-enable them.

| Job | ID | Status |
|---|---|---|
| Market Research | `359e61eb-5d56-452e-ab9d-506ddd9c4cb6` | **DISABLED** |
| Auto Trading | `57ce0ebc-d780-4c80-ba51-476a4a62f7ea` | **DISABLED** |
| Daily Summary | `0a77ecb9-0652-4afa-873d-0dffeb28b708` | **DISABLED** |

**Live OS crontab** (view/edit with `crontab -l` / `crontab -e` on the server):

```
# Hourly :00 — market research
0 * * * *   cd $WORKSPACE && git pull origin main -q && python3 automation/auto_research.py >> /tmp/research.log 2>&1

# Hourly :10 — position resolution
10 * * * *  cd $WORKSPACE && git pull origin main -q && python3 scripts/resolve_positions.py >> /tmp/resolution.log 2>&1

# Hourly :20 — auto trading
20 * * * *  cd $WORKSPACE && git pull origin main -q && python3 automation/auto_trader.py >> /tmp/trader.log 2>&1

# Daily 19:00 UTC — daily summary
0 19 * * *  cd $WORKSPACE && git pull origin main -q && python3 automation/daily_summary.py >> /tmp/daily_summary.log 2>&1

# Sunday 02:00 — calibration harvest
0 2 * * 0   cd $WORKSPACE && git pull origin main -q && python3 scripts/harvest_resolved.py --limit 2000 && python3 scripts/analyze_calibration.py >> /tmp/harvest.log 2>&1

# Sunday 02:30 — M2 re-audit
30 2 * * 0  cd $WORKSPACE && python3 scripts/audit_resolved_markets.py --quiet >> /tmp/harvest.log 2>&1

# Sunday 03:00 — M3 snapshot reconstruction
0 3 * * 0   cd $WORKSPACE && python3 scripts/reconstruct_snapshots.py >> /tmp/harvest.log 2>&1

# Sunday 03:30 — backtest → strategy weights
30 3 * * 0  cd $WORKSPACE && python3 scripts/backtest_from_snapshots.py >> /tmp/harvest.log 2>&1

# Monday 07:00 — weekly EV report
0 7 * * 1   cd $WORKSPACE && git pull origin main -q && python3 scripts/weekly_ev_report.py --telegram >> /tmp/ev_report.log 2>&1

# Monday 07:30 — live strategy weights
30 7 * * 1  cd $WORKSPACE && git pull origin main -q && python3 scripts/compute_strategy_weights.py && git add data/strategy_weights.json && git diff --cached --quiet || git commit -m "chore: update strategy weights [skip ci]" >> /tmp/weights.log 2>&1
```

**Setup on a fresh server:** Run `automation/setup_cron_jobs.py` — it prints the full crontab block ready to paste into `crontab -e`.

### Memory System

- **SQLite DB**: `/home/hackathon/.openclaw/memory/main.sqlite` — has FTS5 + embedding schema but is currently **EMPTY** (0 chunks indexed)
- **Session logs**: ~50 JSONL files in `agents/main/sessions/` (~3.4 MB total)
- **Workspace memory**: `memory/YYYY-MM-DD.md` daily notes (read by bot on startup per AGENTS.md)

### Bot Behavior Files (in workspace)

| File | Purpose | Status |
|---|---|---|
| `SOUL.md` | Bot personality (5 lines — helpful, curious, concise) | Active, minimal |
| `AGENTS.md` | Agent behavior rules (10.8 KB) — startup protocol, memory system, git workflow, group chat etiquette, heartbeat | Active, primary behavior config |
| `IDENTITY.md` | Name/creature/vibe template | **Empty — never filled in** |
| `USER.md` | User profile template | **Empty — never filled in** |
| `BOOTSTRAP.md` | First-run onboarding script | **Should have been deleted after first conversation** |
| `HEARTBEAT.md` | Periodic check template | Active but minimal |
| `TOOLS.md` | Local tool notes template | **Empty — no tools recorded** |

### Skills (bundled with OpenClaw)

8 skills available: `gh-issues`, `github`, `healthcheck`, `node-connect`, `session-logs`, `skill-creator`, `tmux`, `weather`. Loaded lazily from `/usr/local/lib/node_modules/openclaw/skills/`.

### OpenClaw CLI Commands

Full command set: `acp`, `agent`, `agents`, `approvals`, `backup`, `browser`, `channels`, `config`, `configure`, `cron`, `dashboard`, `devices`, `directory`, `dns`, `docs`, `doctor`, `gateway`, `health`, `hooks`, `logs`, `memory`, `message`, `models`, `node`, `nodes`, `onboard`, `pairing`, `plugins`, `qr`, `reset`, `sandbox`, `secrets`, `security`, `sessions`, `setup`, `skills`, `status`, `system`, `tui`, `uninstall`, `update`, `webhooks`.

## Read Telegram Bot Messages

```bash
sshpass -p "$SANDBOX_PASS" ssh -o StrictHostKeyChecking=no hackathon@$SANDBOX_IP 'python3 /usr/local/bin/telegram-reader.py --last 20'
```
Options: `--last N`, `--since HOURS`, `--all`

## Filesystem Map (sandbox workspace)

Root: `/home/hackathon/.openclaw/workspace/` (a git repo tracking `main` of this GitHub repo)

### Active files — what matters

```
manifold_bot/                         # Core bot package
  config.py                           # API config (reads MANIFOLD_API_KEY from env)
  manifold_api.py                     # Manifold Markets API client
  strategies.py                       # 3 strategies: probability_direction, mean_reversion, volume_spike
  paper_trader.py                     # Paper trading engine — loads state from manifold_bot/paper_trading_state.json
  paper_trading_state.json            # *** ACTIVE state file *** ($425 balance, 8 positions)
  dashboard.py                        # FastAPI web dashboard (not deployed)
  main.py                             # CLI entry point

automation/                           # Cron job scripts (these are what cron runs)
  auto_research.py                    # Hourly market scanner → writes market_research.json
  auto_trader.py                      # Hourly trader → reads market_research.json, updates paper_trading_state.json
  daily_summary.py                    # Daily report generator
  send_telegram.py                    # Telegram message helper
  setup_cron_jobs.py                  # Cron job setup utility
  cron_jobs_config.json               # Cron template

market_research.json                  # Latest research output (~426 KB, ~100 markets, ~40 recommendations)
auto_trades.json                      # Trade execution log (8 trades)
memory/                               # Daily session notes read by bot on startup
tests/                                # test_manifold.py, test_automation.py
scripts/                              # Dev tools (demo, portfolio viewer, market ID lookup)
```

### Stale files — to be cleaned up

```
paper_trading_state.json              # ROOT-LEVEL — stale copy ($1,069, from Mar 28 only). NOT used by auto_trader.
paper_trading_state_clean.json        # Leftover from manual cleanup attempts
paper_trading_state_fixed.json        # Leftover from manual fix attempts
auto_research.py                      # ROOT-LEVEL duplicate of automation/auto_research.py
auto_trader.py                        # ROOT-LEVEL duplicate of automation/auto_trader.py
BOOTSTRAP.md                          # Should have been deleted after first conversation
```

### Environment Variables

Set in `/etc/profile.d/hackathon.sh` (sourced for login shells, NOT by the gateway process):
- `MANIFOLD_API_KEY` — Manifold Markets API key (DO NOT commit to repo)
- `DEEPSEEK_API_KEY` — DeepSeek API key (DO NOT commit to repo)
- `PIP_BREAK_SYSTEM_PACKAGES=1`

**Known issue**: The gateway process (started by `runuser` at container boot) does NOT source `/etc/profile.d/`. Cron sessions inherit the gateway's environment, so `MANIFOLD_API_KEY` is not available during cron runs. This causes a warning but doesn't break paper trading (PaperTrader doesn't need the API key for simulated trades).

## Known Recurring Issues

These were identified by analyzing all 439 Telegram messages (Mar 28-30).

### P0 — Cron Session Amnesia (root cause of most problems)

**Problem**: Every cron run is an isolated session. The bot has NO memory of previous runs. Each hour it:
1. Re-reads auto_trader.py from scratch (5+ tool calls)
2. Rediscovers the two paper_trading_state.json files (5+ messages investigating)
3. Misinterprets exit code 1 as an error (it means "no trades executed")
4. Warns about missing MANIFOLD_API_KEY
5. Produces 5-15 Telegram messages of investigation noise per run

**Root cause**: The cron job prompts are bare commands with no context. The bot doesn't know which state file to use, what exit codes mean, or what's expected behavior.

**Fix**: Update cron prompts (via `openclaw cron edit <id> --message "..."`) to include essential context inline, OR create a runbook file and instruct the prompt to read it first.

### P1 — Dual State File

**Problem**: Two `paper_trading_state.json` files with different data:
- Root workspace: $1,069, 3 positions (stale, from Mar 28 testing)
- `manifold_bot/`: $425, 8 positions (active, used by PaperTrader)

**Root cause**: Root file was created during early development. Never cleaned up.

**Fix**: Delete or archive the root file. Also delete `paper_trading_state_clean.json` and `paper_trading_state_fixed.json`.

### P1 — Mock Positions Blocking Trades

**Problem**: 2 of 8 positions in the active state are mock/test markets. Combined with 4 real positions = 6 total, exceeding max_positions=5. No new trades can execute.

**Fix**: Close mock positions in `manifold_bot/paper_trading_state.json`.

### P2 — MANIFOLD_API_KEY Not Reaching Cron

**Problem**: Env var set in `/etc/profile.d/hackathon.sh` but gateway process doesn't source it.

**Fix**: Either set the env var in openclaw.json config, or prefix the cron command with `source /etc/profile.d/hackathon.sh &&`.

### P2 — Exit Code Semantics

**Problem**: `auto_trader.py` returns `exit(1)` when no trades are executed. This is treated as an error by the bot.

**Fix**: Change to `exit(0)` for "no trades" (normal operation), reserve `exit(1)` for actual errors.

### P3 — Research Quality

**Problem**: All 39-44 recommendations per hour show exactly 0.65 confidence, all using `volume_spike` strategy, all "NO" direction. No real signal differentiation.

**Root cause**: Statistical strategies produce uniform output. AI integration (DeepSeek reasoning about market content) is not built yet.

### P3 — Bot Verbosity

**Problem**: 10-30 Telegram messages per hour from cron investigation noise. The group chat is drowning in bot output that's mostly the bot talking to itself.

**Fix**: Addressed by fixing cron amnesia (P0). Better prompts = less investigation = fewer messages.

### P4 — Incomplete Bootstrapping

**Problem**: IDENTITY.md and USER.md are empty templates. BOOTSTRAP.md still exists. Memory DB is empty.

## Automated Pipeline

| Layer | Script | Schedule | Output |
|---|---|---|---|
| Market research | `automation/auto_research.py` | Hourly :00 UTC | `market_research.json` |
| Position resolution | `scripts/resolve_positions.py` | Hourly :10 UTC | updates state, Telegram if resolved |
| Trade execution | `automation/auto_trader.py` | Hourly :20 UTC | updates `paper_trading_state.json` |
| Daily report | `automation/daily_summary.py` | Daily 19:00 UTC | Telegram message |
| Calibration harvest | `scripts/harvest_resolved.py` + `analyze_calibration.py` | Sundays 02:00 UTC | `data/calibration.db`, `calibration_table.json` |
| M2 re-audit | `scripts/audit_resolved_markets.py` | Sundays 02:30 UTC | `data/m2_audit.json` |
| M3 reconstruction | `scripts/reconstruct_snapshots.py` | Sundays 03:00 UTC | `data/market_snapshots.db` (reconstructed rows) |
| Backtest weights | `scripts/backtest_from_snapshots.py` | Sundays 03:30 UTC | `data/strategy_weights.json` (backtest-sourced) |
| EV accuracy report | `scripts/weekly_ev_report.py --telegram` | Mondays 07:00 UTC | Telegram message |
| Strategy weights | `scripts/compute_strategy_weights.py` | Mondays 07:30 UTC | `data/strategy_weights.json` (live-sourced), git commit |

Risk parameters: max 10 positions, max 3 per category, 65% min confidence, MAX_BET_AMOUNT=$5 (throttled — restore to $25 once strategy weights diverge from 1.0).

## Beads Tracking

- Epic: `workbench-f1a` (Manifold Prediction Bot — Ongoing Project)
- Infra setup: `workbench-abd` (closed, contains teardown checklist)

## Development Workflow

- `main` is protected — cron jobs run from it, pre-push hook enforces tests
- Feature branches: `feature/name` or `fix/name` → PR to `main`
- Tests must pass: `python3 tests/test_manifold.py && python3 tests/test_automation.py`
- Bot cannot merge its own PRs — human review required (per AGENTS.md)
- Git sync on every cron run: `git fetch origin && git checkout main && git pull origin main`
