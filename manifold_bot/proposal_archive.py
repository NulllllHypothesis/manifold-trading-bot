"""Weekly rotation of pending-proposal lists.

Shared helper used by `evaluate_positions.py` (pending_closes.json),
`position_swap_checker.py` (pending_swaps.json), and
`detect_stale_positions.py` (pending_abandons.json).

Why this exists:
  Operator feedback after seeing close proposal #152: the monotonically
  growing IDs become noise. Resetting weekly keeps each week's proposals
  numbered from 1, with old proposals preserved in archive files for
  audits.

Design:
  - Detect "new week" by comparing the ISO week of the most recent
    proposal's `proposed_at` with the current ISO week.
  - On rotation: write the entire pre-rotation list to
    `data/{archive_prefix}_archive_{YYYY-WW}.jsonl` (append mode) and
    return a new list containing only `status='pending'` entries
    renumbered with id starting at 1.
  - Stale statuses (expired / dismissed / approved) are dropped on
    rotation — the archive has them.

Trade-off:
  Active pending Telegram CTAs ("approve close 152") become stale
  after rotation since the IDs change. This is the cost of the weekly
  reset the operator asked for. Rotation should be timed near quiet
  hours so few live CTAs are in flight.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


def _iso_week_key(dt: datetime) -> str:
    """Return ISO year-week label like '2026-W19' for a datetime."""
    yr, wk, _ = dt.isocalendar()
    return f"{yr}-W{wk:02d}"


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string into an aware UTC datetime, or None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def rotate_if_new_week(
    pending: List[Dict],
    archive_dir: Path,
    archive_prefix: str,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """Weekly rotation of a pending-proposals list.

    Args:
        pending:         current list of proposal dicts (each must have
                         `proposed_at` and `status`).
        archive_dir:     directory where archive .jsonl files live.
        archive_prefix:  prefix for archive filenames, e.g. 'pending_closes'.
        now:             override for testing; defaults to current UTC time.

    Behavior:
        If the most recent proposed_at is in a different ISO week than
        `now`, write the FULL pre-rotation list to
        `archive_dir/{archive_prefix}_archive_{ISO_WEEK_OF_LATEST}.jsonl`
        (append mode), then return only `status=='pending'` entries
        renumbered with `id` starting at 1.

        If the list is empty, has no parseable timestamps, or is in the
        same ISO week as `now`: return `pending` unchanged.

        Caller is responsible for writing the returned list back to disk.
    """
    if not pending:
        return pending

    if now is None:
        now = datetime.now(timezone.utc)

    latest_dt: Optional[datetime] = None
    for p in pending:
        dt = _parse_dt(p.get('proposed_at'))
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt

    if latest_dt is None:
        return pending  # nothing parseable to compare against

    if _iso_week_key(latest_dt) == _iso_week_key(now):
        return pending

    # New week — archive the full pre-rotation list.
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{archive_prefix}_archive_{_iso_week_key(latest_dt)}.jsonl"
    with open(archive_path, 'a') as f:
        for p in pending:
            f.write(json.dumps(p) + '\n')

    # Keep only currently-pending entries, renumbered.
    active = [dict(p) for p in pending if p.get('status') == 'pending']
    for new_id, p in enumerate(active, start=1):
        p['id'] = new_id
    return active
