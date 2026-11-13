#!/usr/bin/env python3
"""Compare two QQQ holdings lists.

A holdings list is ``{ticker: {"name": str, "weight": float, "shares": float}}``
where ``weight`` is the percentage of the fund (8.9 means 8.9%).

:func:`diff_holdings` is pure — no files, no clock — so it is easy to test and
the job module can stay thin.
"""

from typing import Any, Dict

# Weight moves smaller than this many percentage points are treated as noise.
DEFAULT_THRESHOLD = 0.01


def diff_holdings(old: Dict[str, Dict[str, Any]],
                  new: Dict[str, Dict[str, Any]],
                  threshold: float = DEFAULT_THRESHOLD) -> Dict[str, list]:
    """Return what was added, removed and re-weighted between two lists.

    ``added`` and ``removed`` are ordered by weight, largest first. ``changed``
    holds tickers present in both lists whose weight moved by at least
    ``threshold`` percentage points, ordered by size of the move, largest first;
    ``change`` is ``new_weight - old_weight`` (negative when the weight fell).
    Equal weights or moves are ordered by ticker.
    """
    added = [{"ticker": t, "name": h["name"], "weight": h["weight"]}
             for t, h in new.items() if t not in old]
    removed = [{"ticker": t, "name": h["name"], "weight": h["weight"]}
               for t, h in old.items() if t not in new]

    changed = []
    for ticker, holding in new.items():
        if ticker not in old:
            continue
        # Round away float noise (5.01 - 5.00 is 0.00999...) before comparing.
        change = round(holding["weight"] - old[ticker]["weight"], 4)
        if abs(change) >= threshold:
            changed.append({
                "ticker": ticker,
                "name": holding["name"],
                "old_weight": old[ticker]["weight"],
                "new_weight": holding["weight"],
                "change": change,
            })

    added.sort(key=lambda row: (-row["weight"], row["ticker"]))
    removed.sort(key=lambda row: (-row["weight"], row["ticker"]))
    changed.sort(key=lambda row: (-abs(row["change"]), row["ticker"]))
    return {"added": added, "removed": removed, "changed": changed}
