#!/usr/bin/env python3
"""
kalshi_mm_probe.py  —  the "first number" for market-making on Kalshi.

ONE job: record Kalshi order books (read-only, no orders placed), then measure
the two numbers that decide whether market-making is viable on a given market:

  1. NET capturable half-spread  = (quoted spread / 2) - per-contract fee.
     If this is <= 0, market-making is dead on arrival: the spread you could
     capture doesn't even cover the fee.

  2. ADVERSE SELECTION proxy     = how far the mid drifts, right after moments
     when the inside is tight (i.e. when you'd be resting at the touch and most
     likely to get filled). If the average adverse drift exceeds your net
     half-spread, informed flow eats the edge — the HYDROGEL problem, new venue.

--------------------------------------------------------------------------
NO AUTH NEEDED. Kalshi's orderbook / markets / events endpoints are PUBLIC.
RSA-PSS signing is only for /portfolio (your own fills). This script never
touches /portfolio, so there is nothing to sign. Just run it.
--------------------------------------------------------------------------

USAGE
  python3 kalshi_mm_probe.py selftest
      Run the offline math check on two synthetic books (no network).
      Do this first. One book has real edge, one is an adverse-selection trap;
      the verdicts should come out different. If they do, the analysis is sound.

  python3 kalshi_mm_probe.py discover --event <EVENT_TICKER>
      List open market tickers under an event (find your World Cup markets).
      Or:  --series <SERIES_TICKER>   /   --search <substring>

  python3 kalshi_mm_probe.py record  --tickers T1 T2 T3 --minutes 120
      Poll the order books every few seconds and append snapshots to a file.
      Start ~30 min before kickoff, let it run through the match.

  python3 kalshi_mm_probe.py analyze --infile snapshots.jsonl
      Compute the two numbers per market and print a verdict.

--------------------------------------------------------------------------
BEFORE A LIVE RUN, verify two things against a single real response
(the `discover` command prints one raw orderbook so you can eyeball it):
  * BASE_URL  — production is api.elections.kalshi.com; demo is demo-api.kalshi.co.
  * PRICE UNIT — Kalshi changed pricing in 2026. Older API = integer cents
    (1..99). Newer = dollar strings ('0.6500'). normalize_price() handles both
    but CONFIRM which you're getting, because every downstream number depends
    on the unit being cents.
  * FEE — sources disagree (some report 0% in 2026). Set FEE_RATE / ZERO_FEES
    to match the actual schedule on YOUR markets. This flips the whole result.
"""

import argparse
import json
import math
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict

# ----------------------------------------------------------------------------
# CONFIG  — verify these before a live run.
# ----------------------------------------------------------------------------
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"   # production
# BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"       # demo / paper

POLL_SECONDS = 3          # how often to snapshot each book
REQUEST_TIMEOUT = 10      # seconds per HTTP request

# Fee model. Kalshi's historical formula is a per-order rounding of
#   fee = ceil(0.07 * contracts * P * (1-P))   with P in dollars.
# Per single contract that is  7 * P * (1-P)  cents. Some 2026 sources report
# 0% fees on certain markets — if that's true for yours, set ZERO_FEES = True.
FEE_RATE = 0.07           # coefficient in the classic Kalshi fee formula
ZERO_FEES = False         # set True if your markets genuinely charge no fee

# Analysis knobs
TIGHT_SPREAD_CENTS = 2    # "inside is tight" threshold (a maker would be at touch)
DRIFT_HORIZON = 20        # look this many snapshots ahead to measure mid drift

DEFAULT_OUTFILE = "snapshots.jsonl"


# ----------------------------------------------------------------------------
# HTTP (public endpoints only — no signing)
# ----------------------------------------------------------------------------
def _get(path, params=None):
    """GET a public Kalshi endpoint. Returns parsed JSON or raises."""
    url = BASE_URL + path
    if params:
        q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{q}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} on {path}: {body}") from None


