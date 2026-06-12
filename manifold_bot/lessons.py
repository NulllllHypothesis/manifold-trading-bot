"""
Lessons store — structured trade retros the bot learns from (workbench-s7fo).

Every position close/resolution writes one retro row per trade leg to
data/lessons.db (next to calibration.db):

    thesis, entry/exit probability, outcome, calibration error,
    and a deterministic one-line lesson.

The analysis layer (ai_analyzer) then injects an aggregate track-record
summary plus the most relevant recent lessons back into the LLM prompt,
so past performance shapes new judgments.

Design notes:
  - Lesson text is composed DETERMINISTICALLY (no LLM call) so retro writes
    are free, instant, and unit-testable. Claude remains the heavier
    reflection layer (periodic prompt-optimization PRs measured by the
    scorekeeper).
  - All writes are best-effort: callers wrap in try/except, and nothing in
    here should ever crash a resolution path.
  - calibration_error is SIGNED: ai_estimated_probability − realized
    probability-of-YES. Positive = the AI overestimated YES.

Debugging:
  - Inspect: sqlite3 data/lessons.db "SELECT resolved_at, our_bet, actual_pnl, lesson FROM retros ORDER BY id DESC LIMIT 10"
  - The store is derived data; deleting lessons.db only resets the bot's
    self-knowledge — it is rebuilt as future positions close.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "lessons.db"

# Lessons shown in the analysis prompt. Kept small so the prompt stays lean —
# the aggregate stats line carries most of the calibration signal.
MAX_PROMPT_LESSONS = 3

# Signed bias (in percentage points, toward our own bet) below which we call
# the bot "roughly calibrated" rather than over/underconfident.
_BIAS_DEADBAND_PP = 3.0


def _resolve_db_path(db_path) -> Path:
    return Path(db_path) if db_path is not None else _DB_PATH


def _init_lessons_db(db_path=None) -> None:
    """Create the retros table if needed. Safe to call repeatedly."""
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retros (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id                TEXT NOT NULL,
            question                 TEXT,
            category                 TEXT,
            our_bet                  TEXT,     -- YES or NO
            amount                   REAL,
            entry_probability        REAL,     -- market prob when we entered
            exit_probability         REAL,     -- realized prob-of-YES at close (1/0 binary, res prob MKT, AMM prob early close; NULL cancel/abandoned)
            ai_estimated_probability REAL,     -- our estimate at entry (NULL = no AI)
            ai_confidence            REAL,
            strategies               TEXT,     -- JSON list
            thesis                   TEXT,     -- AI reasoning at entry, or strategy fallback
            resolution_type          TEXT,     -- YES/NO/MKT/CANCEL/CLOSED_EARLY/ABANDONED
            won                      INTEGER,  -- actual_pnl > 0
            actual_pnl               REAL,
            calibration_error        REAL,     -- signed: ai_estimate − exit_probability
            lesson                   TEXT,     -- deterministic one-liner
            resolved_at              TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retros_category ON retros(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retros_resolved_at ON retros(resolved_at)")
    conn.commit()
    conn.close()


def _derive_exit_probability(market_resolution: str,
                             exit_probability: Optional[float]) -> Optional[float]:
    """Realized probability-of-YES implied by the resolution.

    Binary resolutions imply 1.0 / 0.0; MKT and CLOSED_EARLY must be passed
    in by the caller (resolution prob / AMM price); CANCEL and ABANDONED have
    no realized probability.
    """
    if exit_probability is not None:
        return float(exit_probability)
    if market_resolution == "YES":
        return 1.0
    if market_resolution == "NO":
        return 0.0
    return None


def _compose_lesson(our_bet: Optional[str],
                    category: Optional[str],
                    ai_estimated_probability: Optional[float],
                    exit_probability: Optional[float],
                    resolution_type: str,
                    actual_pnl: float) -> str:
    """Deterministic one-line lesson for a closed trade.

    No LLM in the loop: lessons must be free to write and reproducible in
    tests. The phrasing encodes the calibration verdict so that when the
    lesson is later injected into an analysis prompt, the model sees a
    concrete, directional takeaway rather than raw numbers.
    """
    cat = category or "other"
    bet = our_bet or "?"
    pnl_str = f"+${actual_pnl:.2f}" if actual_pnl >= 0 else f"-${abs(actual_pnl):.2f}"

    if resolution_type == "CANCEL":
        return (f"Market cancelled (full refund) — resolution risk is real in {cat} "
                f"markets; weigh creator reliability before entering.")

    if resolution_type == "ABANDONED":
        return (f"Market never resolved — wrote off {pnl_str}; avoid {cat} markets "
                f"with absentee creators or fuzzy resolution criteria.")

    won = actual_pnl > 0

    if ai_estimated_probability is None or exit_probability is None:
        verdict = "won" if won else "lost"
        return (f"No probability estimate recorded at entry — {verdict} {pnl_str} on "
                f"{cat} flying blind; stat-only entries have unmeasured calibration.")

    est = float(ai_estimated_probability)
    out = float(exit_probability)
    err_pp = (est - out) * 100.0                      # + = overestimated YES
    toward_bet_pp = err_pp if bet == "YES" else -err_pp  # + = too bullish on own bet

    action = (f"closed early for {pnl_str}" if resolution_type == "CLOSED_EARLY"
              else f"{'won' if won else 'lost'} {pnl_str}")

    if won and abs(err_pp) <= 15:
        return (f"Bet {bet} on {cat} and {action} — estimate {est:.0%} vs realized "
                f"{out:.0%} was close ({err_pp:+.0f}pp); this edge was real.")
    if won:
        return (f"Bet {bet} on {cat} and {action}, but the estimate missed by "
                f"{err_pp:+.0f}pp ({est:.0%} vs {out:.0%}) — right direction, "
                f"sloppy number.")
    if toward_bet_pp > 0:
        return (f"Bet {bet} on {cat} and {action} — estimate {est:.0%} vs realized "
                f"{out:.0%} ran {abs(toward_bet_pp):.0f}pp too favorable to own bet; "
                f"demand stronger evidence before fading the crowd.")
    return (f"Bet {bet} on {cat} and {action} even though the estimate "
            f"({est:.0%} vs realized {out:.0%}) was not far off — the edge was "
            f"too thin to pay for; require a bigger divergence.")


def write_retro(trade: Dict,
                market_resolution: str,
                actual_pnl: float,
                exit_probability: Optional[float] = None,
                db_path=None) -> bool:
    """Append one structured retro for a closing trade leg.

    Called (via paper_trader._write_bet_outcome) on EVERY close path:
    binary resolution, MKT, CANCEL, early close, swap close, abandonment.

    The trade dict is the record written by place_paper_bet(), so it carries
    entry probability, AI estimate/confidence, strategies, question, category
    and (post-s7fo) the thesis. Missing fields degrade gracefully.

    Returns True if a row was written, False on any failure (never raises).
    """
    try:
        path = _resolve_db_path(db_path)
        _init_lessons_db(path)

        exit_prob = _derive_exit_probability(market_resolution, exit_probability)
        ai_est = trade.get("ai_estimated_probability")
        calibration_error = (
            round(float(ai_est) - exit_prob, 4)
            if (ai_est is not None and exit_prob is not None) else None
        )

        strategies = trade.get("strategies") or []
        thesis = trade.get("thesis")
        if not thesis:
            thesis = (f"Strategies: {', '.join(strategies)}" if strategies
                      else "No thesis recorded at entry.")

        category = trade.get("category") or "other"
        lesson = _compose_lesson(
            our_bet=trade.get("outcome"),
            category=category,
            ai_estimated_probability=ai_est,
            exit_probability=exit_prob,
            resolution_type=market_resolution,
            actual_pnl=float(actual_pnl),
        )

        conn = sqlite3.connect(path)
        conn.execute("""
            INSERT INTO retros
            (market_id, question, category, our_bet, amount,
             entry_probability, exit_probability,
             ai_estimated_probability, ai_confidence, strategies, thesis,
             resolution_type, won, actual_pnl, calibration_error, lesson,
             resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("market_id") or "UNKNOWN",
            trade.get("question"),
            category,
            trade.get("outcome"),
            trade.get("amount"),
            trade.get("entry_probability") or trade.get("probability"),
            exit_prob,
            ai_est,
            trade.get("ai_confidence"),
            json.dumps(strategies),
            thesis,
            market_resolution,
            1 if float(actual_pnl) > 0 else 0,
            float(actual_pnl),
            calibration_error,
            lesson,
            datetime.now().isoformat(),
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        # Retro writes must never break a resolution path.
        print(f"[lessons] warning: could not write retro for "
              f"{trade.get('market_id') if isinstance(trade, dict) else trade}: {e}")
        return False


def aggregate_stats(db_path=None) -> Dict:
    """Aggregate calibration stats over all retros (CANCEL excluded — refunds
    carry no information about our judgment).

    Returns a dict with n=0 when the store is empty or unreadable.
    """
    empty = {
        "n": 0, "wins": 0, "losses": 0, "win_rate": None, "total_pnl": 0.0,
        "n_with_estimate": 0,
        "mean_signed_error_pp": None,     # + = overestimated YES on average
        "mean_abs_error_pp": None,
        "mean_error_toward_bet_pp": None, # + = too bullish on own bets
    }
    try:
        path = _resolve_db_path(db_path)
        if not Path(path).exists():
            return empty
        conn = sqlite3.connect(path)
        rows = conn.execute("""
            SELECT our_bet, won, actual_pnl, calibration_error
            FROM retros WHERE resolution_type != 'CANCEL'
        """).fetchall()
        conn.close()
    except Exception:
        return empty

    if not rows:
        return empty

    n = len(rows)
    wins = sum(1 for r in rows if r[1])
    total_pnl = sum(float(r[2] or 0.0) for r in rows)
    errs = [(r[0], float(r[3])) for r in rows if r[3] is not None]

    stats = dict(empty)
    stats.update({
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": wins / n,
        "total_pnl": round(total_pnl, 2),
        "n_with_estimate": len(errs),
    })
    if errs:
        signed = [e * 100.0 for _, e in errs]
        toward = [(e if bet == "YES" else -e) * 100.0 for bet, e in errs]
        stats["mean_signed_error_pp"] = round(sum(signed) / len(signed), 1)
        stats["mean_abs_error_pp"] = round(sum(abs(x) for x in signed) / len(signed), 1)
        stats["mean_error_toward_bet_pp"] = round(sum(toward) / len(toward), 1)
    return stats


def recent_lessons(category: Optional[str] = None,
                   limit: int = MAX_PROMPT_LESSONS,
                   db_path=None) -> List[Dict]:
    """Most relevant recent lessons: same-category first (newest first),
    then backfill with the newest lessons from other categories."""
    try:
        path = _resolve_db_path(db_path)
        if not Path(path).exists():
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        picked: List[Dict] = []
        if category:
            picked = [dict(r) for r in conn.execute(
                "SELECT * FROM retros WHERE category = ? "
                "ORDER BY resolved_at DESC, id DESC LIMIT ?",
                (category, limit)).fetchall()]
        if len(picked) < limit:
            seen = tuple(r["id"] for r in picked) or (-1,)
            placeholders = ",".join("?" for _ in seen)
            picked += [dict(r) for r in conn.execute(
                f"SELECT * FROM retros WHERE id NOT IN ({placeholders}) "
                f"ORDER BY resolved_at DESC, id DESC LIMIT ?",
                (*seen, limit - len(picked))).fetchall()]
        conn.close()
        return picked
    except Exception:
        return []


def build_lessons_note(question: str = "",
                       category: Optional[str] = None,
                       db_path=None) -> str:
    """Build the track-record block injected into the analysis prompt.

    Returns "" when there is no history yet, so the prompt placeholder
    collapses cleanly. The block reads e.g.:

        Your own trading track record (24 resolved paper trades):
          Record: 11W/13L (46% win rate), net P&L -$8.40
          Calibration: your estimates averaged +9.3pp too favorable to your own
          bets (overconfident) — shade your estimate toward the market price.
        Lessons from your most relevant recent trades:
          - [sports | LOSS -$5.00] Bet YES on sports and lost ...
    """
    stats = aggregate_stats(db_path=db_path)
    if not stats["n"]:
        return ""

    lines = [
        f"Your own trading track record ({stats['n']} resolved paper trades):",
        (f"  Record: {stats['wins']}W/{stats['losses']}L "
         f"({stats['win_rate']:.0%} win rate), net P&L "
         f"{'+' if stats['total_pnl'] >= 0 else '-'}${abs(stats['total_pnl']):.2f}"),
    ]

    bias = stats.get("mean_error_toward_bet_pp")
    if bias is not None:
        if bias > _BIAS_DEADBAND_PP:
            lines.append(
                f"  Calibration: your estimates averaged {bias:+.1f}pp too favorable "
                f"to your own bets (overconfident) — shade your estimate toward the "
                f"market price unless the evidence is strong.")
        elif bias < -_BIAS_DEADBAND_PP:
            lines.append(
                f"  Calibration: your estimates averaged {bias:+.1f}pp — you have been "
                f"underconfident; when you see a real edge, trust it.")
        else:
            lines.append(
                f"  Calibration: roughly calibrated ({bias:+.1f}pp bias toward own "
                f"bets) — keep estimating independently.")
        if stats.get("mean_abs_error_pp") is not None:
            lines[-1] += f" Mean abs error: {stats['mean_abs_error_pp']:.1f}pp."

    lessons = recent_lessons(category=category, limit=MAX_PROMPT_LESSONS,
                             db_path=db_path)
    if lessons:
        lines.append("Lessons from your most relevant recent trades:")
        for l in lessons:
            pnl = float(l.get("actual_pnl") or 0.0)
            tag = "WIN" if l.get("won") else "LOSS"
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            lines.append(f"  - [{l.get('category') or 'other'} | {tag} {pnl_str}] "
                         f"{l.get('lesson') or ''}")

    lines.append("Apply these lessons when forming your estimate below.")
    return "\n".join(lines)
