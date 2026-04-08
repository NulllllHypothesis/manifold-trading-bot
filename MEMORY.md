# MEMORY.md - Long-Term Memory

This file contains distilled learnings and significant events from daily logs. Updated periodically during heartbeat checks.

## 2026-03-30: Project Reorganization

- Restructured workspace into clear directories: `automation/`, `tests/`, `scripts/`, `docs/`
- Cron jobs updated to run from `automation/` directory
- Key cron jobs: Market Research (every 4h), Auto Trading (every hour at :15), Daily Summary (19:00 UTC)
- OpenClaw cron commands now use isolated target with deepseek/deepseek-reasoner model

## 2026-04-02: Cron Job Setup & Monitoring

- Added missing Auto Trading cron job (ID: 00695c33-9a68-4271-93c3-b61496f92725)
- All three cron jobs now running healthy
- Heartbeat checks every 30 minutes to monitor git sync and cron status
- Git repository: `https://github.com/NulllllHypothesis/manifold-trading-bot.git`
- Current commit: `6137b9e` (fix: skip research when at max positions, reduce to every 4h)

## 2026-04-03: Git Updates

- Main branch updated to `fcfd339` ("tech overview") and `df60609` ("review agent set up")
- Feature branch `feature/ai-analyzer` created for development
- Heartbeat monitoring continues with 30-minute intervals

## 2026-04-04: Workspace Cleanup

- Removed accidentally tracked `__pycache__` files and `paper_trading_state.json` from git
- Added `memory/` and `*.bak` to `.gitignore` to prevent committing runtime data
- Added `MEMORY.md` to repository for long-term memory tracking
- Updated remote repository URL note: repository moved to `https://github.com/NulllllHypothesis/manifold-trading-bot.git`
- All cron jobs healthy: Auto Trading (hourly at :15), Market Research (every 4h), Daily Summary (19:00 UTC)

## 2026-04-04: AI Analyzer Feature Merged

- Merged feature/ai-analyzer branch into main (PR #2)
- Added calibration & feedback loop planning
- Fixed auto_research.py truncation and daily_summary exit code
- Current commit: 307fc42 (fix: restore truncated auto_research.py, fix daily_summary exit code)

## 2026-04-04: Diagnostic Scripts & Monitoring

- Added diagnostic scripts (quick_status.py, resolve_positions.py, etc.) to .gitignore to prevent accidental commits
- All cron jobs running consistently: Daily Summary at 19:00 UTC, Market Research every 4h, Auto Trading hourly at :15
- Heartbeat monitoring continues with 30-minute intervals, ensuring git sync and cron health

## 2026-04-05: Bet Details Storage Feature

- Merged feature/store-bet-details branch into main (commit: 584cbf2)
- Auto-trader now stores 'question' (what the bet was about) and 'reasoning' (why the bet was made) in auto_trades.json
- Provides audit trail for trading decisions: combines strategies used and AI reasoning
- All tests pass; cron jobs will pick up change in next trading cycle

## 2026-04-05: Risk Parameter Adjustment

- Merged feature/adjust-risk-parameters branch (commit: 55d588e)
- Increased max_positions from 5 to 10 (bot had 8 open positions, was stuck)
- Reduced confidence threshold from 0.65 to 0.60 to capture more opportunities
- Reduced cooldown period from 6 to 4 hours for faster re-entry
- Bot immediately executed 2 new trades after adjustment (now at 10/10 max positions)
- Current balance: $379.59 with $500 in open positions (57% exposure)

## 2026-04-06: Market Research Cron Job Monitoring

- Heartbeat monitoring detected persistent timeout errors in Market Research cron job (5-minute timeout) across multiple cycles
- Issue resolved by 2026-04-07 20:00 UTC run (status returned to healthy)
- Lesson: Timeout errors can be transient; heartbeat monitoring provides early detection and confirmation of resolution

## Rules & Conventions

- Never push directly to `main` branch
- All development happens on feature branches
- Run full test suite before opening PR
- Cron jobs always run from `main` branch
- Heartbeat checks include git fetch and cron status verification