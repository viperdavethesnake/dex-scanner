# Recommendations — 2026-06-02

Written after stopping the full stack for a reset. Based on 15 days of collector
data (64k rows), 58 shadow trades, and walk-forward ML analysis.

---

## The honest summary

The scanner works. The trading layer does not, for fixable reasons. The right move
is to separate the two clearly: use the scanner, pause the trader, fix the specific
problems before attempting live trading.

---

## 1. Restart the scanner only

The scanner (webhook → LLM → HTML) has always worked and was never broken. Restart
it when you want to use it:

```bash
docker compose up -d llamacpp timescaledb n8n
```

Do not restart the collector or trader at the same time. They are independent.

**What the scanner is good for:** Manual screening. You trigger the webhook, you
read the output, you decide. The LLM does qualitative pattern matching that no
model trained on DexScreener features can replicate — it considers token names,
launch behavior, concentration patterns, and narrative context.

---

## 2. Restart the collector when you want to accumulate data

The collector is running cleanly and passively. Restart it independently:

```bash
docker compose up -d dex-collector-db dex-collector
```

It requires no attention once running. Every day it adds ~2,400 Base + ~3,500 Solana
rows with 5-minute outcomes. This data is the only thing that will make the model
better over time.

Do not start the collector unless you intend to leave it running for weeks. Short
runs produce data but the time-series gaps confuse walk-forward validation.

---

## 3. Do not restart the shadow trader yet

The shadow trader has three unfixed problems. Any of them alone would justify
stopping. All three together make it a money sink:

**a) Double-momentum selection.** The hard_filter pre-selects pumping tokens;
the model also prefers pumping tokens. Combined they select end-of-pump, not
beginning-of-pump. The trader performs worse than random as a result. Fix: let
the model handle momentum selection; strip micro_trend and sell_pressure exclusions
from hard_filter.

**b) Cost structure.** $10 positions pay ~8.3% round-trip in gas + slippage.
The model edge is ~15pp lift in win rate. At average outcome distributions, this
produces ~+2% expected net — fragile and barely positive. At $100 positions, the
same edge produces ~+9% expected net. The minimum viable position size is $50–100,
not $10.

**c) Stop-loss blind spot.** Rugged pairs disappear from DexScreener. The stop-loss
never fires. The trader holds a rugged position for 5 minutes and exits at
the crashed aggregator price. Fix: treat a None DexScreener fetch as exit signal.

None of these are hard to fix. But fix them before running the trader again.

---

## 4. What to fix before the next shadow run

In priority order:

### Fix 1: Hard-filter decoupling (before model scoring)

In `dex-trader/signals.py`, remove `micro_trend` and `sell_pressure_5m` from
`hard_filter()`. Keep only hard safety gates: age window, extreme V/L (>10),
liquidity floor (<$1k). Let the model score the full filtered-in population.
Re-add micro_trend and sell_pressure as features if the model finds them useful.

### Fix 2: Stop-loss on None price fetch

In `dex-trader/main.py:_manage_open_positions`, when
`_fetch_dexscreener_price` returns None for a position that has been held for
>60 seconds, treat it as a stop-loss trigger. Exit immediately at `fill_price * 0.5`
or via aggregator quote. Do not wait for the timer.

### Fix 3: Honest cost accounting in next backtest

Before running any new backtest, replace the 1.5% round-trip assumption with the
measured 8.3%. Any strategy that doesn't show positive expected value at 8.3% cost
should not be run in shadow mode. If it doesn't clear that bar on paper, it won't
clear it live.

### Fix 4: Position size

Update `TRADE_SIZE_USD` from $10 to $50 in `compose.yaml` for the next shadow run.
This cuts gas as a percentage of trade from ~5% to ~1%. The model's edge becomes
tradeable at that cost level.

---

## 5. What to delete

**Delete:** `prompts/2026-05-24-shadow-trader.md`
This prompt drove the shadow trader build (Phases 1–4). All phases are complete
or explicitly paused. It is now redundant and will confuse future sessions.

**Archive or delete:** `docs/SHADOW-TRADER-PROJECTIONS.md`
The projections in that document are based on 1.5% cost assumption and a
backtest win rate that did not survive contact with real execution. The document
is misleading as a reference.

---

## 6. What not to do

- Do not add more data sources (Birdeye new_listing Phase 2) until the trader
  problems are fixed. More intake data does not fix execution problems.

- Do not retrain the model until hard_filter is decoupled. A model trained against
  hard-filtered data will learn the wrong distribution. Train on the full collector
  population, then apply model threshold at inference.

- Do not attempt live trading until shadow run at $50 position size shows at least
  200 exits at positive expected value after the 8.3% cost assumption.

---

## 7. Longer-term: on-chain features

The current model ceiling is approximately AUC 0.63–0.66 using DexScreener features.
That is not bad, but it is fragile because deployers can manipulate every feature
the model sees. On-chain features that cannot be easily faked:

- Deployer wallet age and transaction history
- LP lock status and lock duration at token launch
- Holder concentration at mint (top 10 wallets %)
- Number of unique buyers in first 60 seconds

These are available via Alchemy/Infura on-chain calls or GoPlus API. They would
require changes to the collector schema and a new training run. Worth doing in a
future session after the execution problems are fixed.

---

## Decision summary

| Item | Decision |
|------|----------|
| Scanner | Restart when needed — it works |
| Collector | Restart when you want passive data accumulation |
| Shadow trader | Do not restart until 3 bugs fixed + position size updated |
| Model | Do not retrain until hard_filter is decoupled |
| Prompt file | Delete |
| Shadow trader projections doc | Archive or delete |
| Next milestone | 3 bug fixes → shadow run at $50 → 200 exits → evaluate |
