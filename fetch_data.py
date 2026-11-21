#!/usr/bin/env python3
"""
fetch_data.py - Market Conditions Dashboard data pipeline
==========================================================

What it does (runs once per weekday via GitHub Actions):
  1. Downloads daily QQQ closing prices. It tries three free sources in order:
       a) yfinance (Python library that reads Yahoo Finance; no key)
       b) Yahoo Finance's public chart endpoint, called directly (no key).
          Same data, different code path: helps when the yfinance library
          itself breaks after a Yahoo change, which is the usual failure.
       c) Stooq CSV. NOTE: since March 2026 Stooq requires a free API key
          (obtained through a CAPTCHA on stooq.com). This source is only
          tried if you store that key as a repository secret named
          STOOQ_APIKEY. Without it, it is skipped.
  2. Checks the data is sane (enough rows, recent, no zero/blank prices).
  3. Calculates the signals and the framework tier for every day.
  4. Runs a simple backtest: plain monthly DCA vs. the tiered approach.
  5. Writes everything to data/market.json.

Safety rule: if every source fails, or the data looks wrong, the script
does NOT touch the existing data/market.json. It logs a warning and exits,
so the website keeps showing the last good data (with its real date).

Run locally:   pip install yfinance pandas requests
               python fetch_data.py
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# =============================================================================
# EDITABLE SETTINGS - change the framework here. The widget reads these values
# from the JSON, so the page always shows the rules actually being used.
# =============================================================================

TICKER = "QQQ"            # Nasdaq-100 ETF
STOOQ_SYMBOL = "qqq.us"   # same fund on Stooq

INCLUDE_VIX = True        # also fetch the VIX (shown as context only; it does
VIX_TICKER = "^VIX"       # not affect the tier)

HISTORY_YEARS = 3         # years of daily history written to the JSON / chart.
                          # (Full history since 1999 is downloaded behind the
                          # scenes so the 200DMA and the all-time high are
                          # correct from the first day shown.)
MA_WINDOW = 200           # moving-average length in trading days

# --- Tier thresholds (all in percent) ---------------------------------------
MA_BAND_PCT = 5.0         # Tier 1 if price is within +/- this % of the 200DMA;
                          # Tier 0 if MORE than this % above it.
MA_DEEP_PCT = 10.0        # Tier 2 if 5-10% below the 200DMA;
                          # Tier 3 if MORE than this % below it.
DD_LEAN_PCT = 10.0        # Tier 2 if drawdown from the high is 10-20%
DD_AGGR_PCT = 20.0        # Tier 3 if drawdown is MORE than this
DIP_DAY_PCT = -1.5        # "Buy-the-dip day" flag if latest daily move <= this

TIERS = [
    {"id": 0, "name": "Baseline",   "multiplier": 1.0},
    {"id": 1, "name": "Accumulate", "multiplier": 1.5},
    {"id": 2, "name": "Lean In",    "multiplier": 2.0},
    {"id": 3, "name": "Aggressive", "multiplier": 3.0},
]

# --- Backtest ---------------------------------------------------------------
BACKTEST_ENABLED = True
BACKTEST_MONTHLY_AMOUNT = 1000.0   # hypothetical base contribution (illustrative)

# --- Data checks --------------------------------------------------------------
MIN_ROWS = MA_WINDOW + 260         # need at least ~1 year beyond the MA warm-up
MAX_STALENESS_DAYS = 7             # reject data whose last close is older than this

# --- Output -------------------------------------------------------------------
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "market.json"
HISTORY_START = date(1999, 3, 1)   # QQQ launched March 1999
MARKET_TZ = ZoneInfo("America/New_York")
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (market-conditions-dashboard; +github actions)"}

# =============================================================================

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("fetch_data")


def gh_warning(message: str) -> None:
    """Log a warning. The '::warning::' prefix makes GitHub Actions show it
    as a yellow annotation on the workflow run page."""
    log.warning(message)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}")


# -----------------------------------------------------------------------------
# 1. DATA SOURCES. Each returns a pandas Series of closing prices indexed by
#    date (oldest first). Closes are split-adjusted but NOT dividend-adjusted,
#    which keeps all three sources consistent with each other.
# -----------------------------------------------------------------------------

def fetch_yfinance(ticker: str, start: date) -> pd.Series:
    """Source A: the yfinance library."""
    import yfinance as yf  # imported here so the other sources work without it

    hist = yf.Ticker(ticker).history(start=start.isoformat(), interval="1d",
                                     auto_adjust=False, actions=False)
    if hist is None or hist.empty or "Close" not in hist:
        raise ValueError("yfinance returned no data")
    closes = hist["Close"].copy()
    closes.index = [d.date() for d in pd.to_datetime(closes.index)]
    return closes


def fetch_yahoo_chart(ticker: str, start: date) -> pd.Series:
    """Source B: Yahoo Finance's public chart JSON endpoint, called directly."""
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.now(timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(ticker)}"
    params = {"period1": period1, "period2": period2, "interval": "1d", "events": "split"}
    resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    tz = ZoneInfo(result["meta"].get("exchangeTimezoneName", "America/New_York"))
    dates = [datetime.fromtimestamp(t, tz).date() for t in stamps]
    return pd.Series(closes, index=dates, dtype="float64")


