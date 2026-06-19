# Changelog

This log summarises the project at the milestone level. For commit-level history, see `git log`. For per-PR detail and reviewer notes, see `PLAN.md` (V1 archive) and `PLAN_V2.md` (V2 work program).

---

## 2026-05: Parked

The project moved into freeze-and-polish state in May 2026. The decision rationale, what was built, and why active development stopped are summarised in the README. The codebase continues to run on a sandbox server as a live demo, but no new features are planned.

The freeze-and-polish work itself is preserved in this branch (`feature/portfolio-freeze`): README rewrite as a portfolio document, scaffolding moved into `meta/`, `CHANGELOG.md` and status notices added to `PLAN.md` / `PLAN_V2.md`.

---

## 2026-04 to 2026-05: V2 dead-loop diagnosis and AI-reliability work

V1 had built the observability dashboard, and the dashboard exposed three structural problems: the trade book was jammed, the AI layer was acting as a brake (vetoing legitimate trades on AI-side timeouts), and the calibration loop never accumulated outcomes because no trades resolved. PLAN_V2.md formalised this as the three dead loops:

```
1. AI failure -> treated as SKIP -> trader veto -> no trades
2. No trades  -> no resolutions  -> no bet_outcomes
3. No outcomes -> no weight updates -> no learning -> back to stat-only
```

V2 was the program of work to cut all three. The shipped pieces:

- **Phase 1 (PR #12, 2026-04-18)**: separate `ai_status` field (`not_run` / `no_result` / `skip` / `agree` / `disagree`); stat-derived probability fallback for EV; analyze-once AI cache.
- **Phase 2.1 (PR #12)**: position lifecycle classification (term, resolvability, position_class).
- **Phase 2.2 (PR #14, 2026-04-19)**: hourly position repricing into `data/calibration.db:position_snapshots`.
- **Phase 2.3 (PR #15)**: position evaluator with `position_score` and propose-only CLOSE proposals.
- **Phase 2.4 (PR #16)**: stale-position detection with STRANDED / ABANDONED transitions.
- **Phase 1.4 (PR #17)**: legacy position metadata backfill.
- **Phase 2.5 (PR #18, 2026-04-20)**: position management dashboard page.
- **Reactive filters (PRs #19, #20, 2026-04-20)**: unverifiable-market hard block; vanity-market compound gate.
- **Evaluator min-hold (PR #21, 2026-04-21)**: 2-hour minimum-hold guard against rounding-artifact closes.
- **Negative-EV trader gate (PR #22, 2026-04-21)**: `MIN_ESTIMATED_EV_FLOOR=0.0` enforced both pre-flight and at live recompute.
- **Reporting fixes (PR #23, 2026-04-23)**: `capital_epochs` first-class, blended-confidence persistence, daily-summary effective baseline, `/positions` AI status tag.
- **Dismiss cooldown (PR #24, 2026-04-23)**: 48-hour cooldown on close + swap proposals after operator dismissal.
- **Solo-momentum-low-resolvability block (PR #25, 2026-04-25)**: rejection of solo `probability_direction` in `resolvability=low` markets.
- **Honest stat-derived EV (PR #26, 2026-04-25)**: replaced the `confidence == P(YES)` shortcut with current-price-anchored bounded edge.
- **Asymmetric backtest weight clamp (PR #27, 2026-04-25)**: `MAX_BACKTEST_ONLY_WEIGHT=1.0` blocks up-weights from backtest until live samples confirm, with parallel mirroring in `compute_strategy_weights.py`.
- **Sports keyword expansion (PR #28, 2026-04-27)**: low-collision NBA/NFL/MLB/NHL team names added to category inference.
- **AI parse-failure fallback (PR #29, 2026-04-27)**: DeepSeek fallback also fires on Ollama parse-failures (not just transport failures); lazy-read of `DEEPSEEK_API_KEY`; observability flags split into `ollama_returned_none`, `ollama_parse_failed`, `deepseek_attempted`, `deepseek_returned_value`, `deepseek_saved_a_parse_failure`; switch from deprecated `deepseek-chat` to `deepseek-v4-flash`.
- **Ollama structured outputs (PR #30, 2026-04-27)**: `format=ANALYSIS_SCHEMA` constrained-decoding parameter on the Ollama leg; DeepSeek `response_format={"type":"json_object"}`. Verified post-merge: parse-success rate moved from 22% (lifetime) to 100% over the next 7.6 days, on the same model.
- **Silent-skip counters + capital epoch script + weekly proposal-counter rotation (PR #31, 2026-05-06)**: wired `kelly_no_edge`, `size_too_small`, `market_state_changed` rejection counters that previously vanished into `passed_filter - trades_executed`; new `scripts/start_new_capital_epoch.py` for prospective capital resets; `manifold_bot/proposal_archive.py` rotates pending close / swap / abandon files weekly to keep operator-facing IDs fresh.
- **Polymarket read-only ingestion (PR #32, 2026-05-06)**: `manifold_bot/polymarket_api.py`, `manifold_bot/markets_normalized.py`, `scripts/ingest_polymarket.py`, new `data/markets_normalized.db` separate from `calibration.db` and `market_snapshots.db` to keep the schema boundary clean. Cron entry at `:15`.
- **Solo-no-AI-confirmation gate (PR #33, 2026-05-06)**: rejection of solo `probability_direction` when `ai_status` is `not_run` or `no_result`.

Test count moved from 513 (Phase 1 baseline) to 867 over the V2 program.

---

## 2026-03 to 2026-04: V1 buildout

The original program of work was a one-month sprint to a working end-to-end system. The headline pieces in commit-time order:

- **2026-03-28**: initial commit, core paper-trading engine, Manifold API client, six trading strategies.
- **2026-03-30**: project reorganised into `automation/`, `tests/`, `scripts/`, `docs/`, with the OS-cron schedule defined and the OpenClaw scaffolding (`AGENTS.md`, daily memory files) put in place.
- **2026-03-30 to 2026-04-06**: cron reliability fixes (PR #1), GitHub Actions CI for PR test gating, security cleanup of leaked credentials, `CODEOWNERS` for auto-review.
- **2026-04-06**: distillation logging (`logs/llm_calls.jsonl`) merged via PR #5, with chain-of-thought extraction and log rotation.
- **2026-04-06 to 2026-04-12**: AI integration (`manifold_bot/ai_analyzer.py`) wired into research, batch analysis with per-market timeout enforcement, dual-back-end (Ollama then DeepSeek), category v2 taxonomy (`gaming` / `entertainment` / `business` added).
- **2026-04-08 to 2026-04-12**: calibration pipeline shipped (PR #6 onward): `bet_outcomes` table, EV calculation, weekly EV-accuracy report, `weekly_ev_report.py`, calibration-table generation from 1,000+ resolved markets, per-(category, bucket) bias modeling.
- **2026-04-10 to 2026-04-12**: smart position swap (PR #7+), Op1 to Op7.1 operational track including dual-field EV (reference vs executable size), research/trader observability counters, `days_before_close` on live snapshots, per-bucket `probability_bias` lookup, swap-margin buffer, noise-market filter with Unicode normalization.
- **2026-04-12 to 2026-04-15**: 15-page operations dashboard (`dashboard/`, `dashboard_api/`) covering portfolio, snapshots, swaps, weights, calibration, EV report, opportunities, news.
- **2026-04-15 to 2026-04-17**: dashboard exposed structural pipeline issues (jammed book, dead AI-vote loop, calibration not learning), motivating the V2 design document (`PLAN_V2.md`, written 2026-04-17).

Test count moved from 0 to 513 over V1.
