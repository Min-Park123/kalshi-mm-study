# Market-Making Viability on Kalshi Prediction Markets

*An empirical microstructure study of whether a fee-paying retail participant can
capture positive edge by quoting a resting two-sided market on live Kalshi sports
markets — and, where they cannot, which cost binds.*

Author: Minyoung Park · July 3, 2026 · Data collected during the 2026 Men's World Cup.

---

## 1. Question

Profit per fill for a resting market-making quote decomposes as:

    net = s − α·J − fee

where `s` is the half-spread earned per fill, `fee` is the per-contract exchange fee,
and `α·J` is adverse selection (α = fraction of toxic fills, J = adverse mid move on
those fills). The question is decomposed into two measurable parts: does the quoted
spread clear the fee, and does adverse selection consume what remains. The aim is not
a yes/no on profitability but identifying which term binds, per market regime.

## 2. Method

Order books were recorded read-only via Kalshi's public `/markets/{ticker}/orderbook`
endpoint. No authentication is required for market data and no orders were placed at
any point.

Kalshi serves a bid-only book: resting YES bids and resting NO bids. The two-sided YES
book is reconstructed as best YES bid = max(YES bids), best YES ask = 100 − max(NO bids).
The current API nests prices under `orderbook_fp` with `*_dollars` keys as fixed-point
dollar strings at sub-cent tick sizes — a format not reflected in most public
documentation, verified here against raw responses. The parser preserves sub-cent
precision; rounding to integer cents would corrupt spreads on markets where the entire
quoted spread is under one cent.

Two metrics are computed per market: net capturable half-spread after fee (median
quoted spread / 2, minus the modeled per-contract fee), and mean absolute mid drift
over a fixed fill horizon as an adverse-selection proxy, reported at 15s and 60s
horizons to distinguish jump repricing from diffusion. Fees are modeled with Kalshi's
`0.07 · P · (1−P)` formula; the live fee schedule was not independently verified
against executed fills, so results under a zero-fee assumption are reported alongside.

## 3. Data

- Spain vs. Austria, 2 July 2026 — both tournament-winner contracts, 2,736 snapshots
  per market at 3-second intervals through the full match.
- Portugal vs. Croatia, 2 July 2026 — both tournament-winner contracts, 920 snapshots
  (degraded collection, ~8% yield due to intermittent network loss).
- Australia vs. Egypt, 3 July 2026 — both tournament-winner contracts, 1,905 snapshots
  per market at ~4-second effective intervals through regulation, extra time, and a
  penalty shootout (clean collection; zero fetch errors).

## 4. Results

| Market                | Median spread | Gross half-spread | Fee/contract | Net half-spread | Adverse drift (15s / 60s) | Verdict |
|-----------------------|--------------:|------------------:|-------------:|----------------:|--------------------------:|---------|
| Spain (favorite)      | 0.10¢         | 0.05¢             | 0.70¢        | −0.65¢          | 0.02¢ / 0.04¢             | Dead    |
| Austria (longshot)    | 0.10¢         | 0.05¢             | 0.07¢        | −0.02¢          | 0.00¢ / 0.00¢             | Dead    |
| Portugal              | 0.10¢         | 0.05¢             | 0.40¢        | −0.35¢          | n/a                       | Dead    |
| Croatia               | 0.10¢         | 0.05¢             | 0.07¢        | −0.02¢          | n/a                       | Dead    |
| Egypt (longshot)      | 0.10¢         | 0.05¢             | 0.07¢        | −0.02¢          | 0.00¢ / 0.00¢             | Dead    |
| Australia (longshot)  | n/a           | n/a               | n/a          | n/a             | n/a                       | No two-sided book |

## 5. Finding

Across the sampled markets, three distinct failure regimes emerged: on liquid books
the fee dominates a spread too thin to clear it; on deep-longshot books no two-sided
market exists at all; and at discrete resolution events makers withdraw and the price
jumps, so a resting quote faces adverse selection as absent liquidity rather than
drift.

Adverse selection, which the model predicted would bind during live play, did not.
Drift was at most 0.04¢ even at a 60-second horizon, despite meaningful repricing over
the match — Spain's mid moved from ~10.2¢ to ~12.15¢. The mid path shows information
arriving as one-tick-at-a-time diffusion with long plateaus rather than jumps. A
resting quote in this book is not picked off by informed flow; it simply cannot earn a
spread wide enough to pay the fee. These are distinct failure modes: the markets are
efficient in the no-spread sense, not the toxic-flow sense.

Under a zero-fee assumption the net half-spread turns marginally positive (~0.05¢
gross against ~0.04¢ drift), but the margin is within measurement noise and below
realistic execution friction — approximately break-even, not exploitable edge.

A replication attempt (Portugal–Croatia) was degraded by network loss. Spread metrics
were broadly consistent with Spain–Austria; drift metrics were not computable from the
irregularly sampled data, since the fixed snapshot-count horizon no longer corresponds
to a fixed time interval.

### 5.1 Behavior at discrete resolution (penalty shootout)

The Australia–Egypt match went to penalties, providing a first observation of these
markets under a discrete resolution event. Three behaviors appeared in sequence in
Egypt's book. During the early kicks the two-sided book withdrew entirely (no
reconstructable quote for ~100 seconds). It then re-formed and froze at a 0.10¢ spread
through the middle kicks. At resolution, the mid repriced from 0.15¢ to 0.25¢ in a
single ~4-second sampling interval — a discrete jump equal to twice the capturable
half-spread, with no intermediate prints.

This confirms the roadmap hypothesis in modified form: under jump conditions, makers
do not defend by widening — they withdraw, and the price gaps when the book re-forms.
Adverse selection in this regime manifests as absent liquidity rather than measurable
drift, which is why aggregate drift statistics (computed over the full match) remain
near zero. Caveat: with the contract priced one to two ticks above zero, the observed
jump is a single tick on a quantized ladder; the withdrawal–freeze–gap pattern, not
its magnitude, is the finding.

Separately, Australia's contract had no two-sided book at any point in the match: the
bid side was absent in all 1,905 snapshots. For deep-longshot tournament contracts,
the market-making question is moot — there is no market to make.

## 6. Limitations

- Winner markets are an indirect target: a single match only partially reprices a
  team's tournament odds, attenuating adverse selection relative to a match moneyline.
- The adverse-selection proxy is book-only. It measures realized mid drift over a
  fixed horizon, not fill-conditional toxicity, since no orders were executed.
- The Portugal–Croatia recording yielded ~8% of intended snapshots due to network
  loss; spread metrics are reported, drift metrics withheld. A clean replication
  (Australia–Egypt) is in collection.
- One tournament, one market class. No thin or obscure market has been sampled yet,
  which is where the binding constraint could plausibly differ.

## 7. Roadmap

- Match-moneyline contrast: record a single-game regulation-result market during live
  play, where a thinner book may quote a wider spread but reprice in jumps (larger J,
  higher α). Tests whether the binding constraint flips from fees to adverse selection.
- Cross-venue price discovery: Kalshi vs. Polymarket on identical outcomes, using
  Hasbrouck information shares / Gonzalo–Granger decomposition to measure which venue
  leads. Reuses this study's recorder as the collection layer.

## 8. Reproduce

- `kalshi_mm_probe.py` — recording and analysis instrument. Offline self-test:
  `python3 kalshi_mm_probe.py selftest`
- `snapshots_spain_austria.jsonl`, `snapshots_portugal_croatia.jsonl` — raw books.
- Analysis: `python3 kalshi_mm_probe.py analyze --infile <file>`
