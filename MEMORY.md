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
- Git repository: `https://github.com/amirghari/manifold-trading-bot`
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

## Rules & Conventions

- Never push directly to `main` branch
- All development happens on feature branches
- Run full test suite before opening PR
- Cron jobs always run from `main` branch
- Heartbeat checks include git fetch and cron status verification