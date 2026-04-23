#!/usr/bin/env python3
"""
Smart Position Swap Checker.

Runs at :40 every hour (after trading at :20, before next research at :00).

Logic:
1. If positions are full (>= MAX_POSITIONS), scan ALL open positions for losers
   — any position where market has moved against us (unrealised_pnl < 0).
2. For each loser, find the best available new opportunity from market_research.json
   (confidence >= MIN_CONFIDENCE, AI not SKIP, positive EV, not already held).
   Each losing position gets paired with a unique replacement (no two proposals
   share the same open target).
3. Only propose a swap when new_opportunity_ev > abs(unrealised_loss) — i.e.
   the new trade is worth more than what we'd crystallise to enter it.
4. Write all qualifying proposals to pending_swaps.json as a numbered list.
5. Send one Telegram message per proposal: "Proposal #1 ... Reply 'approve swap 1'"
6. Checker will not re-propose while any non-expired pending entry remains.

Human approves or dismisses each proposal individually:
  approve:  python3 scripts/execute_swap.py --id 1
  dismiss:  python3 scripts/execute_swap.py --dismiss 2
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manifold_bot.manifold_api import api_client
from manifold_bot.config import MIN_CONFIDENCE, MIN_LIQUIDITY
from automation.send_telegram import send_telegram_message

# ── Paths ──────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).resolve().parent.parent
STATE_FILE = WORKSPACE / "manifold_bot" / "paper_trading_state.json"
RESEARCH_FILE = WORKSPACE / "market_research.json"
PENDING_SWAPS_FILE = WORKSPACE / "pending_swaps.json"

# ── Constants ──────────────────────────────────────────────────────────────────
MAX_POSITIONS = 10       # must match AutoTrader.max_positions
SWAP_EXPIRY_HOURS = 4    # proposals expire after this many hours

# Dismissal cooldown — once the operator dismisses a swap, suppress further
# swap proposals targeting the SAME close_market_id for this many hours. The
# replacement market (open target) can change between runs, but from the
# operator's perspective the annoying thing is the same losing position being
# poked at every hour. Cooldown on close_market_id matches that intent.
DISMISS_COOLDOWN_HOURS = 48

# Op5 (2026-04-11): minimum EV cushion beyond the realised loss that a swap
# must clear before we're willing to execute it.
#
# Why not zero: the 2026-04-11 11:40 run produced a technically-valid swap
# where close loss was $0.30 and open exec EV was $0.51 — net $0.21 of edge
# for crystallising a $0.30 loss and giving up an existing position. Op2
# correctly passed it (exec EV > loss) but the margin was too thin to be
# worth the churn, and we ended up dismissing it manually.
#
# $1.00 is chosen as one round of MIN_BET_AMOUNT — smaller and there's no
# actual breathing room above noise; larger and we'll reject many valid
# but modest opportunities. Tune after more production data.
SWAP_MIN_MARGIN = 1.00


# ── Core maths ─────────────────────────────────────────────────────────────────

def compute_unrealised_pnl(trade: Dict, current_prob: float) -> float:
    """
    Estimate the P&L if we closed this position NOW at current_prob.

    For a YES position entered at entry_prob p:
        current_value = amount * current_prob / p
    For a NO  position entered at entry_prob p:
        current_value = amount * (1 - current_prob) / (1 - p)

    unrealised_pnl = current_value - amount

    Returns 0.0 if entry_probability is missing or degenerate.
    """
    amount = trade.get('amount', 0)
    if amount <= 0:
        return 0.0

    entry_prob = trade.get('entry_probability') or trade.get('probability')
    if entry_prob is None:
        return 0.0

    outcome = trade.get('outcome', 'YES')
    if outcome == 'YES':
        ep = max(float(entry_prob), 0.001)
        current_value = amount * current_prob / ep
    else:  # NO
        ep = max(1.0 - float(entry_prob), 0.001)
        current_value = amount * (1.0 - current_prob) / ep

    return current_value - amount


# ── Data loaders ───────────────────────────────────────────────────────────────

def load_state() -> Dict:
    with open(STATE_FILE) as f:
        return json.load(f)


def load_research() -> Dict:
    with open(RESEARCH_FILE) as f:
        data = json.load(f)
    return data.get('latest', data)


# ── Position analysis ──────────────────────────────────────────────────────────

def find_all_weak_positions(positions: Dict) -> List[Dict]:
    """
    Scan all open positions, fetch current market probabilities via the API,
    and return every position with unrealised_pnl < 0, sorted worst-first.

    Each entry: {trade, market_id, question, current_prob, unrealised_pnl}
    """
    weak = []

    for market_id, pos_list in positions.items():
        open_trades = [p for p in pos_list if p.get('status') == 'OPEN']
        if not open_trades:
            continue

        try:
            market = api_client.get_market(market_id)
        except Exception as e:
            print(f"  Warning: could not fetch market {market_id}: {e}")
            continue

        if market.get('isResolved'):
            continue  # about to be auto-resolved; don't swap

        current_prob = float(market.get('probability', 0.5))
        question = market.get('question', market_id)

        for trade in open_trades:
            pnl = compute_unrealised_pnl(trade, current_prob)
            if pnl < 0:
                weak.append({
                    'trade': trade,
                    'market_id': market_id,
                    'question': question,
                    'current_prob': current_prob,
                    'unrealised_pnl': round(pnl, 4),
                })

    weak.sort(key=lambda x: x['unrealised_pnl'])  # worst first
    return weak


# ── Opportunity analysis ───────────────────────────────────────────────────────

def _swap_ev(rec: Dict) -> Optional[float]:
    """
    Return the EV figure to use for swap decisions.

    Swaps compare against a REAL unrealised loss on an open position, so they
    must use the EV computed at the actual executable stake (MAX_BET_AMOUNT),
    not the $25 cross-market ranking reference.

    Falls back to the legacy 'estimated_ev' field for pre-migration research
    files that don't yet have 'estimated_ev_exec'.
    """
    ev = rec.get('estimated_ev_exec')
    if ev is None:
        ev = rec.get('estimated_ev')
    return ev


def find_best_opportunity(research: Dict, existing_ids: set) -> Optional[Dict]:
    """
    Find the recommendation with the highest positive executable-size EV that:
    - Is not in existing_ids (already held OR already proposed as a target)
    - Has confidence >= MIN_CONFIDENCE
    - Was not vetoed by AI (SKIP)
    - Has a positive estimated_ev_exec (or legacy estimated_ev fallback)

    Ranking by exec EV instead of reference EV is the correct choice for swaps:
    reference EV at $25 overstates the attractiveness of any proposal under the
    current $5 MAX_BET_AMOUNT throttle by 5×.

    Returns None if no qualifying opportunity exists.
    """
    recs = research.get('recommendations', [])
    best = None
    best_ev = 0.0

    for rec in recs:
        if rec.get('ai_recommendation') == 'SKIP' or rec.get('ai_returned_skip'):
            continue
        if rec.get('recommendation') == 'SKIP':
            continue
        if rec.get('confidence', 0) < MIN_CONFIDENCE:
            continue
        if rec.get('market_id') in existing_ids:
            continue
        # Enforce the same liquidity floor as auto_trader.py — a swap target that
        # would be rejected by the trader at execution time is not a valid proposal.
        if rec.get('liquidity', 0) < MIN_LIQUIDITY:
            continue

        ev = _swap_ev(rec)
        if ev is None or ev <= 0:
            continue

        if ev > best_ev:
            best_ev = ev
            best = rec

    return best


# ── Pending swap helpers ───────────────────────────────────────────────────────

def load_pending_swaps() -> List[Dict]:
    """Load pending_swaps.json. Returns empty list if missing or corrupt."""
    if not PENDING_SWAPS_FILE.exists():
        return []
    try:
        with open(PENDING_SWAPS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_pending_swaps(swaps: List[Dict]) -> None:
    with open(PENDING_SWAPS_FILE, 'w') as f:
        json.dump(swaps, f, indent=2)


def _auto_dismiss_expired_swaps() -> None:
    """Mark any pending swap older than SWAP_EXPIRY_HOURS as 'expired'.

    Called at the start of run_swap_check() so stale proposals don't block
    new proposals. Previously expired swaps sat as status='pending' forever
    in the file and only the time-window check in has_active_pending_swaps()
    prevented them from blocking — but they still prevented proposals during
    the 4h window even when no human was available to approve.
    """
    swaps = load_pending_swaps()
    cutoff = datetime.now() - timedelta(hours=SWAP_EXPIRY_HOURS)
    changed = False
    for s in swaps:
        if s.get('status') != 'pending':
            continue
        try:
            proposed_at = datetime.fromisoformat(s.get('proposed_at', '2000-01-01'))
        except ValueError:
            continue
        if proposed_at < cutoff:
            s['status'] = 'expired'
            print(f"  Auto-expired swap #{s.get('id', '?')} (proposed {s.get('proposed_at', '?')[:19]})")
            changed = True
    if changed:
        save_pending_swaps(swaps)


def has_active_pending_swaps() -> bool:
    """Return True if any entry in pending_swaps.json is pending and not expired."""
    swaps = load_pending_swaps()
    cutoff = datetime.now() - timedelta(hours=SWAP_EXPIRY_HOURS)
    for s in swaps:
        if s.get('status') != 'pending':
            continue
        try:
            proposed_at = datetime.fromisoformat(s.get('proposed_at', '2000-01-01'))
        except ValueError:
            continue
        if proposed_at > cutoff:
            return True
    return False


def recently_dismissed_close_ids() -> set:
    """Return close_market_ids dismissed within DISMISS_COOLDOWN_HOURS.

    Suppresses re-proposing a swap for a position the operator already said
    "no" to. Falls back to proposed_at if dismissed_at is missing (older rows
    from before the execute_swap.py 'dismissed_at' field existed)."""
    swaps = load_pending_swaps()
    cutoff = datetime.now() - timedelta(hours=DISMISS_COOLDOWN_HOURS)
    dismissed = set()
    for s in swaps:
        if s.get('status') != 'dismissed':
            continue
        ts_raw = s.get('dismissed_at') or s.get('proposed_at', '2000-01-01')
        try:
            dismissed_at = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if dismissed_at > cutoff:
            close_id = s.get('close_market_id')
            if close_id:
                dismissed.add(close_id)
    return dismissed


def build_swap_entry(swap_id: int, weakest: Dict, best_rec: Dict) -> Dict:
    """Build a single swap proposal dict."""
    return {
        'id': swap_id,
        'close_market_id': weakest['market_id'],
        'close_question': weakest['question'],
        'close_outcome': weakest['trade']['outcome'],
        'close_entry_probability': (
            weakest['trade'].get('entry_probability')
            or weakest['trade'].get('probability')
        ),
        'close_current_probability': weakest['current_prob'],
        'close_unrealised_pnl': weakest['unrealised_pnl'],
        'open_market_id': best_rec['market_id'],
        'open_question': best_rec.get('question', ''),
        'open_recommendation': {k: best_rec[k] for k in best_rec if k != 'strategies'},
        'open_strategies': best_rec.get('strategies', []),
        'proposed_at': datetime.now().isoformat(),
        'status': 'pending',
    }


def format_telegram_message(swap: Dict) -> str:
    """Format one Telegram proposal message, clearly labelled with its ID."""
    swap_id = swap.get('id', '?')
    close_entry = swap.get('close_entry_probability', 0)
    close_now = swap.get('close_current_probability', 0)
    close_pnl = swap.get('close_unrealised_pnl', 0)
    open_rec = swap.get('open_recommendation', {})
    # Prefer exec EV — that's the number the swap decision was gated on.
    open_ev = _swap_ev(open_rec) or 0
    open_conf = open_rec.get('confidence', 0) or 0

    return (
        f"♻️ Swap Proposal #{swap_id}\n"
        f"Close: {swap['close_outcome']} on \"{swap['close_question'][:70]}\"\n"
        f"  Entered {close_entry:.0%}, now {close_now:.0%}, unrealised ${close_pnl:+.2f}\n"
        "\n"
        f"Open: {open_rec.get('recommendation', '?')} on \"{swap['open_question'][:70]}\"\n"
        f"  AI confidence {open_conf:.0%}, exec EV +${open_ev:.2f}\n"
        "\n"
        f"Reply \"approve swap {swap_id}\" to execute.\n"
        f"Reply \"dismiss swap {swap_id}\" to skip.\n"
        f"(Expires in {SWAP_EXPIRY_HOURS}h)"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def run_swap_check() -> int:
    """
    Core logic. Returns 0 on success (including no-swap cases), 1 on error.
    Separated from main() so tests can call it directly.
    """
    if not STATE_FILE.exists():
        print("No paper trading state found — skipping swap check.")
        return 0

    state = load_state()
    positions = state.get('positions', {})

    open_count = sum(
        1 for pos_list in positions.values()
        for p in pos_list if p.get('status') == 'OPEN'
    )
    print(f"Open positions: {open_count}/{MAX_POSITIONS}")

    if open_count < MAX_POSITIONS:
        print("Slots available — no swap needed.")
        return 0

    # Auto-dismiss expired pending swaps so they don't block new proposals.
    # Previously an expired swap sat as status='pending' forever, and while
    # has_active_pending_swaps() correctly ignored it after the 4h window,
    # ANY swap proposed during the first 4h blocked new proposals. By
    # auto-dismissing at the start of each check, a stale 4h+ swap gets
    # cleaned up and the loop can propose fresh swaps immediately.
    _auto_dismiss_expired_swaps()

    if has_active_pending_swaps():
        print("Active pending swaps already exist — skipping.")
        return 0

    if not RESEARCH_FILE.exists():
        print("No research file found — cannot find new opportunities.")
        return 0

    research = load_research()

    print("Evaluating open positions for swap candidates...")
    weak_positions = find_all_weak_positions(positions)

    if not weak_positions:
        print("All positions are profitable or neutral — no swap recommended.")
        return 0

    # Suppress positions the operator recently dismissed a swap for. Without
    # this filter, dismissing swap #N at 10:15 leads to swap #N+1 for the
    # exact same close_market_id at 11:15 — just a different open target.
    cooldown_ids = recently_dismissed_close_ids()
    if cooldown_ids:
        before = len(weak_positions)
        weak_positions = [w for w in weak_positions if w['market_id'] not in cooldown_ids]
        skipped = before - len(weak_positions)
        if skipped:
            print(
                f"  Skipping {skipped} position(s) in {DISMISS_COOLDOWN_HOURS}h "
                f"post-dismissal cooldown."
            )
    if not weak_positions:
        print("All losing positions are in post-dismissal cooldown — no swap proposed.")
        return 0

    print(f"Found {len(weak_positions)} losing position(s). Pairing with opportunities...")

    # IDs to exclude from being proposed as open targets — grows as we assign each one
    excluded_ids = set(positions.keys())

    proposals = []
    for weakest in weak_positions:
        best = find_best_opportunity(research, existing_ids=excluded_ids)
        if best is None:
            print(f"  No remaining opportunities for {weakest['market_id']} — stopping.")
            break

        loss = abs(weakest['unrealised_pnl'])
        # Gate on exec-size EV, not reference EV. A "+$4 EV at $25 stake"
        # proposal is actually "+$0.80 EV at $5 stake" — and that's what we'd
        # really capture if we executed the swap right now.
        best_ev = _swap_ev(best) or 0.0

        # Op5: require a minimum cushion above the loss. Technically valid
        # but tiny-margin swaps (e.g. $0.51 exec EV vs $0.30 loss = $0.21
        # net edge) aren't worth the churn of crystallising and re-opening.
        required = loss + SWAP_MIN_MARGIN
        if best_ev < required:
            print(
                f"  {weakest['market_id']}: best exec EV ${best_ev:.2f} below "
                f"${loss:.2f} loss + ${SWAP_MIN_MARGIN:.2f} margin = ${required:.2f} "
                f"— skipping this pair."
            )
            continue

        swap_id = len(proposals) + 1
        entry = build_swap_entry(swap_id, weakest, best)
        proposals.append(entry)
        excluded_ids.add(best['market_id'])  # don't reuse this target for another pair
        print(
            f"  Proposal #{swap_id}: close {weakest['market_id']} "
            f"(pnl ${weakest['unrealised_pnl']:+.2f}) → open {best['market_id']} "
            f"(exec EV ${best_ev:.2f})"
        )

    if not proposals:
        print("No qualifying swap pairs found.")
        return 0

    save_pending_swaps(proposals)

    for swap in proposals:
        msg = format_telegram_message(swap)
        send_telegram_message(msg)

    print(f"\n{len(proposals)} swap proposal(s) written to pending_swaps.json and sent to Telegram.")
    return 0


def main():
    return run_swap_check()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