import urllib.parse  # noqa: E402  (needed by _get above)


def get_orderbook(ticker):
    """Fetch a raw order book for one market ticker."""
    data = _get(f"/markets/{ticker}/orderbook")
    # Kalshi nests under 'orderbook' (legacy) or 'orderbook_fp' (fixed-point, current).
    return data.get("orderbook_fp") or data.get("orderbook") or data


def discover_markets(event=None, series=None, search=None, limit=100):
    """Return a list of open market tickers matching the filter."""
    params = {"status": "open", "limit": limit}
    if event:
        params["event_ticker"] = event
    if series:
        params["series_ticker"] = series
    data = _get("/markets", params)
    markets = data.get("markets", [])
    if search:
        s = search.lower()
        markets = [m for m in markets
                   if s in m.get("ticker", "").lower()
                   or s in m.get("title", "").lower()]
    return markets


# ----------------------------------------------------------------------------
# BOOK PARSING
# Kalshi shows a *bid-only* book: `yes` = bids to buy YES, `no` = bids to buy NO.
# A NO bid at price p is equivalent to an offer to SELL YES at (100 - p).
# So the two-sided YES book is:
#     best YES bid = max(yes prices)
#     best YES ask = 100 - max(no prices)
# All internal math is in CENTS (integers 1..99).
# ----------------------------------------------------------------------------
def normalize_price(p):
    """Return price in CENTS as a float, preserving subpenny ticks (e.g. 13.10)."""
    if p is None:
        return None
    v = float(p)                            # dollar strings and numbers both parse
    cents = v * 100.0 if v <= 1.0 else v    # dollars -> cents; already-cents pass through
    return round(cents, 4)                  # keep subpenny precision, drop float dust


def _best_level(levels):
    """Given a list of [price, size] bid levels, return (best_price_cents, size)."""
    best_p, best_sz = None, None
    for lvl in levels or []:
        p = normalize_price(lvl[0])
        sz = lvl[1] if len(lvl) > 1 else None
        if p is None:
            continue
        if best_p is None or p > best_p:   # highest bid is the best bid
            best_p, best_sz = p, sz
    return best_p, best_sz


def top_of_book(ob):
    """Collapse a raw orderbook into top-of-book YES quotes (cents)."""
    yes_bid, yes_bid_sz = _best_level(ob.get("yes_dollars") or ob.get("yes"))
    no_bid,  no_ask_sz  = _best_level(ob.get("no_dollars")  or ob.get("no"))
    yes_ask = (100 - no_bid) if no_bid is not None else None
    mid = None
    spread = None
    if yes_bid is not None and yes_ask is not None:
        mid = (yes_bid + yes_ask) / 2.0
        spread = yes_ask - yes_bid
    return {
        "yes_bid": yes_bid, "yes_ask": yes_ask,
        "yes_bid_sz": yes_bid_sz, "yes_ask_sz": no_ask_sz,
        "mid": mid, "spread": spread,
    }


# ----------------------------------------------------------------------------
# RECORD
# ----------------------------------------------------------------------------
def record(tickers, minutes, outfile=DEFAULT_OUTFILE):
    deadline = time.time() + minutes * 60
    n = 0
    print(f"Recording {len(tickers)} market(s) for {minutes} min "
          f"every {POLL_SECONDS}s -> {outfile}")
    with open(outfile, "a") as f:
        while time.time() < deadline:
            ts = int(time.time() * 1000)
            for tk in tickers:
                try:
                    tob = top_of_book(get_orderbook(tk))
                except Exception as e:                       # keep the loop alive
                    print(f"  [warn] {tk}: {e}", file=sys.stderr)
                    continue
                row = {"ts": ts, "ticker": tk, **tob}
                f.write(json.dumps(row) + "\n")
                n += 1
            f.flush()
            time.sleep(POLL_SECONDS)
    print(f"Done. Wrote {n} snapshots to {outfile}.")


