"""
Per-trade risk assessment + drawdown circuit breaker (workbench-g4wz).

Pure functions only — no I/O, no state. PaperTrader owns the persisted
risk_state dict (peak equity, breaker flag) in paper_trading_state.json;
this module owns the math so it can be unit-tested in isolation and reused
by auto_trader's pre-flight check without instantiating a trader.

Definitions used throughout:

  open exposure   = sum of `amount` over all positions with status == 'OPEN'
                    (cost basis of the live book; WIN/LOSS/CLOSED/ABANDONED
                    rows hold no capital and never count)
  equity          = cash balance + open exposure
                    (cost-basis equity — what the bankroll is worth if every
                    open position came back at exactly its stake)
  drawdown        = 1 - equity / peak_equity   (0 when at/above the peak)

The limits themselves live in config.py (MAX_TRADE_BALANCE_PCT,
MAX_TOTAL_EXPOSURE_PCT, MAX_MARKET_EXPOSURE_PCT, DRAWDOWN_HALT_PCT,
DRAWDOWN_RESUME_PCT) — single source of truth, snapshotted onto every trade
record so retros can correlate outcomes with the limits in force at the time.
"""

from typing import Dict, List, Optional, Tuple

from .config import (
    MAX_TRADE_BALANCE_PCT,
    MAX_TOTAL_EXPOSURE_PCT,
    MAX_MARKET_EXPOSURE_PCT,
    DRAWDOWN_HALT_PCT,
    DRAWDOWN_RESUME_PCT,
)


def open_exposure(positions: Dict[str, List[Dict]]) -> float:
    """Total stake locked in OPEN positions across all markets."""
    total = 0.0
    for pos_list in (positions or {}).values():
        for pos in pos_list:
            if pos.get('status') == 'OPEN':
                total += float(pos.get('amount') or 0.0)
    return total


def market_open_exposure(positions: Dict[str, List[Dict]], market_id: str) -> float:
    """Stake locked in OPEN positions in one specific market."""
    total = 0.0
    for pos in (positions or {}).get(market_id, []):
        if pos.get('status') == 'OPEN':
            total += float(pos.get('amount') or 0.0)
    return total


def equity(balance: float, positions: Dict[str, List[Dict]]) -> float:
    """Cost-basis equity: cash + stake currently deployed in OPEN positions."""
    return float(balance) + open_exposure(positions)


def assess_trade_risk(
    amount: float,
    balance: float,
    positions: Dict[str, List[Dict]],
    market_id: str,
) -> Tuple[Optional[str], Dict]:
    """
    Run the three per-trade risk gates against a proposed bet.

    Returns (rejection_reason, risk_fields):

      rejection_reason  None when the trade passes every gate, else one of:
                          'risk_size_pct'        amount > MAX_TRADE_BALANCE_PCT
                                                 of the cash balance
                          'risk_market_exposure' market stake incl. this trade
                                                 > MAX_MARKET_EXPOSURE_PCT of equity
                          'risk_total_exposure'  open stake incl. this trade
                                                 > MAX_TOTAL_EXPOSURE_PCT of equity
      risk_fields       dict snapshot of the inputs/derived numbers, suitable
                        for storing on the trade record (see place_paper_bet)
                        or logging on rejection. Always populated, pass or fail.

    Boundary semantics: limits are inclusive — a trade exactly AT a cap
    passes; only exceeding it rejects. All comparisons add a tiny epsilon so
    float noise (e.g. 0.1*balance computed two ways) can't reject a
    legitimately at-cap trade.
    """
    amount = float(amount)
    balance = float(balance)
    exposure_before = open_exposure(positions)
    market_exposure_before = market_open_exposure(positions, market_id)
    eq = balance + exposure_before
    open_count = sum(
        1 for pos_list in (positions or {}).values()
        for pos in pos_list if pos.get('status') == 'OPEN'
    )

    risk_fields = {
        'balance_before': round(balance, 4),
        'trade_pct_of_balance': round(amount / balance, 4) if balance > 0 else None,
        'open_positions_before': open_count,
        'open_exposure_before': round(exposure_before, 4),
        'total_exposure_pct_after': round((exposure_before + amount) / eq, 4) if eq > 0 else None,
        'market_exposure_before': round(market_exposure_before, 4),
        'market_exposure_pct_after': round((market_exposure_before + amount) / eq, 4) if eq > 0 else None,
        # Limits in force at trade time — config can change; the record
        # keeps what the gate actually compared against (mirrors the
        # inference_cost pattern: additive fields, consumers tolerate absence).
        'limits': {
            'max_trade_balance_pct': MAX_TRADE_BALANCE_PCT,
            'max_total_exposure_pct': MAX_TOTAL_EXPOSURE_PCT,
            'max_market_exposure_pct': MAX_MARKET_EXPOSURE_PCT,
            'drawdown_halt_pct': DRAWDOWN_HALT_PCT,
        },
    }

    _EPS = 1e-9

    if balance <= 0 or amount > balance * MAX_TRADE_BALANCE_PCT + _EPS:
        return 'risk_size_pct', risk_fields

    if market_exposure_before + amount > eq * MAX_MARKET_EXPOSURE_PCT + _EPS:
        return 'risk_market_exposure', risk_fields

    if exposure_before + amount > eq * MAX_TOTAL_EXPOSURE_PCT + _EPS:
        return 'risk_total_exposure', risk_fields

    return None, risk_fields


def drawdown_pct(equity_now: float, peak_equity: float) -> float:
    """Fractional drawdown from peak; 0.0 when at/above peak or peak invalid."""
    if peak_equity is None or peak_equity <= 0:
        return 0.0
    return max(0.0, 1.0 - float(equity_now) / float(peak_equity))


def evaluate_circuit_breaker(
    equity_now: float,
    peak_equity: float,
    already_tripped: bool,
) -> Tuple[bool, float]:
    """
    Hysteresis state machine for the halt-on-drawdown breaker.

    Returns (tripped, drawdown):
      - not tripped → trips when drawdown >= DRAWDOWN_HALT_PCT
      - tripped     → stays tripped until drawdown <= DRAWDOWN_RESUME_PCT
                      (recovery comes from resolutions/closes adding cash,
                      or a capital top-up which resets the peak entirely —
                      see PaperTrader.check_circuit_breaker)
    """
    dd = drawdown_pct(equity_now, peak_equity)
    if already_tripped:
        return (dd > DRAWDOWN_RESUME_PCT), dd
    return (dd >= DRAWDOWN_HALT_PCT), dd
