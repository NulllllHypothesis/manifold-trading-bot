"""
Paper Trading Bot for Manifold Markets
Simulates trading without using real play money.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .manifold_api import api_client
from .config import INITIAL_BALANCE, MIN_BET_AMOUNT, MAX_BET_AMOUNT

def _notify_resolution(market_id: str, question: str, outcome: str, our_bet: str, total_pnl: float, new_balance: float) -> None:
    """Send a Telegram notification when a market resolves. Never raises — resolution must not fail due to Telegram."""
    try:
        import sys, os
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from automation.send_telegram import send_message
        won = our_bet == outcome
        emoji = "✅" if won else "❌"
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        msg = (
            f"{emoji} *Market Resolved*\n"
            f"{question[:80]}\n\n"
            f"Resolved: *{outcome}* | Our bet: *{our_bet}*\n"
            f"P&L: *{pnl_str}* | Balance: ${new_balance:.2f}"
        )
        send_message(msg)
    except Exception:
        pass  # never block resolution

def _market_resolved_at_iso(market: Dict) -> Optional[str]:
    """Manifold's authoritative resolution time as ISO-8601 UTC, or None.

    The API reports ``resolutionTime`` in epoch **milliseconds** on resolved
    markets. Returns None when the field is missing or unparseable so the
    caller can fall back to detection time (now). Never raises — resolution
    must not fail because of a malformed timestamp.
    """
    try:
        ts_ms = market.get('resolutionTime')
        if ts_ms is None:
            return None
        return datetime.fromtimestamp(
            float(ts_ms) / 1000.0, tz=timezone.utc
        ).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# SQLite database — shared with calibration scripts
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "calibration.db"

# Trades placed on/after this date have ai_estimated_probability and estimated_ev
# stored correctly.  Rows before this date are excluded from EV-accuracy learning.
_POST_EV_FIX_DATE = "2026-04-07"


def _init_bet_outcomes_db() -> None:
    """Create bet_outcomes table if it doesn't exist, and run schema migrations."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bet_outcomes (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id                TEXT NOT NULL,
            our_recommendation       TEXT,    -- YES or NO (what we bet)
            amount                   REAL,    -- stake
            probability              REAL,    -- market prob when we placed the bet
            estimated_ev             REAL,    -- EV predicted at trade time (NULL = no AI estimate)
            ai_confidence            REAL,    -- AI confidence score (NULL = no AI)
            ai_estimated_probability REAL,    -- AI's true-prob estimate at trade time (NULL = no AI)
            strategies               TEXT,    -- JSON list of strategy names that triggered
            market_resolution        TEXT,    -- YES or NO (actual outcome)
            actual_pnl               REAL,    -- realized profit/loss
            ev_error                 REAL,    -- estimated_ev - actual_pnl; NULL when estimated_ev is NULL
            era                      TEXT,    -- 'post_ev_fix' or 'pre_ev_fix' (learning gate)
            resolved_at              TEXT     -- ISO timestamp
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bet_outcomes_market
        ON bet_outcomes(market_id)
    """)

    # Schema migrations — add columns that didn't exist in earlier versions.
    # ALTER TABLE ADD COLUMN raises OperationalError if the column already exists;
    # we catch and ignore so this function is safe to call repeatedly.
    for migration in [
        "ALTER TABLE bet_outcomes ADD COLUMN ai_estimated_probability REAL",
        "ALTER TABLE bet_outcomes ADD COLUMN era TEXT",
        "ALTER TABLE bet_outcomes ADD COLUMN category TEXT",
    ]:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists — no-op

    # Backfill era for rows that pre-date the EV-pipeline fix (identified by
    # NULL estimated_ev — all pre-fix trades lack both EV and AI probability).
    conn.execute("""
        UPDATE bet_outcomes SET era = 'pre_ev_fix'
        WHERE era IS NULL AND estimated_ev IS NULL
    """)

    conn.commit()
    conn.close()


def _write_bet_outcome(
    trade: Dict,
    market_resolution: str,
    actual_pnl: float,
    era: str = "post_ev_fix",
) -> None:
    """
    Append one resolved-trade record to bet_outcomes.

    Called by resolve_market() for every position that closes.
    The trade dict is the record originally written by place_paper_bet(),
    so it already carries estimated_ev, ai_confidence, ai_estimated_probability,
    and strategies if auto_trader passed them in.

    ev_error = estimated_ev - actual_pnl
      > 0 means AI overestimated the edge (predicted more profit than occurred)
      < 0 means AI underestimated (we made more than predicted)
      NULL when there was no AI estimate (confidence-scaled fallback)

    era: defaults to 'post_ev_fix' (EV pipeline deployed); callers that close
         a position before market resolution (early close / swap close) pass
         era='early_close' so the weekly EV audit can segment close-at-AMM
         outcomes from true-resolution outcomes. Existing pre-fix rows are
         backfilled to 'pre_ev_fix' in _init_bet_outcomes_db().

    resolved_at: taken from the trade record when present — the resolve paths
         stamp the authoritative ISO-8601 UTC resolution time (Manifold's
         resolutionTime when the caller has it) onto the trade *before*
         calling here, so bet_outcomes and trade_history agree. Falls back
         to detection time (now, UTC) for records without the field
         (early close, abandonment write-off, external callers).
    """
    try:
        estimated_ev   = trade.get("estimated_ev")
        ai_est_prob    = trade.get("ai_estimated_probability")
        ev_error       = (estimated_ev - actual_pnl) if estimated_ev is not None else None
        strategies_json = json.dumps(trade.get("strategies") or [])
        # Authoritative resolution time stamped on the trade by the resolve
        # paths (PR #42); detection time (now, UTC) only as fallback.
        resolved_at    = trade.get("resolved_at") or datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            INSERT INTO bet_outcomes
            (market_id, our_recommendation, amount, probability,
             estimated_ev, ai_confidence, ai_estimated_probability, strategies,
             market_resolution, actual_pnl, ev_error, era, resolved_at, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("market_id"),
            trade.get("outcome"),
            trade.get("amount"),
            trade.get("probability"),
            estimated_ev,
            trade.get("ai_confidence"),
            ai_est_prob,
            strategies_json,
            market_resolution,
            actual_pnl,
            ev_error,
            era,
            resolved_at,
            trade.get("category"),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        # Never let DB errors crash the paper trader
        print(f"[bet_outcomes] warning: could not write outcome for {trade.get('market_id')}: {e}")


class PaperTrader:
    """Paper trading simulation for Manifold Markets"""
    
    def __init__(self, initial_balance: float = INITIAL_BALANCE):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions = {}  # market_id -> position info
        self.trade_history = []
        self.performance_metrics = {}
        # List of capital-reset events. Each entry:
        #   {"started_at": iso8601, "topup_from": float, "topup_to": float, "note": str}
        # Used by daily_summary.py to compute the effective baseline — without
        # this, "Total Profit" would compare balance against the original
        # INITIAL_BALANCE and show nonsense like -$922 when a deliberate reset
        # was made. Persisted across save_state / load_state.
        self.capital_epochs: List[Dict] = []

        # Load/save state
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading_state.json")
        self.load_state()

    def load_state(self):
        """Load trading state from file"""
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                self.balance = state.get('balance', self.initial_balance)
                self.positions = state.get('positions', {})
                self.trade_history = state.get('trade_history', [])
                self.capital_epochs = state.get('capital_epochs', [])
                print(f"Loaded state: Balance=${self.balance:.2f}, {len(self.positions)} positions")
        except FileNotFoundError:
            print("No saved state found, starting fresh")

    def save_state(self):
        """Save trading state to file"""
        state = {
            'balance': self.balance,
            'positions': self.positions,
            'trade_history': self.trade_history,
            'capital_epochs': self.capital_epochs,
            'saved_at': datetime.now().isoformat()
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def effective_baseline(self) -> float:
        """Return the most recent capital-reset top-up value, else INITIAL_BALANCE.

        Used by daily_summary (and any other "total profit" calculation) to
        avoid comparing current balance against a baseline that was superseded
        by a deliberate capital reset.
        """
        return self.baseline_from_state(
            {"capital_epochs": self.capital_epochs},
            default=self.initial_balance,
        )

    @staticmethod
    def baseline_from_state(state: Dict, default: float = INITIAL_BALANCE) -> float:
        """Compute the effective baseline from a state dict (or anything with
        a ``capital_epochs`` key). Lets callers that already loaded state
        avoid a second disk read + PaperTrader instantiation, while keeping
        the baseline math in one place."""
        epochs = (state or {}).get("capital_epochs") or []
        if epochs:
            return float(epochs[-1].get("topup_to", default))
        return float(default)
    
    def place_paper_bet(self, market_id: str, outcome: str,
                       amount: float, probability: float,
                       estimated_ev: Optional[float] = None,
                       ai_confidence: Optional[float] = None,
                       ai_estimated_probability: Optional[float] = None,
                       strategies: Optional[List[str]] = None,
                       category: Optional[str] = None,
                       question: Optional[str] = None,
                       term: Optional[str] = None,
                       resolvability: Optional[str] = None,
                       position_class: Optional[str] = None,
                       close_time_ms: Optional[int] = None,
                       confidence: Optional[float] = None,
                       ai_status: Optional[str] = None,
                       inference_cost: Optional[float] = None) -> bool:
        """
        Place a paper trade (simulated bet).

        Args:
            market_id:               Manifold market ID
            outcome:                 "YES" or "NO"
            amount:                  Bet amount in dollars
            probability:             Current market probability (0–1)
            estimated_ev:            AI-estimated expected value at trade time (stored for calibration)
            ai_confidence:           AI confidence score (stored for calibration)
            ai_estimated_probability: AI's estimated true probability at trade time.
                                     Distinct from ai_confidence (certainty) — this is
                                     the probability estimate itself. Stored in bet_outcomes
                                     to enable AI calibration curve analysis.
            strategies:              List of strategy names that triggered this trade
            term:                    V2 Phase 2.1: 'short' / 'medium' / 'long' / 'unknown'
            resolvability:           V2 Phase 2.1: 'high' / 'medium' / 'low'
            position_class:          V2 Phase 2.1: class used for slot-cap enforcement
                                     ('short' / 'medium' / 'long_reliable' / 'long_risky')
            close_time_ms:           V2 Phase 2.1: Manifold closeTime in unix ms.
                                     Persisted so legacy positions can be re-classified
                                     if position_class is missing.
            inference_cost:          Real LLM spend (USD) for the AI analysis that
                                     backed this trade, computed from API-reported
                                     token usage x DeepSeek pricing (0.0 for local
                                     Ollama analyses). None when no AI ran or the
                                     cost is unknown. Additive/optional: the
                                     scorekeeper's manifold_bot adapter prefers
                                     this explicit field and falls back to a flat
                                     --inference-cost-per-ai-call estimate when
                                     it is None/absent.

        Returns:
            bool: True if trade successful
        """
        # Validate inputs
        if amount < MIN_BET_AMOUNT:
            print(f"Amount ${amount} below minimum ${MIN_BET_AMOUNT}")
            return False
        
        if amount > MAX_BET_AMOUNT:
            print(f"Amount ${amount} above maximum ${MAX_BET_AMOUNT}")
            return False
        
        if amount > self.balance:
            print(f"Insufficient balance: ${self.balance:.2f} < ${amount}")
            return False
        
        if outcome not in ["YES", "NO"]:
            print(f"Invalid outcome: {outcome}. Must be 'YES' or 'NO'")
            return False
        
        # Calculate expected value
        if outcome == "YES":
            payout = amount / probability if probability > 0 else 0
            profit_if_win = payout - amount
            profit_if_lose = -amount
        else:  # NO
            payout = amount / (1 - probability) if probability < 1 else 0
            profit_if_win = payout - amount
            profit_if_lose = -amount
        
        # Record trade
        trade = {
            'trade_id': len(self.trade_history) + 1,
            'timestamp': datetime.now().isoformat(),
            'market_id': market_id,
            'outcome': outcome,
            'amount': amount,
            'probability': probability,
            # Entry snapshot — used by position_swap_checker to measure drift
            'entry_probability': probability,
            'entry_confidence': ai_confidence,
            # Blended stat+AI confidence that gated the trade. Distinct from
            # ai_confidence (0 when AI didn't vote) and entry_confidence (legacy
            # alias for ai_confidence). Telegram portfolio reads this so "conf X%"
            # reflects the number the gate actually saw.
            'confidence': confidence,
            'payout_if_win': payout,
            'profit_if_win': profit_if_win,
            'profit_if_lose': profit_if_lose,
            'status': 'OPEN',
            # Calibration fields — stored at trade time, read back at resolution time
            'estimated_ev': estimated_ev,
            'ai_confidence': ai_confidence,
            'ai_estimated_probability': ai_estimated_probability,
            # ai_status at entry: not_run / no_result / skip / agree / disagree.
            # Kept so Telegram portfolio can distinguish "AI didn't vote" from
            # "AI confidence was 0", and post-hoc audits can tell which trades
            # the AI actually weighed in on.
            'ai_status': ai_status,
            # Real per-trade LLM spend in USD (see docstring). Stored even when
            # None so trade rows have a consistent shape; consumers must treat
            # None as "unknown — use an estimate", NOT as zero.
            'inference_cost': inference_cost,
            'strategies': strategies,
            # Category and question stored for exposure-cap counting and daily summary
            'category': category or 'other',
            'question': question or market_id,
            # V2 Phase 2.1 — position classification for per-class slot caps.
            # Persisted so _count_open_in_class() doesn't have to fall back to
            # on-the-fly classification (which misses closeTime and misclassifies
            # short/medium positions as unknown→long_risky).
            'term':           term,
            'resolvability':  resolvability,
            'position_class': position_class,
            'close_time_ms':  close_time_ms,
        }
        
        # Update balance and positions
        self.balance -= amount
        
        if market_id not in self.positions:
            self.positions[market_id] = []
        
        self.positions[market_id].append(trade)
        self.trade_history.append(trade)
        
        print(f"Paper trade placed: {outcome} ${amount} @ {probability:.1%}")
        print(f"New balance: ${self.balance:.2f}")
        
        self.save_state()
        return True
    
    def resolve_market(self, market_id: str, outcome: str, question: str = "",
                       resolved_at: Optional[str] = None):
        """
        Resolve a market and calculate P&L.

        Args:
            market_id:   Market to resolve
            outcome:     Actual outcome ("YES" or "NO")
            question:    Human-readable question text for Telegram notification (optional)
            resolved_at: ISO-8601 timestamp of when the market actually resolved
                         (Manifold's ``resolutionTime`` when the caller has it).
                         Defaults to now (UTC) — i.e. detection time.

        Every position that closes here gets an explicit ``resolved_at`` stamped
        on both the position record and its trade_history twin, so downstream
        consumers (e.g. the workbench scorekeeper adapter) no longer have to
        approximate resolution time from position-snapshot detection lag.

        Side-effect: appends one row per resolved position to the bet_outcomes
        SQLite table in data/calibration.db for EV calibration tracking.
        Sends a Telegram notification per resolved market (best-effort, never raises).
        """
        if market_id not in self.positions:
            print(f"No positions in market {market_id}")
            return

        _init_bet_outcomes_db()   # no-op if table already exists

        if resolved_at is None:
            resolved_at = datetime.now(timezone.utc).isoformat()

        positions = self.positions[market_id]
        total_pnl = 0
        updated_trade_ids = []

        for trade in positions:
            # Include STRANDED positions too — their market may have eventually
            # resolved, so we reconcile to the real outcome (Phase 2.4). OPEN
            # is the usual case; CLOSED_EARLY / WIN / LOSE / ABANDONED are
            # terminal and must be skipped.
            if trade['status'] not in ('OPEN', 'STRANDED'):
                continue

            if trade['outcome'] == outcome:
                # Winning trade
                profit = trade['profit_if_win']
                trade['status'] = 'WIN'
                trade['actual_outcome'] = outcome
                trade['profit'] = profit
                total_pnl += profit
                print(f"Trade {trade['trade_id']}: WIN +${profit:.2f}")
            else:
                # Losing trade
                profit = trade['profit_if_lose']
                trade['status'] = 'LOSE'
                trade['actual_outcome'] = outcome
                trade['profit'] = profit
                total_pnl += profit
                print(f"Trade {trade['trade_id']}: LOSE ${profit:.2f}")

            trade['resolved_at'] = resolved_at

            # Write outcome to SQLite for EV calibration tracking
            _write_bet_outcome(trade, market_resolution=outcome, actual_pnl=trade['profit'])

            updated_trade_ids.append(trade['trade_id'])

        # Also update trade_history to stay consistent
        for trade in self.trade_history:
            if trade['trade_id'] in updated_trade_ids:
                # Find the corresponding position to copy status
                for pos in positions:
                    if pos['trade_id'] == trade['trade_id']:
                        trade['status'] = pos['status']
                        if 'actual_outcome' in pos:
                            trade['actual_outcome'] = pos['actual_outcome']
                        if 'profit' in pos:
                            trade['profit'] = pos['profit']
                        if 'resolved_at' in pos:
                            trade['resolved_at'] = pos['resolved_at']
                        break
        
        # Update balance
        self.balance += total_pnl

        print(f"Market {market_id} resolved as {outcome}")
        print(f"Total P&L: ${total_pnl:.2f}")
        print(f"New balance: ${self.balance:.2f}")

        # Notify Telegram — use bet direction of first resolved trade as "our bet"
        if updated_trade_ids:
            our_bet = next(
                (p['outcome'] for p in positions if p.get('trade_id') in updated_trade_ids),
                "?"
            )
            _notify_resolution(
                market_id=market_id,
                question=question or market_id,
                outcome=outcome,
                our_bet=our_bet,
                total_pnl=total_pnl,
                new_balance=self.balance,
            )

        self.save_state()
    

    def resolve_market_mkt(self, market_id: str, resolution_prob: float, question: str = "",
                           resolved_at: Optional[str] = None):
        """
        Resolve a market that settled as MKT (to a specific probability).

        On Manifold, MKT means the market resolved to `resolutionProbability`
        instead of a binary YES/NO. Payout is proportional:
          - YES bettors get: amount × resolutionProb / entryProb
          - NO bettors get:  amount × (1 − resolutionProb) / (1 − entryProb)

        P&L = payout − amount (can be positive or negative).

        resolved_at: ISO-8601 timestamp of the actual resolution (Manifold's
        ``resolutionTime`` when available); defaults to now (UTC). Stamped on
        each closing position and its trade_history twin.
        """
        if market_id not in self.positions:
            print(f"No positions in market {market_id}")
            return

        _init_bet_outcomes_db()

        if resolved_at is None:
            resolved_at = datetime.now(timezone.utc).isoformat()

        positions = self.positions[market_id]
        total_pnl = 0
        updated_trade_ids = []

        for trade in positions:
            # Include STRANDED positions too — their market may have eventually
            # resolved, so we reconcile to the real outcome (Phase 2.4). OPEN
            # is the usual case; CLOSED_EARLY / WIN / LOSE / ABANDONED are
            # terminal and must be skipped.
            if trade['status'] not in ('OPEN', 'STRANDED'):
                continue

            amount = trade.get('amount', 0)
            entry  = trade.get('entry_probability') or trade.get('probability', 0.5)
            bet    = trade.get('outcome', 'YES')

            if bet == 'YES':
                payout = amount * resolution_prob / entry if entry > 0 else 0
            else:
                payout = amount * (1 - resolution_prob) / (1 - entry) if entry < 1 else 0

            profit = round(payout - amount, 4)
            trade['status'] = 'WIN' if profit >= 0 else 'LOSE'
            trade['actual_outcome'] = 'MKT'
            trade['profit'] = profit
            trade['resolved_at'] = resolved_at
            total_pnl += profit
            updated_trade_ids.append(trade['trade_id'])
            print(f"Trade {trade['trade_id']}: MKT@{resolution_prob:.0%} → ${profit:+.2f}")

            _write_bet_outcome(trade, market_resolution='MKT', actual_pnl=profit)

        for trade in self.trade_history:
            if trade['trade_id'] in updated_trade_ids:
                for pos in positions:
                    if pos['trade_id'] == trade['trade_id']:
                        trade['status'] = pos['status']
                        trade['actual_outcome'] = pos.get('actual_outcome')
                        trade['profit'] = pos.get('profit')
                        trade['resolved_at'] = pos.get('resolved_at')
                        break

        self.balance += total_pnl
        print(f"Market {market_id} resolved as MKT@{resolution_prob:.0%}, P&L: ${total_pnl:+.2f}, balance: ${self.balance:.2f}")

        if updated_trade_ids:
            our_bet = next(
                (p['outcome'] for p in positions if p.get('trade_id') in updated_trade_ids),
                "?"
            )
            _notify_resolution(
                market_id=market_id,
                question=question or market_id,
                outcome=f"MKT@{resolution_prob:.0%}",
                our_bet=our_bet,
                total_pnl=total_pnl,
                new_balance=self.balance,
            )

        self.save_state()

    def resolve_market_cancel(self, market_id: str, question: str = "",
                              resolved_at: Optional[str] = None):
        """
        Resolve a cancelled market — full refund of the bet amount.

        On Manifold, CANCEL means the market was voided and all bets are
        returned at face value. P&L = 0 for every position.

        resolved_at: ISO-8601 timestamp of the actual cancellation (Manifold's
        ``resolutionTime`` when available); defaults to now (UTC). Stamped on
        each closing position and its trade_history twin.
        """
        if market_id not in self.positions:
            print(f"No positions in market {market_id}")
            return

        _init_bet_outcomes_db()

        if resolved_at is None:
            resolved_at = datetime.now(timezone.utc).isoformat()

        positions = self.positions[market_id]
        updated_trade_ids = []

        for trade in positions:
            # Include STRANDED positions too — their market may have eventually
            # resolved, so we reconcile to the real outcome (Phase 2.4). OPEN
            # is the usual case; CLOSED_EARLY / WIN / LOSE / ABANDONED are
            # terminal and must be skipped.
            if trade['status'] not in ('OPEN', 'STRANDED'):
                continue

            trade['status'] = 'CANCELLED'
            trade['actual_outcome'] = 'CANCEL'
            trade['profit'] = 0.0
            trade['resolved_at'] = resolved_at
            updated_trade_ids.append(trade['trade_id'])
            print(f"Trade {trade['trade_id']}: CANCELLED (refund ${trade.get('amount', 0):.2f})")

            _write_bet_outcome(trade, market_resolution='CANCEL', actual_pnl=0.0)

        for trade in self.trade_history:
            if trade['trade_id'] in updated_trade_ids:
                for pos in positions:
                    if pos['trade_id'] == trade['trade_id']:
                        trade['status'] = pos['status']
                        trade['actual_outcome'] = pos.get('actual_outcome')
                        trade['profit'] = 0.0
                        trade['resolved_at'] = pos.get('resolved_at')
                        break

        # Balance unchanged — the original amount was already deducted at bet time,
        # but since P&L = 0, add back the amount to simulate the refund.
        refund = sum(t.get('amount', 0) for t in positions if t.get('trade_id') in updated_trade_ids)
        self.balance += refund
        print(f"Market {market_id} CANCELLED, refund ${refund:.2f}, balance: ${self.balance:.2f}")

        if updated_trade_ids:
            our_bet = next(
                (p['outcome'] for p in positions if p.get('trade_id') in updated_trade_ids),
                "?"
            )
            _notify_resolution(
                market_id=market_id,
                question=question or market_id,
                outcome="CANCEL",
                our_bet=our_bet,
                total_pnl=0.0,
                new_balance=self.balance,
            )

        self.save_state()

    def mark_market_abandoned(self, market_id: str, reason: str = '') -> Optional[float]:
        """
        Phase 2.4 — terminal write-off for a market that never resolved.

        Applied only when the market is 90+ days past its closeTime with no
        resolution from the creator. Every OPEN or STRANDED leg in the market
        is flipped to `status='ABANDONED'` with `profit = -amount` (full
        loss), the balance is reduced by the sum of amounts, and a row is
        written to bet_outcomes with era='write_off' so weekly EV accounting
        treats these as real realised losses (not as unresolved pending).

        Returns total loss booked (negative float), or None if the market
        has no terminally-eligible positions.
        """
        if market_id not in self.positions:
            print(f"No positions in market {market_id} to abandon")
            return None

        _init_bet_outcomes_db()
        positions = self.positions[market_id]
        total_loss = 0.0
        any_abandoned = False
        now_iso = datetime.now().isoformat()

        for trade in positions:
            # OPEN (detector never ran) and STRANDED (detector moved it
            # earlier) are both eligible. Terminal statuses must be skipped.
            if trade.get('status') not in ('OPEN', 'STRANDED'):
                continue

            amount = float(trade.get('amount') or 0)
            loss = -amount  # full write-off
            trade['status'] = 'ABANDONED'
            trade['actual_outcome'] = 'ABANDONED'
            trade['profit'] = round(loss, 4)
            trade['abandoned_at'] = now_iso
            if reason:
                trade['abandoned_reason'] = reason
            total_loss += loss
            any_abandoned = True

            _write_bet_outcome(
                trade,
                market_resolution='ABANDONED',
                actual_pnl=round(loss, 4),
                era='write_off',
            )
            print(f"Trade {trade.get('trade_id')}: ABANDONED -${amount:.2f}")

        if not any_abandoned:
            print(f"No open/stranded positions to abandon in {market_id}")
            return None

        # Sync trade_history records
        abandoned_ids = {t['trade_id'] for t in positions if t.get('status') == 'ABANDONED'}
        for th in self.trade_history:
            if th.get('trade_id') in abandoned_ids:
                for pos in positions:
                    if pos.get('trade_id') == th.get('trade_id'):
                        th['status'] = pos['status']
                        th['profit'] = pos['profit']
                        break

        self.balance += total_loss  # total_loss is negative
        print(f"Market {market_id} ABANDONED. Realised loss: ${total_loss:.2f}")
        print(f"New balance: ${self.balance:.2f}")
        self.save_state()
        return round(total_loss, 4)

    def close_position_early(self, market_id: str, current_prob: float,
                             close_context: Optional[Dict] = None) -> Optional[float]:
        """
        Close an open position early at the current market probability.

        Simulates selling the position back to the market at the current price.
        Return value is based on share value at current_prob vs entry_prob.

          YES position: current_value = amount * current_prob / entry_prob
          NO  position: current_value = amount * (1 - current_prob) / (1 - entry_prob)

        Args:
            close_context: optional dict describing WHY the close happened
                (position_score, close_reason, close_type='auto'|'manual_approval',
                 score components). Stamped onto each closed trade record so the
                learning loop can later compare close-time reasoning against the
                market's eventual resolution. None → nothing stamped (legacy
                callers unchanged).

        Returns:
            float: The realised P&L (current_value - amount), or None if no open position.
        """
        if market_id not in self.positions:
            print(f"No positions in market {market_id}")
            return None

        _init_bet_outcomes_db()

        positions = self.positions[market_id]
        total_pnl = 0.0
        any_closed = False

        for trade in positions:
            # Only OPEN positions can be closed early at an AMM price.
            # STRANDED markets are past closeTime with no live AMM — calling
            # close_position_early on them would realise a fake sale value.
            # They must wait for resolve_positions to reconcile against a
            # real resolution (or be written off via execute_abandon.py).
            if trade['status'] != 'OPEN':
                continue

            entry_prob = trade.get('entry_probability') or trade.get('probability', 0.5)
            amount = trade['amount']
            outcome = trade['outcome']

            if outcome == 'YES':
                ep = max(entry_prob, 0.001)
                current_value = amount * current_prob / ep
            else:  # NO
                ep = max(1.0 - entry_prob, 0.001)
                current_value = amount * (1.0 - current_prob) / ep

            pnl = current_value - amount
            trade['status'] = 'CLOSED_EARLY'
            trade['actual_outcome'] = 'CLOSED_EARLY'
            trade['profit'] = round(pnl, 4)
            if close_context:
                trade['close_context'] = close_context
            total_pnl += pnl
            any_closed = True

            _write_bet_outcome(
                trade,
                market_resolution='CLOSED_EARLY',
                actual_pnl=round(pnl, 4),
                era='early_close',
            )
            print(f"Trade {trade['trade_id']}: CLOSED_EARLY {pnl:+.2f}")

        if not any_closed:
            print(f"No open positions to close in {market_id}")
            return None

        # Sync trade_history records
        closed_ids = {t['trade_id'] for t in positions if t.get('status') == 'CLOSED_EARLY'}
        for th in self.trade_history:
            if th['trade_id'] in closed_ids:
                for pos in positions:
                    if pos['trade_id'] == th['trade_id']:
                        th['status'] = pos['status']
                        th['profit'] = pos['profit']
                        break

        self.balance += total_pnl
        print(f"Market {market_id} closed early. P&L: {total_pnl:+.2f}")
        print(f"New balance: ${self.balance:.2f}")
        self.save_state()
        return round(total_pnl, 4)

    def auto_resolve_markets(self):
        """Auto-check Manifold for resolved markets and update positions.

        Handles three resolution types:
          YES / NO  → binary win/loss (resolve_market)
          MKT       → proportional payout at resolutionProbability (resolve_market_mkt)
          CANCEL    → full refund (resolve_market_cancel)

        Passes the market's authoritative ``resolutionTime`` (epoch ms from the
        Manifold API) down as ``resolved_at`` so trade records carry the TRUE
        resolution time, not the time this monitor happened to notice it.
        Falls back to detection time inside resolve_market* when the API
        doesn't report one.
        """
        resolved_count = 0
        for market_id in list(self.positions.keys()):
            # Scan OPEN and STRANDED positions. STRANDED markets (Phase 2.4)
            # are past closeTime + grace with no resolution yet; if the creator
            # eventually resolves, this path reconciles them to WIN/LOSS using
            # real P&L. Pure resolution — no AMM-mark write. ABANDONED is
            # terminal and must be skipped.
            live_positions = [
                p for p in self.positions[market_id]
                if p.get('status') in ('OPEN', 'STRANDED')
            ]
            if not live_positions:
                continue
            try:
                market = api_client.get_market(market_id)
                if not market.get('isResolved', False):
                    continue

                resolution = market.get('resolution', '')
                q = market.get('question', market_id)
                resolved_at = _market_resolved_at_iso(market)

                if resolution in ('YES', 'NO'):
                    print(f"Auto-resolving {q[:50]}... as {resolution}")
                    self.resolve_market(market_id, resolution, question=q,
                                        resolved_at=resolved_at)
                    resolved_count += 1

                elif resolution == 'MKT':
                    res_prob = market.get('resolutionProbability')
                    if res_prob is not None:
                        print(f"Auto-resolving {q[:50]}... as MKT@{res_prob:.0%}")
                        self.resolve_market_mkt(market_id, float(res_prob), question=q,
                                                resolved_at=resolved_at)
                        resolved_count += 1
                    else:
                        print(f"MKT resolution for {market_id} but no resolutionProbability — skipping")

                elif resolution == 'CANCEL':
                    print(f"Auto-resolving {q[:50]}... as CANCEL (refund)")
                    self.resolve_market_cancel(market_id, question=q,
                                               resolved_at=resolved_at)
                    resolved_count += 1

                else:
                    print(f"Unknown resolution type '{resolution}' for {market_id} — skipping")

            except Exception as e:
                print(f"Error checking market {market_id}: {e}")

        if resolved_count:
            print(f"Auto-resolved {resolved_count} market(s)")
        else:
            print("No markets to auto-resolve")
        return resolved_count

    def get_performance_metrics(self) -> Dict:
        """Calculate performance metrics"""
        if not self.trade_history:
            return {}

        open_trades   = [t for t in self.trade_history if t.get('status') == 'OPEN']
        win_trades    = [t for t in self.trade_history if t.get('status') == 'WIN']
        lose_trades   = [t for t in self.trade_history if t.get('status') == 'LOSE']
        resolved_trades = win_trades + lose_trades

        if not resolved_trades:
            return {
                'total_trades': len(self.trade_history),
                'open_trades': len(open_trades),
                'current_balance': self.balance,
                'total_pnl': 0,
                'win_rate': 0,
            }

        total_pnl  = sum(t.get('profit', 0) for t in resolved_trades)
        win_rate   = len(win_trades) / len(resolved_trades)

        win_profits  = [t.get('profit', 0) for t in win_trades]
        lose_profits = [t.get('profit', 0) for t in lose_trades]

        avg_win  = sum(win_profits)  / len(win_profits)  if win_profits  else 0
        avg_loss = sum(lose_profits) / len(lose_profits) if lose_profits else 0

        lose_sum = sum(lose_profits)
        profit_factor = abs(sum(win_profits) / lose_sum) if lose_sum != 0 else float('inf')

        money_in_open = sum(t.get('amount', 0) for t in open_trades)

        potential_pnl = 0.0
        for trade in open_trades:
            prob = trade.get('probability', 0.5)
            amt  = trade.get('amount', 0)
            if trade.get('outcome') == 'YES':
                payout = amt / prob if prob > 0 else 0
            else:
                payout = amt / (1 - prob) if prob < 1 else 0
            potential_pnl += payout - amt

        self.performance_metrics = {
            'total_trades':       len(self.trade_history),
            'resolved_trades':    len(resolved_trades),
            'open_trades':        len(open_trades),
            'win_trades':         len(win_trades),
            'lose_trades':        len(lose_trades),
            'win_rate':           win_rate,
            'realized_pnl':       total_pnl,
            'current_balance':    self.balance,
            'initial_balance':    self.initial_balance,
            'net_return_pct':     (self.balance - self.initial_balance) / self.initial_balance * 100,
            'money_in_open_trades': money_in_open,
            'potential_pnl_open': potential_pnl,
            'total_exposure':     money_in_open + abs(total_pnl),
            'avg_win':            avg_win,
            'avg_loss':           avg_loss,
            'profit_factor':      profit_factor,
            'largest_win':        max(win_profits,  default=0),
            'largest_loss':       min(lose_profits, default=0),
        }

        return self.performance_metrics
    
    def print_portfolio_summary(self):
        """Print portfolio summary"""
        metrics = self.get_performance_metrics()
        
        print("\n" + "="*60)
        print("📊 PORTFOLIO SUMMARY")
        print("="*60)