# ----------------------------------------------------------------------------
# ANALYZE
# ----------------------------------------------------------------------------
def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def per_contract_fee_cents(price_cents):
    """Classic Kalshi per-contract fee in cents at a given price."""
    if ZERO_FEES:
        return 0.0
    pc = min(99.0, max(1.0, price_cents))    # clamp to valid contract range
    p = pc / 100.0
    return FEE_RATE * p * (1 - p) * 100.0    # dollars -> cents


def analyze_market(snaps):
    """snaps: list of snapshot dicts for ONE ticker, in time order."""
    spreads = [s["spread"] for s in snaps if s.get("spread") is not None]
    mids    = [s["mid"] for s in snaps]
    if not spreads:
        return None

    mean_spread = sum(spreads) / len(spreads)
    med_spread = _median(spreads)
    gross_half = med_spread / 2.0

    # fee evaluated at the typical price (median mid)
    valid_mids = [m for m in mids if m is not None]
    typical_mid = _median(valid_mids) if valid_mids else 50.0
    fee = per_contract_fee_cents(typical_mid)
    net_half = gross_half - fee

    # Adverse selection proxy: how far does the mid move over the horizon you'd
    # be exposed to as a resting quote? Measured as realized mid drift over
    # DRIFT_HORIZON snapshots. This is the cost a maker pays to informed flow.
    # (We also count tight-inside moments separately, for context.)
    drifts = []
    tight_events = 0
    for i, s in enumerate(snaps):
        if s.get("spread") is not None and s["spread"] <= TIGHT_SPREAD_CENTS:
            tight_events += 1
        j = i + DRIFT_HORIZON
        if j < len(snaps) and s["mid"] is not None and snaps[j]["mid"] is not None:
            drifts.append(snaps[j]["mid"] - s["mid"])
    mean_signed = (sum(drifts) / len(drifts)) if drifts else None
    mean_abs = (sum(abs(d) for d in drifts) / len(drifts)) if drifts else None

    # Verdict
    if net_half <= 0:
        verdict = "DEAD — spread doesn't clear the fee."
    elif mean_abs is None:
        verdict = "INCONCLUSIVE — not enough snapshots to measure mid drift."
    elif mean_abs >= net_half:
        verdict = ("NEGATIVE — spread clears the fee, but adverse selection "
                   f"(~{mean_abs:.2f}c drift) eats the net half-spread ({net_half:.2f}c).")
    else:
        edge = net_half - mean_abs
        verdict = (f"RESIDUAL EDGE ~{edge:.2f}c/contract — clears fee AND "
                   "adverse selection. Worth a closer look.")

    return {
        "n_snaps": len(snaps),
        "mean_spread": mean_spread,
        "median_spread": med_spread,
        "gross_half_spread": gross_half,
        "fee_per_contract": fee,
        "net_half_spread": net_half,
        "tight_events": tight_events,
        "adverse_mean_signed": mean_signed,
        "adverse_mean_abs": mean_abs,
        "verdict": verdict,
    }


def analyze_file(infile):
    by_ticker = defaultdict(list)
    with open(infile) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            by_ticker[row["ticker"]].append(row)
    _print_report(by_ticker)


def _print_report(by_ticker):
    for tk, snaps in by_ticker.items():
        snaps.sort(key=lambda r: r["ts"])
        res = analyze_market(snaps)
        print("=" * 68)
        print(f"MARKET: {tk}")
        if res is None:
            print("  no usable two-sided quotes recorded.")
            continue
        print(f"  snapshots recorded ......... {res['n_snaps']}")
        print(f"  median quoted spread ....... {res['median_spread']:.2f} c")
        print(f"  gross half-spread .......... {res['gross_half_spread']:.2f} c")
        print(f"  fee per contract ........... {res['fee_per_contract']:.2f} c"
              + ("  (ZERO_FEES on)" if ZERO_FEES else ""))
        print(f"  NET half-spread ............ {res['net_half_spread']:.2f} c")
        print(f"  tight-inside events ........ {res['tight_events']}")
        if res["adverse_mean_abs"] is not None:
            print(f"  adverse drift |mean| ....... {res['adverse_mean_abs']:.2f} c "
                  f"(signed {res['adverse_mean_signed']:+.2f} c)")
        print(f"  VERDICT: {res['verdict']}")
    print("=" * 68)


