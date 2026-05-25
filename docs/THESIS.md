# Trading Thesis

## What We're Actually Doing

This is **DEX meme coin scalping**. Not investing. Not analysis. Not long-term holds.

New tokens launch on Base and Solana DEXes constantly — hundreds per day. The vast majority are worthless. They launch, get discovered by bots and degens, pump 10–30% in a few minutes on pure hype and FOMO, then collapse as early buyers exit into the liquidity. The token is junk. That doesn't matter.

**The only question is: is this token being pumped right now, and can we get in and out before it reverses?**

The window is 5 minutes. Exit is hard — no exceptions, no "let's see where it goes." The model was trained on 5-minute outcomes. Beyond 5 minutes the data consistently shows mean reversion: even the best tokens at the best momentum states give back gains at 15 minutes and go negative at 30 minutes. The scanner finds momentum events, not investments.

---

## What the Data Confirms

From 28,000+ unbiased observations (all tokens, not just scanner survivors):

**The pump is real and short.** Tokens in the best momentum state (micro_trend=up, volume accelerating, early age) average +5–11% at 5 minutes. The same tokens average negative by 15–30 minutes. The window is exactly as short as the thesis assumes.

**Chain matters more than anything.** Base is consistently more predictable and better-performing than Solana with available data. Base tokens at the right momentum state average +6–11% at 5 minutes with ~50% win rate after costs. Solana requires additional data (Birdeye net inflow) to reach comparable predictability.

**The model finds momentum, not quality.** The top predictive features are:
- `volume_5m` — is there actual buying happening right now?
- `txn_accel` — are buy transactions accelerating vs the 1-hour baseline?
- `price_ch_6h` — was there a move building before this 5-minute window?
- `buy_pct_1h` — have buyers been in control for an extended period?

None of these are project quality signals. They're all pure momentum. The model is asking: *is someone buying this aggressively enough that there's a wave to ride?*

**Wins are larger than losses.** At model threshold ≥0.70, average winning trade = +$3.22 on $10, average losing trade = −$1.68 on $10. The asymmetry exists because winning trades are catching actual pumps with room to run; losing trades are stopped hard at 5 minutes. This is the core of the edge.

---

## What Traditional Market Thinking Gets Wrong Here

Do not apply:
- **Fundamental analysis** — the token has no fundamentals. Holder count, LP burn, insider distribution matter only as pump indicators (does the structure allow a clean pump?), not as investment signals.
- **Long-term trend analysis** — 6h and 1h data is used to confirm momentum context, not to project a trend. By the time a trend is visible it's already reversing.
- **Risk/reward ratios from traditional markets** — a 1.5% cost on a $10 trade is noise. A 10% pump in 5 minutes is the signal. Traditional position sizing and Kelly criterion don't map to this.
- **"Good tokens will outperform bad tokens"** — false. INTERESTING ≠ better 5-minute return. The LLM rating adds almost zero predictive value for the 5-minute window. The ML model on raw momentum signals outperforms the LLM's conviction assessment.

---

## The Safety Filter's Role

Safety filters exist for one reason in this context: **can we actually sell?**

A honeypot where sell transactions are blocked = we can't execute the exit. That kills the strategy regardless of how good the pump looks. Honeypot.is (Base) and RugCheck's honeypot flag (Solana) are the only safety checks that truly matter for 5-minute scalping.

The other safety checks (GoPlus flags, holder distribution, LP burn) are useful for the manual LLM sessions where you're making a larger discretionary call. For the automated 5-minute scalp: if you can buy it and sell it, it's tradeable. Everything else is noise at this time horizon.

---

## The LLM's Role (Manual Sessions Only)

The LLM adds value in **manual trading sessions** where you're looking at a token and making a discretionary call. It reads narrative signals the data can't capture — is the name memeable? Does the launch pattern look like organic degen energy or a coordinated insider pump? Is the lifecycle stage early enough to have room to run?

For the **automated trading path**, the LLM is not in the loop. The ML model score is the signal. Threshold exceeded → buy → 5-minute timer → sell. That's the entire decision tree.

---

## The Numbers

**Target edge:** Profit factor 1.5–2.5x (for every $1 lost, make $1.50–$2.50 back).  
**Win rate after 1.5% round-trip cost:** 49–55% depending on threshold.  
**Average winning trade:** +20–32% on position size.  
**Average losing trade:** −16–20% on position size (hard stopped at 5m).  
**Position size:** $10–15 per trade, max 3 simultaneous, hard daily loss limit.  
**Target wallet size:** $200–300 (covers max observed drawdown with buffer).