def fetch_stooq(symbol: str, start: date) -> pd.Series:
    """Source C: Stooq CSV (needs the free STOOQ_APIKEY secret since 2026)."""
    apikey = os.environ.get("STOOQ_APIKEY", "").strip()
    if not apikey:
        raise ValueError("skipped: no STOOQ_APIKEY secret set (Stooq now requires a free key)")
    params = {"s": symbol, "i": "d", "d1": start.strftime("%Y%m%d"),
              "d2": date.today().strftime("%Y%m%d"), "apikey": apikey}
    resp = requests.get("https://stooq.com/q/d/l/", params=params, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text.lower().startswith("date"):
        # Stooq answers with an HTML/text message (bad key, daily limit) instead of CSV
        raise ValueError(f"Stooq did not return CSV: {text[:120]!r}")
    df = pd.read_csv(io.StringIO(text))
    return pd.Series(df["Close"].values, index=pd.to_datetime(df["Date"]).dt.date, dtype="float64")


# -----------------------------------------------------------------------------
# 2. DATA CHECKS
# -----------------------------------------------------------------------------

def clean_and_validate(closes: pd.Series, min_rows: int, max_jump: float | None = 0.25) -> pd.Series:
    """Tidy the series and raise ValueError if it doesn't look trustworthy.
    max_jump: largest believable one-day move (0.25 = 25%). Use None to skip
    the check for series that genuinely jump a lot, like the VIX."""
    s = pd.Series(closes, dtype="float64")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s.dropna()
    s = drop_unfinished_session(s)

    if len(s) < min_rows:
        raise ValueError(f"only {len(s)} rows (need at least {min_rows})")
    if not all(math.isfinite(v) and v > 0 for v in s.values):
        raise ValueError("found zero, negative or non-numeric prices")
    age = (datetime.now(MARKET_TZ).date() - s.index[-1]).days
    if age > MAX_STALENESS_DAYS:
        raise ValueError(f"latest close is {s.index[-1]} ({age} days old)")
    # A one-day move of more than 25% in QQQ almost certainly means bad data
    # (e.g. an unadjusted split), not a real market move.
    # (The VIX skips this check: it can rise 25%+ in a single day.)
    worst = s.pct_change().abs().max()
    if max_jump is not None and worst > max_jump:
        raise ValueError(f"suspicious one-day jump of {worst:.0%} in the data")
    return s


def drop_unfinished_session(s: pd.Series) -> pd.Series:
    """If the script is run by hand during market hours, the last 'close' is
    really a live intraday price. Drop it so the dashboard only uses final
    closes. (The scheduled run happens after the close, so this rarely fires.)"""
    now = datetime.now(MARKET_TZ)
    if len(s) and s.index[-1] == now.date() and (now.hour, now.minute) < (16, 15):
        log.info("Dropping today's unfinished session (%s)", now.date())
        return s.iloc[:-1]
    return s


def get_prices(sources: list[tuple[str, callable]], min_rows: int,
               max_jump: float | None = 0.25) -> tuple[pd.Series, str]:
    """Try each source in order; return the first one that passes the checks."""
    errors = []
    for name, fetch in sources:
        try:
            s = clean_and_validate(fetch(), min_rows, max_jump)
            log.info("Using %s: %d rows, latest close %s = %.2f", name, len(s), s.index[-1], s.iloc[-1])
            return s, name
        except Exception as exc:  # any failure -> try the next source
            log.info("Source %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
    raise RuntimeError("all sources failed -> " + " | ".join(errors))


# -----------------------------------------------------------------------------
# 3. FRAMEWORK LOGIC
# -----------------------------------------------------------------------------

def tier_from_ma(pct_from_ma: float) -> int:
    """Tier implied by the % distance from the 200-day moving average."""
    if pct_from_ma > MA_BAND_PCT:
        return 0                      # more than 5% above
    if pct_from_ma >= -MA_BAND_PCT:
        return 1                      # within +/- 5%
    if pct_from_ma >= -MA_DEEP_PCT:
        return 2                      # 5-10% below
    return 3                          # more than 10% below


def tier_from_drawdown(drawdown_pct: float) -> int:
    """Tier implied by the drawdown from the high (drawdown_pct is positive,
    e.g. 12.0 means 12% below the high). Small drawdowns add nothing (0)."""
    if drawdown_pct > DD_AGGR_PCT:
        return 3
    if drawdown_pct >= DD_LEAN_PCT:
        return 2
    return 0


def compute_signals(closes: pd.Series) -> pd.DataFrame:
    """Add the 200DMA, distance, drawdown, daily move and tiers for every day."""
    df = pd.DataFrame({"close": closes})
    df["ma"] = df["close"].rolling(MA_WINDOW).mean()
    df["pct_from_ma"] = (df["close"] / df["ma"] - 1) * 100
    df["high"] = df["close"].cummax()                       # trailing all-time high
    df["drawdown"] = (1 - df["close"] / df["high"]) * 100   # positive number
    df["daily_change"] = df["close"].pct_change() * 100
    df = df.dropna(subset=["ma"]).copy()                     # MA needs 200 days of warm-up
    df["tier_ma"] = df["pct_from_ma"].apply(tier_from_ma)
    df["tier_dd"] = df["drawdown"].apply(tier_from_drawdown)
    df["tier"] = df[["tier_ma", "tier_dd"]].max(axis=1)     # more aggressive wins
    return df


def explain(row: pd.Series) -> str:
    """One plain-English sentence saying why today's tier applies."""
    t = TIERS[int(row["tier"])]
    mult = f"{t['multiplier']:g}×"   # e.g. "1.5×"
    p, dd = row["pct_from_ma"], row["drawdown"]
    side = "above" if p >= 0 else "below"

    if row["tier_dd"] > row["tier_ma"]:
        # The drawdown signal is the more aggressive one
        text = (f"QQQ sits {abs(p):.1f}% {side} its 200-day average but {dd:.1f}% below "
                f"its high, and the drawdown sets the tier: {t['name']} ({mult} contribution).")
    elif t["id"] == 0:
        text = (f"QQQ closed {abs(p):.1f}% above its 200-day average and only "
                f"{dd:.1f}% off its high, so a normal {mult} contribution applies.")
    elif t["id"] == 1:
        text = (f"QQQ closed {abs(p):.1f}% {side} its 200-day average, inside the "
                f"±{MA_BAND_PCT:g}% band, so the framework calls for a {mult} contribution.")
    else:
        text = (f"QQQ closed {abs(p):.1f}% below its 200-day average, which puts it in the "
                f"{t['name']} zone ({mult} contribution).")

    if row["daily_change"] <= DIP_DAY_PCT:
        text += f" The latest session fell {abs(row['daily_change']):.1f}%, which also flags a buy-the-dip day."
    return text


# -----------------------------------------------------------------------------
# 4. BACKTEST: plain monthly DCA vs. tiered DCA, same total dollars
# -----------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame) -> dict | None:
    """Buy on the first trading day of each month in the history window.

    Plain:  the same base amount every month.
    Tiered: base amount x that day's tier multiplier, then every tiered
            contribution is scaled by one constant so the TOTAL invested
            equals the plain total. That makes it a fair "same dollars,
            different timing" comparison. (The scaling uses hindsight about
            the total, so treat the result as illustrative.)
    Ending values use the latest close. Dividends are not included (both
    strategies would receive them roughly equally).
    """
    if df.empty:
        return None
    monthly = df.groupby([df.index.map(lambda d: d.year), df.index.map(lambda d: d.month)]).head(1)
    if len(monthly) < 6:
        return None

    base = BACKTEST_MONTHLY_AMOUNT
    mults = monthly["tier"].map(lambda t: TIERS[int(t)]["multiplier"])
    plain_amounts = pd.Series(base, index=monthly.index)
    scale = plain_amounts.sum() / (base * mults).sum()
    tiered_amounts = base * mults * scale

    last_close = float(df["close"].iloc[-1])

    def summarize(amounts: pd.Series) -> dict:
        shares = float((amounts / monthly["close"]).sum())
        invested = float(amounts.sum())
        value = shares * last_close
        return {
            "invested": round(invested, 2),
            "shares": round(shares, 4),
            "avg_cost": round(invested / shares, 2),
            "value": round(value, 2),
            "return_pct": round((value / invested - 1) * 100, 2),
        }

    plain, tiered = summarize(plain_amounts), summarize(tiered_amounts)
    return {
        "start": monthly.index[0].isoformat(),
        "end": df.index[-1].isoformat(),
        "months": int(len(monthly)),
        "monthly_amount": base,
        "scale_factor": round(float(scale), 4),
        "tier_months": {str(t["id"]): int((monthly["tier"] == t["id"]).sum()) for t in TIERS},
        "plain": plain,
        "tiered": tiered,
        "difference": round(tiered["value"] - plain["value"], 2),
        "difference_pct": round((tiered["value"] / plain["value"] - 1) * 100, 2),
    }


# -----------------------------------------------------------------------------
# 5. BUILD AND WRITE THE JSON
# -----------------------------------------------------------------------------

def framework_block() -> dict:
    """The rules, written into the JSON so the widget can display them."""
    rules = {
        0: f"Price more than {MA_BAND_PCT:g}% above the {MA_WINDOW}-day average",
        1: f"Price within ±{MA_BAND_PCT:g}% of the {MA_WINDOW}-day average",
        2: (f"{MA_BAND_PCT:g}–{MA_DEEP_PCT:g}% below the {MA_WINDOW}-day average, "
            f"or a {DD_LEAN_PCT:g}–{DD_AGGR_PCT:g}% drawdown from the high"),
        3: (f"More than {MA_DEEP_PCT:g}% below the {MA_WINDOW}-day average, "
            f"or a drawdown of more than {DD_AGGR_PCT:g}%"),
    }
    return {
        "ma_window": MA_WINDOW,
        "thresholds": {
            "ma_band_pct": MA_BAND_PCT,
            "ma_deep_pct": MA_DEEP_PCT,
            "dd_lean_pct": DD_LEAN_PCT,
            "dd_aggr_pct": DD_AGGR_PCT,
            "dip_day_pct": DIP_DAY_PCT,
        },
        "tiers": [dict(t, rule=rules[t["id"]]) for t in TIERS],
        "combine_rule": "When the signals disagree, the more aggressive tier applies.",
        "dip_rule": f"If the latest session closed down {abs(DIP_DAY_PCT):g}% or more, "
                    f"a buy-the-dip flag is shown on top of the tier.",
    }


def fetch_vix(start: date) -> dict | None:
    """Optional VIX reading. Failure here never blocks the main update."""
    sources = [("yfinance", lambda: fetch_yfinance(VIX_TICKER, start)),
               ("yahoo-chart", lambda: fetch_yahoo_chart(VIX_TICKER, start))]
    try:
        s, _ = get_prices(sources, min_rows=20, max_jump=None)
    except Exception as exc:
        gh_warning(f"VIX not updated ({exc})")
        return None
    return {
        "close": round(float(s.iloc[-1]), 2),
        "change": round(float(s.iloc[-1] - s.iloc[-2]), 2),
        "as_of": s.index[-1].isoformat(),
    }


def build_payload(df: pd.DataFrame, source: str, vix: dict | None) -> dict:
    latest = df.iloc[-1]
    cutoff = df.index[-1] - timedelta(days=int(HISTORY_YEARS * 365.25))
    hist = df[df.index > cutoff]
    tier = TIERS[int(latest["tier"])]
    high_date = df.index[df["close"] == latest["high"]][-1] if (df["close"] == latest["high"]).any() else None

    return {
        "ticker": TICKER,
        "last_updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "as_of": df.index[-1].isoformat(),
        "source": source,
        "framework": framework_block(),
        "latest": {
            "close": round(float(latest["close"]), 2),
            "ma": round(float(latest["ma"]), 2),
            "pct_from_ma": round(float(latest["pct_from_ma"]), 2),
            "high": round(float(latest["high"]), 2),
            "high_date": high_date.isoformat() if high_date else None,
            "drawdown_pct": round(float(latest["drawdown"]), 2),
            "daily_change_pct": round(float(latest["daily_change"]), 2),
            "tier": tier["id"],
            "tier_name": tier["name"],
            "multiplier": tier["multiplier"],
            "tier_from_ma": int(latest["tier_ma"]),
            "tier_from_drawdown": int(latest["tier_dd"]),
            "dip_day": bool(latest["daily_change"] <= DIP_DAY_PCT),
            "explanation": explain(latest),
        },
        "vix": vix,
        # Column-style arrays keep the file small (one entry per trading day)
        "history": {
            "dates": [d.isoformat() for d in hist.index],
            "close": [round(float(v), 2) for v in hist["close"]],
            "ma": [round(float(v), 2) for v in hist["ma"]],
            "tier": [int(v) for v in hist["tier"]],
        },
        "backtest": run_backtest(hist) if BACKTEST_ENABLED else None,
    }


def write_json_atomic(payload: dict, path: Path) -> None:
    """Write to a temp file first, then swap it in, so a crash mid-write can
    never leave a half-written market.json on the website."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), allow_nan=False)
    os.replace(tmp, path)


def main() -> int:
    sources = [
        ("yfinance",    lambda: fetch_yfinance(TICKER, HISTORY_START)),
        ("yahoo-chart", lambda: fetch_yahoo_chart(TICKER, HISTORY_START)),
        ("stooq",       lambda: fetch_stooq(STOOQ_SYMBOL, HISTORY_START)),
    ]
    try:
        closes, source = get_prices(sources, MIN_ROWS)
        df = compute_signals(closes)
        vix = fetch_vix(date.today() - timedelta(days=60)) if INCLUDE_VIX else None
        payload = build_payload(df, source, vix)
    except Exception as exc:
        if OUTPUT_PATH.exists():
            gh_warning(f"Data NOT updated; keeping the previous {OUTPUT_PATH.name}. Reason: {exc}")
            return 0   # not a failure: the site keeps serving the last good file
        log.error("No data and no previous file to fall back on: %s", exc)
        return 1

    write_json_atomic(payload, OUTPUT_PATH)
    lt = payload["latest"]
    log.info("Wrote %s | as of %s | close %.2f | %+.2f%% vs 200DMA | drawdown %.2f%% | Tier %d (%s)%s",
             OUTPUT_PATH, payload["as_of"], lt["close"], lt["pct_from_ma"], lt["drawdown_pct"],
             lt["tier"], lt["tier_name"], " + dip day" if lt["dip_day"] else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
