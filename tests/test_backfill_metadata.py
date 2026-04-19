#!/usr/bin/env python3
"""
Unit tests for V2 Phase 1.4 — legacy position metadata backfill.

Covers:
- backfill_leg pure function: market-source fields, auto_trades fallback,
  existing-values-preserved contract, missing sources
- run_backfill integration: dry-run produces no writes, --apply persists,
  trade_history mirroring, API fetch batching, idempotency on rerun
- _is_missing semantics (None, '', [], absent)
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import backfill_position_metadata as bf
from scripts.backfill_position_metadata import (
    backfill_leg,
    run_backfill,
    _is_missing,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ms_from_now(delta: timedelta) -> int:
    return int((datetime.now(timezone.utc).timestamp() + delta.total_seconds()) * 1000)


def _minimal_leg(**overrides):
    """A legacy position: no question/category/close_time_ms/etc."""
    base = {
        'trade_id': 1,
        'market_id': 'mkt_A',
        'outcome': 'YES',
        'amount': 5.0,
        'probability': 0.5,
        'entry_probability': 0.5,
        'status': 'OPEN',
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _market(**overrides):
    base = {
        'question': 'Will X happen by Q2?',
        'category': 'economics',
        'closeTime': _ms_from_now(timedelta(days=10)),
        'totalLiquidity': 500,
        'uniqueBettorCount': 20,
    }
    base.update(overrides)
    return base


# ── _is_missing ──────────────────────────────────────────────────────────────

class TestIsMissing(unittest.TestCase):
    def test_none_is_missing(self):
        self.assertTrue(_is_missing(None))

    def test_empty_string_is_missing(self):
        self.assertTrue(_is_missing(''))

    def test_empty_list_is_missing(self):
        self.assertTrue(_is_missing([]))

    def test_empty_dict_is_missing(self):
        self.assertTrue(_is_missing({}))

    def test_zero_is_not_missing(self):
        # 0 is a legitimate value (e.g. zero liquidity, price). Only actual
        # absence counts as missing.
        self.assertFalse(_is_missing(0))
        self.assertFalse(_is_missing(0.0))

    def test_false_is_not_missing(self):
        # False is the value a boolean field can actually carry.
        self.assertFalse(_is_missing(False))

    def test_populated_values_not_missing(self):
        self.assertFalse(_is_missing('text'))
        self.assertFalse(_is_missing(['item']))
        self.assertFalse(_is_missing({'k': 'v'}))


# ── backfill_leg ─────────────────────────────────────────────────────────────

class TestBackfillLeg(unittest.TestCase):
    def test_market_provides_question_category_and_close_time(self):
        leg = _minimal_leg()
        written = backfill_leg(leg, _market(), None)
        self.assertEqual(leg['question'], 'Will X happen by Q2?')
        self.assertEqual(leg['category'], 'economics')
        self.assertIn('close_time_ms', leg)
        # Derived fields present
        self.assertIn(leg['term'], ('short', 'medium', 'long'))
        self.assertIn(leg['position_class'], ('short', 'medium', 'long_or_uncertain'))
        self.assertIn('close_time_ms', written)

    def test_existing_values_are_preserved(self):
        # Backfill must NEVER overwrite a non-empty field on the leg.
        leg = _minimal_leg(question='EXISTING Q', category='crypto')
        backfill_leg(leg, _market(), None)
        self.assertEqual(leg['question'], 'EXISTING Q')
        self.assertEqual(leg['category'], 'crypto')

    def test_auto_trades_fills_strategies_and_ev(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        leg = _minimal_leg(timestamp=now_iso)
        trade_record = {
            'market_id': 'mkt_A',
            'timestamp': now_iso,   # exact match with leg
            'strategies': ['probability_bias'],
            'estimated_ev': 1.23,
            'confidence': 0.67,
        }
        backfill_leg(leg, None, [trade_record])
        self.assertEqual(leg['strategies'], ['probability_bias'])
        self.assertEqual(leg['estimated_ev'], 1.23)
        self.assertEqual(leg['confidence'], 0.67)

    def test_auto_trades_fallback_question_when_market_missing(self):
        leg = _minimal_leg()
        # question is market-level — any record supplies it regardless
        # of timestamp, so this intentionally has no timestamp match.
        backfill_leg(leg, None, [{'question': 'From trades log'}])
        self.assertEqual(leg['question'], 'From trades log')

    def test_market_question_beats_trade_question(self):
        # Priority: API data > auto_trades. The market API reflects the
        # current canonical question; auto_trades is a historical snapshot.
        leg = _minimal_leg()
        backfill_leg(leg,
                     _market(question='API question'),
                     [{'question': 'Trade log question'}])
        self.assertEqual(leg['question'], 'API question')

    def test_nothing_written_when_both_sources_empty(self):
        leg = _minimal_leg()
        written = backfill_leg(leg, None, None)
        self.assertEqual(written, {})
        self.assertNotIn('question', leg)
        self.assertNotIn('close_time_ms', leg)

    def test_derived_position_class_for_close_in_3_days_is_short(self):
        leg = _minimal_leg()
        backfill_leg(leg, _market(closeTime=_ms_from_now(timedelta(days=3))), None)
        self.assertEqual(leg['term'], 'short')
        self.assertEqual(leg['position_class'], 'short')

    def test_derived_position_class_for_close_in_60_days_is_long_or_uncertain(self):
        leg = _minimal_leg()
        backfill_leg(leg, _market(closeTime=_ms_from_now(timedelta(days=60))), None)
        self.assertEqual(leg['term'], 'long')
        self.assertEqual(leg['position_class'], 'long_or_uncertain')

    def test_leg_level_backfill_skipped_when_no_timestamp_match(self):
        # Reviewer fix (Medium): if the leg's timestamp is far from every
        # auto_trades record, we MUST NOT apply that record's strategies /
        # estimated_ev / confidence — those are stamped at trade time.
        # Only market-level fallbacks (question) are still applied.
        leg_ts = datetime(2026, 4, 19, tzinfo=timezone.utc).isoformat()
        rec_ts = datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()  # >1y off
        leg = _minimal_leg(timestamp=leg_ts)
        backfill_leg(leg, None, [{
            'market_id': 'mkt_A', 'timestamp': rec_ts,
            'strategies': ['WRONG'], 'estimated_ev': 99.0, 'confidence': 0.99,
            'question': 'question is ok',
        }])
        # question is market-level → filled
        self.assertEqual(leg.get('question'), 'question is ok')
        # Leg-level fields must stay missing — no safe attribution
        self.assertNotIn('strategies', leg)
        self.assertNotIn('estimated_ev', leg)
        self.assertNotIn('confidence', leg)


# ── run_backfill integration ─────────────────────────────────────────────────

class TestRunBackfill(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpdir.name)
        self.state_path  = tmp / 'paper_trading_state.json'
        self.trades_path = tmp / 'auto_trades.json'
        self._patchers = [
            patch.object(bf, '_STATE_PATH', self.state_path),
            patch.object(bf, '_AUTO_TRADES_PATH', self.trades_path),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmpdir.cleanup()

    def _write_state(self, positions, trade_history=None):
        with open(self.state_path, 'w') as f:
            json.dump({
                'balance': 100.0,
                'positions': positions,
                'trade_history': trade_history or [],
            }, f)

    def _write_trades(self, trades):
        with open(self.trades_path, 'w') as f:
            json.dump({'total_trades': len(trades), 'trades': trades}, f)

    def _read_state(self):
        with open(self.state_path) as f:
            return json.load(f)

    def test_dry_run_counts_but_does_not_write(self):
        self._write_state({'mkt_A': [_minimal_leg(market_id='mkt_A')]})

        counts = run_backfill(
            fetch_market=lambda _id: _market(),
            apply_changes=False,
        )
        self.assertGreater(counts['fields_written'], 0)

        # State file on disk must be unchanged — no question/category/etc.
        leg = self._read_state()['positions']['mkt_A'][0]
        self.assertNotIn('question', leg)
        self.assertNotIn('close_time_ms', leg)

    def test_apply_persists_all_derived_fields(self):
        self._write_state({'mkt_A': [_minimal_leg(market_id='mkt_A')]})

        counts = run_backfill(
            fetch_market=lambda _id: _market(),
            apply_changes=True,
        )
        self.assertGreater(counts['fields_written'], 0)

        leg = self._read_state()['positions']['mkt_A'][0]
        for expected in ('question', 'category', 'close_time_ms',
                         'term', 'resolvability', 'position_class'):
            self.assertIn(expected, leg, f"{expected} missing after apply")

    def test_trade_history_mirrors_backfill(self):
        legs = [_minimal_leg(market_id='mkt_A', trade_id=42)]
        history = [{'trade_id': 42, 'market_id': 'mkt_A', 'status': 'OPEN'}]
        self._write_state({'mkt_A': legs}, trade_history=history)

        run_backfill(fetch_market=lambda _id: _market(), apply_changes=True)

        state = self._read_state()
        th_entry = state['trade_history'][0]
        self.assertEqual(th_entry['question'], 'Will X happen by Q2?')
        self.assertEqual(th_entry['category'], 'economics')

    def test_api_failure_does_not_crash_and_still_uses_auto_trades(self):
        leg_ts = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc).isoformat()
        self._write_state({'mkt_A': [
            _minimal_leg(market_id='mkt_A', timestamp=leg_ts)
        ]})
        self._write_trades([{
            'market_id': 'mkt_A', 'timestamp': leg_ts,   # matches leg
            'strategies': ['volume_spike'], 'estimated_ev': 0.50,
            'question': 'Fallback question',
        }])

        def fetch(_id):
            raise RuntimeError('API down')

        counts = run_backfill(fetch_market=fetch, apply_changes=True)
        self.assertEqual(counts['api_failed'], 1)

        leg = self._read_state()['positions']['mkt_A'][0]
        # Fallback values from auto_trades applied
        self.assertEqual(leg['strategies'], ['volume_spike'])
        self.assertEqual(leg['estimated_ev'], 0.50)
        self.assertEqual(leg['question'], 'Fallback question')
        # Classification fields still missing (no market) — that's the
        # honest outcome, not a silently-wrong default.
        self.assertNotIn('close_time_ms', leg)

    def test_rerun_is_idempotent(self):
        self._write_state({'mkt_A': [_minimal_leg(market_id='mkt_A')]})

        first = run_backfill(fetch_market=lambda _id: _market(), apply_changes=True)
        second = run_backfill(fetch_market=lambda _id: _market(), apply_changes=True)

        self.assertGreater(first['fields_written'], 0)
        self.assertEqual(second['fields_written'], 0,
                         "second run must write nothing — all fields already present")

    def test_leg_matches_closest_timestamp_record(self):
        # Reviewer fix: leg-level fields (strategies / EV / confidence) are
        # stamped at trade time, so the correct record for a given leg is
        # the one whose timestamp matches. Here a leg placed on 2026-04-01
        # must match the 2026-04-01 record, not the 2026-03-01 one — even
        # if both records exist for this market.
        apr_1 = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        mar_1 = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
        self._write_state({'mkt_A': [
            _minimal_leg(market_id='mkt_A', timestamp=_iso(apr_1))
        ]})
        self._write_trades([
            {'market_id': 'mkt_A', 'timestamp': _iso(mar_1),
             'strategies': ['old_strat'], 'estimated_ev': 0.10},
            {'market_id': 'mkt_A', 'timestamp': _iso(apr_1),
             'strategies': ['new_strat'], 'estimated_ev': 0.50},
        ])

        run_backfill(fetch_market=lambda _id: None, apply_changes=True)

        leg = self._read_state()['positions']['mkt_A'][0]
        self.assertEqual(leg['strategies'], ['new_strat'])
        self.assertEqual(leg['estimated_ev'], 0.50)

    def test_multiple_legs_same_market_each_keep_own_attribution(self):
        # Reviewer's exact reproducer: two OPEN legs on the same market,
        # entered at different times. Before the fix, both legs received
        # the NEWEST auto_trades record, silently overwriting the older
        # leg's real strategies/EV. After the fix, each leg must match
        # its own record by timestamp.
        mar_1 = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
        apr_1 = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        self._write_state({'mkt_A': [
            _minimal_leg(market_id='mkt_A', trade_id=1,
                         timestamp=_iso(mar_1)),
            _minimal_leg(market_id='mkt_A', trade_id=2,
                         timestamp=_iso(apr_1)),
        ]})
        self._write_trades([
            {'market_id': 'mkt_A', 'timestamp': _iso(mar_1),
             'strategies': ['old_strat'], 'estimated_ev': 0.10,
             'confidence': 0.60},
            {'market_id': 'mkt_A', 'timestamp': _iso(apr_1),
             'strategies': ['new_strat'], 'estimated_ev': 0.50,
             'confidence': 0.80},
        ])

        run_backfill(fetch_market=lambda _id: None, apply_changes=True)

        legs = self._read_state()['positions']['mkt_A']
        by_tid = {l['trade_id']: l for l in legs}
        self.assertEqual(by_tid[1]['strategies'], ['old_strat'])
        self.assertEqual(by_tid[1]['estimated_ev'], 0.10)
        self.assertEqual(by_tid[1]['confidence'], 0.60)
        self.assertEqual(by_tid[2]['strategies'], ['new_strat'])
        self.assertEqual(by_tid[2]['estimated_ev'], 0.50)
        self.assertEqual(by_tid[2]['confidence'], 0.80)

    def test_leg_with_no_timestamp_match_keeps_leg_level_fields_empty(self):
        # If the closest record is still far from the leg's timestamp
        # (outside tolerance), we must NOT attribute — the leg stays
        # missing strategies/EV rather than silently grabbing a stale one.
        leg_ts = datetime(2026, 4, 19, tzinfo=timezone.utc)
        ancient = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self._write_state({'mkt_A': [
            _minimal_leg(market_id='mkt_A', timestamp=_iso(leg_ts))
        ]})
        self._write_trades([
            {'market_id': 'mkt_A', 'timestamp': _iso(ancient),
             'strategies': ['stale'], 'estimated_ev': 99.0,
             'confidence': 0.99, 'question': 'market-level is fine'},
        ])

        run_backfill(fetch_market=lambda _id: None, apply_changes=True)

        leg = self._read_state()['positions']['mkt_A'][0]
        # Market-level field still fills from the ancient record
        self.assertEqual(leg.get('question'), 'market-level is fine')
        # Leg-level fields skipped — no wrong attribution
        self.assertNotIn('strategies', leg)
        self.assertNotIn('estimated_ev', leg)
        self.assertNotIn('confidence', leg)

    def test_trade_id_match_beats_timestamp_match(self):
        # If auto_trades happens to carry trade_id (future format), a
        # direct trade_id match wins over a timestamp-closest match.
        leg_ts = datetime(2026, 4, 19, tzinfo=timezone.utc)
        other_ts = datetime(2026, 4, 19, 0, 0, 30, tzinfo=timezone.utc)
        self._write_state({'mkt_A': [
            _minimal_leg(market_id='mkt_A', trade_id=42,
                         timestamp=_iso(leg_ts))
        ]})
        self._write_trades([
            # Timestamp-closest, but WRONG trade
            {'market_id': 'mkt_A', 'timestamp': _iso(other_ts),
             'trade_id': 99,
             'strategies': ['by_timestamp'], 'estimated_ev': 1.0},
            # Exact trade_id match with slightly farther timestamp
            {'market_id': 'mkt_A', 'timestamp': _iso(leg_ts),
             'trade_id': 42,
             'strategies': ['by_trade_id'], 'estimated_ev': 2.0},
        ])

        run_backfill(fetch_market=lambda _id: None, apply_changes=True)

        leg = self._read_state()['positions']['mkt_A'][0]
        self.assertEqual(leg['strategies'], ['by_trade_id'])
        self.assertEqual(leg['estimated_ev'], 2.0)

    def test_non_open_legs_not_enriched(self):
        # Terminal statuses (WIN/LOSE/CLOSED_EARLY/ABANDONED) are excluded
        # from slot accounting already — backfilling them adds noise
        # without any downstream benefit. Only OPEN and STRANDED qualify.
        self._write_state({
            'mkt_A': [
                _minimal_leg(market_id='mkt_A', trade_id=1, status='WIN'),
                _minimal_leg(market_id='mkt_A', trade_id=2, status='LOSE'),
            ],
        })
        counts = run_backfill(fetch_market=lambda _id: _market(),
                              apply_changes=True)
        self.assertEqual(counts['legs'], 0)

    def test_stranded_legs_are_enriched(self):
        self._write_state({'mkt_A': [
            _minimal_leg(market_id='mkt_A', status='STRANDED')
        ]})
        counts = run_backfill(fetch_market=lambda _id: _market(),
                              apply_changes=True)
        self.assertEqual(counts['legs'], 1)
        self.assertGreater(counts['fields_written'], 0)

    def test_batches_api_calls_per_market(self):
        # Two legs in one market → one API call, not two.
        self._write_state({'mkt_A': [
            _minimal_leg(market_id='mkt_A', trade_id=1),
            _minimal_leg(market_id='mkt_A', trade_id=2),
        ]})
        call_count = {'n': 0}
        def fetch(_id):
            call_count['n'] += 1
            return _market()

        run_backfill(fetch_market=fetch, apply_changes=True)
        self.assertEqual(call_count['n'], 1)


if __name__ == '__main__':
    unittest.main()
