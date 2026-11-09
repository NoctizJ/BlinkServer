#!/usr/bin/env python3
"""Download the QQQ holdings list from Invesco.

:func:`fetch_holdings` returns ``(as_of, holdings)``:

  * ``as_of`` — the file's ``effectiveBusinessDate``, ``"YYYY-MM-DD"``: the last
    trading day the holdings reflect. The job uses it, not the clock, to decide
    whether the data is new. (``effectiveDate`` is not used: it can be a Sunday.)
  * ``holdings`` — ``{ticker: {"name": str, "weight": float, "shares": float}}``,
    ``weight`` being percent of the fund.

Only stock lines count as holdings. Invesco's file also lists cash, an index
future that rolls every quarter, and bookkeeping lines (some with no ticker or no
weight); those are skipped, or the report would announce a "removed" and an "added"
futures contract every quarter. A security type this module has never seen is kept,
so a genuinely new kind of holding shows up in the report instead of vanishing.

The endpoint is Invesco's own, undocumented. It answers HTTP 406 to the
``shareclasses/QQQ`` + ``idType=ticker`` form, so this uses QQQ's CUSIP.
"""

import datetime
import html
import logging
import math
import re
import time
from typing import Any, Dict, Tuple

import requests

logger = logging.getLogger(__name__)

QQQ_CUSIP = "46090E103"
INVESCO_URL = ("https://dng-api.invesco.com/cache/v1/accounts/en_US/"
               f"shareclasses/{QQQ_CUSIP}/holdings/fund")
INVESCO_PARAMS = {"idType": "cusip", "productType": "ETF"}
REQUEST_TIMEOUT = 20  # seconds
USER_AGENT = "BlinkServer-qqqholdings/1.0"

# securityTypeCode values that are not stocks: currency, index future,
# currency collateral, synthetic cash.
NON_STOCK_CODES = frozenset({"CURR", "IFUT", "CURRCOL", "SYN"})


class HoldingsFetchError(Exception):
    """The holdings could not be downloaded or understood."""


def _number(value: Any) -> float:
    """Parse a number sent as a number or a string; reject bools, NaN and infinity."""
    if isinstance(value, bool):
        raise ValueError(f"not a number: {value!r}")
    result = float(str(value))
    if not math.isfinite(result):
        raise ValueError(f"not a finite number: {value!r}")
    return result


def _normalize_date(value: Any) -> str:
    """Return the leading YYYY-MM-DD of ``value``; raise ValueError unless it is a real date."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value).strip())
    if not match:
        raise ValueError(f"unrecognized as-of date: {value!r}")
    try:
        datetime.date.fromisoformat(match.group(1))
    except ValueError:
        raise ValueError(f"not a calendar date: {value!r}") from None
    return match.group(1)


def parse_holdings(data: Any) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Turn Invesco's JSON into ``(as_of, holdings)``; raise ValueError if malformed.

    A ticker that appears twice has its weight and shares added together.
    """
    rows = data.get("holdings") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("response has no holdings")
    raw_date = data.get("effectiveBusinessDate") or data.get("effectiveDate")
    if not raw_date:
        raise ValueError("response has no as-of date")
    as_of = _normalize_date(raw_date)

    holdings: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"unexpected holdings row: {row!r}")
        ticker = str(row.get("ticker") or "").strip()
        weight = row.get("percentageOfTotalNetAssets")
        code = str(row.get("securityTypeCode") or "").upper()
        if not ticker or weight is None or code in NON_STOCK_CODES:
            continue
        weight = _number(weight)
        shares = _number(row.get("units") or 0)
        if ticker in holdings:
            holdings[ticker]["weight"] = round(holdings[ticker]["weight"] + weight, 6)
            holdings[ticker]["shares"] += shares
        else:
            holdings[ticker] = {
                "name": html.unescape(str(row.get("issuerName") or ticker)),
                "weight": weight,
                "shares": shares,
            }
    if not holdings:
        raise ValueError("response had rows but no stock holdings")
    return as_of, holdings


def fetch_holdings(session=None, attempts: int = 3, backoff: float = 2.0,
                   sleep=time.sleep) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Download and parse the holdings, retrying with doubling backoff.

    ``session`` and ``sleep`` are injectable for tests. Raises
    :class:`HoldingsFetchError` once every attempt has failed.
    """
    http = session or requests
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = http.get(INVESCO_URL, params=INVESCO_PARAMS,
                                headers={"User-Agent": USER_AGENT},
                                timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return parse_holdings(response.json())
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("QQQ holdings fetch attempt %d/%d failed: %s",
                           attempt, attempts, exc)
            if attempt < attempts:
                sleep(backoff * 2 ** (attempt - 1))
    raise HoldingsFetchError(
        f"could not fetch QQQ holdings after {attempts} attempts: {last_error}") from last_error