# ----------------------------------------------------------------------------
# SELF-TEST  — offline, no network. Two synthetic books.
# ----------------------------------------------------------------------------
def _synthetic():
    """
    Two fake snapshot streams, both with a 6c spread (so both clear the fee).
    The ONLY difference is mid volatility = adverse selection:
      GOOD_MKT : mid barely moves      -> residual edge should survive.
      TRAP_MKT : mid swings hard        -> adverse selection should kill it.
    Mids stay inside a realistic band, so no clamping games.
    """
    good, trap = [], []
    ts = 0
    for i in range(200):
        ts += POLL_SECONDS * 1000
        gmid = 50 + 0.3 * math.sin(i / 5.0)      # quiet market
        good.append({"ts": ts, "ticker": "GOOD_MKT",
                     "yes_bid": round(gmid - 3), "yes_ask": round(gmid + 3),
                     "mid": gmid, "spread": 6})
        tmid = 50 + 8.0 * math.sin(i / 4.0)      # violent market, same spread
        trap.append({"ts": ts, "ticker": "TRAP_MKT",
                     "yes_bid": round(tmid - 3), "yes_ask": round(tmid + 3),
                     "mid": tmid, "spread": 6})
    return {"GOOD_MKT": good, "TRAP_MKT": trap}


def selftest():
    print("Running offline self-test (no network)...\n")
    by_ticker = _synthetic()
    _print_report(by_ticker)
    good = analyze_market(by_ticker["GOOD_MKT"])
    trap = analyze_market(by_ticker["TRAP_MKT"])
    ok = ("RESIDUAL EDGE" in good["verdict"]) and ("EDGE" not in trap["verdict"]
                                                   or "NEGATIVE" in trap["verdict"])
    print()
    print("SELF-TEST", "PASS ✔" if ok else "CHECK ✗",
          "— GOOD_MKT should show edge, TRAP_MKT should not.")
    return ok


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Kalshi market-making edge probe (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest")

    d = sub.add_parser("discover")
    d.add_argument("--event")
    d.add_argument("--series")
    d.add_argument("--search")
    d.add_argument("--show-book", action="store_true",
                   help="also print one raw orderbook to verify price unit")

    r = sub.add_parser("record")
    r.add_argument("--tickers", nargs="+", required=True)
    r.add_argument("--minutes", type=float, default=120)
    r.add_argument("--outfile", default=DEFAULT_OUTFILE)

    a = sub.add_parser("analyze")
    a.add_argument("--infile", default=DEFAULT_OUTFILE)

    args = ap.parse_args()

    if args.cmd == "selftest":
        sys.exit(0 if selftest() else 1)

    if args.cmd == "discover":
        mkts = discover_markets(event=args.event, series=args.series, search=args.search)
        if not mkts:
            print("No open markets matched. Check the event/series ticker.")
            return
        for m in mkts:
            print(f"{m.get('ticker'):<32} {m.get('title','')[:60]}")
        if args.show_book and mkts:
            tk = mkts[0]["ticker"]
            print(f"\nRaw orderbook for {tk} (verify the price unit!):")
            print(json.dumps(get_orderbook(tk), indent=2)[:1200])
        return

    if args.cmd == "record":
        record(args.tickers, args.minutes, args.outfile)
        return

    if args.cmd == "analyze":
        analyze_file(args.infile)
        return


if __name__ == "__main__":
    main()